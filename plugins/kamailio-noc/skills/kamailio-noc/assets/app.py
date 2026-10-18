#!/usr/bin/env python3
"""Kamailio NOC - healthcheck + monitoreo carrier-grade (Vodafone/AT&T style).

Backend stdlib-only: collector (kamcmd + xhttp opcional + SIP OPTIONS + MySQL + PDD pasivo + MOS RTPEngine NG)
+ API JSON + sirve dashboard. Historico 24h en SQLite.

Endpoints:
  GET /                      -> dashboard
  GET /api/snapshot          -> estado actual + rates/sec + health + alertas
  GET /api/history?minutes=N -> series para graficas (max 1440)
  GET /api/healthz           -> liveness simple para LB/K8s
  GET /api/calls             -> calidad por llamada (MOS/jitter/perdida/RTT + origen/destino)
  GET /api/alarms?hours=N    -> historial de alarmas (persistidas en sqlite)
  POST /api/alarms/ack       -> reconocer alarma {"id": N} (JSON, requiere sesion)
"""
import ctypes
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "noc_history.db")
USERS_FILE = os.path.join(BASE, "users.json")
POLL_INTERVAL = 2.0       # s, muestreo en memoria
HIST_EVERY = 5.0          # s, escritura a sqlite
RETENTION_H = 26          # h

# --- auth ---
SESSION_TTL = 8 * 3600    # s, sesion absoluta
PBKDF2_ITER = 200_000
MAX_FAILS = 5             # intentos antes de bloqueo
FAIL_WINDOW = 300         # s, ventana de conteo
LOCK_SECS = 900           # s, duracion del bloqueo
COOKIE_NAME = "noc_session"
_fails = {}               # ip -> [ts...]
_locked = {}              # ip -> hasta (ts)
_sys_prev = {"cpu": None, "net": {}}  # deltas CPU y red

# ---------- configuracion por sitio (noc.conf.json, 600) ----------
# Todo lo especifico del proxy vive en el JSON; ver noc.conf.example.json.
CONFIG_PATH = os.environ.get("NOC_CONFIG", os.path.join(BASE, "noc.conf.json"))
DEFAULT_CONFIG = {
    "site_name": "", "sip_domain": "", "sip_targets": [],
    "kamailio_http": "",            # "" = sin endpoints xhttp; liveness via kamcmd
    "db": None,                     # {"host","port","user","password","name"} o null
    "ha": {"vip": "", "keepalived_service": "keepalived"},
    "core_ips": [],                 # IPs del core (FS/SBC): INVITE desde aqui = entrante
    "dispatcher_ignore_sets": [],   # sets visibles pero que no alarman
    "rtpengine": {"ng": "127.0.0.1:2223", "service": "rtpengine-daemon", "kernel_table": 0},
    "pdd": {"enabled": True, "bpf": "udp and (port 5060 or port 5070)"},
    "mos_bad": 3.6,
    "listen_port": 8899,
    "baseline_sigma": 3.0,          # desviaciones bajo la linea base para alarmar (ASR/CPS)
    # notificaciones de alarmas (vacio = deshabilitado)
    "notify": {"telegram_token": "", "telegram_chat": "", "webhook_url": "",
               "min_sev": "major", "delay_s": 30},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        print(f"[noc] sin {CONFIG_PATH}: usando valores por defecto", flush=True)
    return cfg


CFG = load_config()
_KHTTP = (CFG["kamailio_http"] or "").rstrip("/")
METRICS_URL = _KHTTP + "/metrics" if _KHTTP else ""
HEALTHZ_URL = _KHTTP + "/healthz" if _KHTTP else ""
READYZ_URL = _KHTTP + "/readyz" if _KHTTP else ""
SIP_TARGETS = [(h, int(p)) for h, p in CFG["sip_targets"]]
DSP_IGNORE_SETS = set(CFG["dispatcher_ignore_sets"])
DB = CFG["db"] or None
MYCNF = os.path.join(BASE, ".noc-my.cnf")


def write_mycnf():
    """Credenciales MySQL en fichero 600 (no en argv, visible en ps)."""
    if not DB:
        return
    fd = os.open(MYCNF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"[client]\nhost={DB.get('host', '127.0.0.1')}\nport={DB.get('port', 3306)}\n"
                f"user={DB.get('user', '')}\npassword={DB.get('password', '')}\n")


def mysql(sql, timeout=4):
    if not DB:
        return ""
    return run(["mysql", f"--defaults-extra-file={MYCNF}", "-N", "-e", sql], timeout=timeout)

# Contadores que convertimos a rate/sec (nombre stat -> key corta)
RATE_MAP = {
    "core:rcv_requests": "total_req",
    "core:rcv_requests_register": "register",
    "core:rcv_requests_invite": "invite",
    "core:rcv_requests_ack": "ack",
    "core:rcv_requests_bye": "bye",
    "core:rcv_requests_cancel": "cancel",
    "core:rcv_requests_options": "options",
    "core:rcv_requests_prack": "prack",
    "core:rcv_requests_update": "update",
    "core:rcv_requests_info": "info",
    "core:rcv_replies": "total_rpl",
    "core:rcv_replies_2xx": "rpl_2xx",
    "core:rcv_replies_4xx": "rpl_4xx",
    "core:rcv_replies_5xx": "rpl_5xx",
    "core:rcv_replies_6xx": "rpl_6xx",
    "core:drop_requests": "drop_req",
    "sl:xxx": "sl_total",
    "sl:2xx": "sl_2xx",
    "sl:4xx": "sl_4xx",
    "sl:5xx": "sl_5xx",
    "dialog:processed_dialogs": "dlg_processed",
    "dialog:failed_dialogs": "dlg_failed",
    "sl:401": "auth_401",
    "sl:407": "auth_407",
    "sl:403": "auth_403",
    "core:rcv_replies_2xx_invite": "inv_2xx",
    "core:rcv_replies_3xx_invite": "inv_3xx",
    "core:rcv_replies_4xx_invite": "inv_4xx",
    "core:rcv_replies_5xx_invite": "inv_5xx",
    "core:rcv_replies_6xx_invite": "inv_6xx",
    "registrar:accepted_regs": "reg_ok",
    "registrar:rejected_regs": "reg_rej",
    "tcp:con_reset": "tcp_reset",
    "tcp:con_timeout": "tcp_timeout",
}

# KPIs de servicio (estilo carrier) calculados sobre ventana deslizante
KPI_WINDOW = 300          # s, ventana para ASR/NER/CPS/ACD/RSR
WIN_KEYS = ("dialog:processed_dialogs", "dialog:failed_dialogs",
            "core:rcv_replies_5xx_invite", "registrar:accepted_regs",
            "registrar:rejected_regs")

# columnas nuevas del historico (ALTER TABLE si faltan)
EXTRA_COLS = [("early", "INTEGER"), ("cps", "REAL"), ("asr", "REAL"), ("ner", "REAL"),
              ("acd", "REAL"), ("reg_ok_pct", "REAL"), ("inv_2xx_ps", "REAL"),
              ("inv_4xx_ps", "REAL"), ("inv_5xx_ps", "REAL"), ("auth_401_ps", "REAL"),
              ("auth_403_ps", "REAL"), ("drop_ps", "REAL"), ("rtp_sessions", "INTEGER"),
              ("health_ok", "INTEGER"), ("pdd_out", "REAL"), ("pdd_out_p95", "REAL"),
              ("pdd_in", "REAL"), ("mos_act_avg", "REAL"), ("mos_act_min", "REAL"),
              ("mos_done_avg", "REAL"), ("mos_bad_pct", "REAL")]
# columnas nuevas de call_quality: partes de la llamada vistas por la sonda SIP
CALLQ_EXTRA = [("src_user", "TEXT"), ("dst_user", "TEXT"), ("sub", "TEXT"),
               ("ua", "TEXT"), ("dir", "TEXT")]

state = {
    "last": None,          # snapshot anterior (para rates)
    "snap": None,          # snapshot actual (dict)
    "series": deque(maxlen=1800),  # ultimos ~60min en memoria (cada 2s)
    "lock": threading.Lock(),
    "reg_contacts": 0,
    "reg_contacts_ts": 0,
    "win": deque(maxlen=900),      # (ts, {WIN_KEYS}, calls, registered) ~30 min
    "peaks": {}, "peaks_ts": 0,
}
_hist_last_write = 0.0
_rtp_last = 0.0
_rtp_cache: dict = {}


# ---------- helpers ----------
def run(cmd, timeout=4):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""


def http_get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def parse_stat_lines(txt):
    """'core:rcv_requests = 123' o '200: 56337' -> dict."""
    d = {}
    for line in txt.splitlines():
        m = re.match(r"\s*([\w:\.]+)\s*[:=]\s*(-?\d+)", line)
        if m:
            try:
                d[m.group(1)] = int(m.group(2))
            except ValueError:
                pass
    return d


def parse_metrics_prom(txt):
    """kamailio_core_rcv_requests_register 123 ... -> dict core:rcv_requests_register."""
    d = {}
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"kamailio_(\w+?)_(.+?)\s+(-?\d+)", line)
        if m:
            grp, name, val = m.group(1), m.group(2), int(m.group(3))
            d[f"{grp}:{name}"] = val
    return d


SIP_DOMAIN = CFG["sip_domain"] or "localhost"


def sip_options_ping(host, port, timeout=1.5):
    """Envia OPTIONS real (R-URI = dominio local) y mide RTT. Retorna (ok, ms)."""
    # Averiguar IP origen real hacia el target para Via + bind correctos
    try:
        tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tmp.connect((host, port))
        src_ip = tmp.getsockname()[0]
        tmp.close()
    except Exception:
        src_ip = "127.0.0.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.bind((src_ip, 0))
        src_port = s.getsockname()[1]
    except Exception:
        src_port = 58999
    callid = f"noc-{int(time.time()*1000)}-{port}@healthcheck"
    branch = f"z9hG4bKnoc{port}{int(time.time()*10)%100000}"
    # R-URI con dominio alias => kamailio responde 200 Keepalive (route OPTIONS)
    msg = (f"OPTIONS sip:{SIP_DOMAIN} SIP/2.0\r\n"
           f"Via: SIP/2.0/UDP {src_ip}:{src_port};branch={branch};rport\r\n"
           f"From: <sip:noc@{SIP_DOMAIN}>;tag=noc1\r\n"
           f"To: <sip:{SIP_DOMAIN}>\r\n"
           f"Call-ID: {callid}\r\n"
           f"CSeq: 1 OPTIONS\r\n"
           f"Max-Forwards: 1\r\n"
           f"Content-Length: 0\r\n\r\n")
    try:
        t0 = time.monotonic()
        s.sendto(msg.encode(), (host, port))
        data, _ = s.recvfrom(4096)
        ms = (time.monotonic() - t0) * 1000.0
        ok = data.startswith(b"SIP/2.0")
        return ok, round(ms, 1)
    except Exception:
        return False, None
    finally:
        s.close()


def dispatcher_list():
    out = run(["kamcmd", "dispatcher.list"], timeout=4)
    dests = []
    # Formato kamcmd: URI: ... FLAGS: .. (AP/IX etc). Parseo tolerante.
    cur, last_set = {}, None
    for line in out.splitlines():
        m = re.search(r"URI:\s*(\S+)", line)
        if m:
            if cur.get("uri"):
                dests.append(cur)
            cur = {"uri": m.group(1), "set": last_set, "flags": "", "raw": line.strip()}
            continue
        m2 = re.search(r"FLAGS:\s*(\S+)", line)
        if m2 and cur.get("uri"):
            cur["flags"] = m2.group(1)
            continue
        m3 = re.search(r"ID:\s*(\d+)", line)
        if m3:
            last_set = int(m3.group(1))
            if cur.get("uri") and cur.get("set") is None:
                cur["set"] = last_set
    if cur.get("uri"):
        dests.append(cur)
    # estado: A=active, I=inactive, P=probing... X=disabled
    for d in dests:
        f = d.get("flags", "")
        d["up"] = ("A" in f) and ("X" not in f and "I" not in f or f == "AP")
        # kamailio usa AP=active+probing, IP=inactive+probing
        if f.startswith("I"):
            d["up"] = False
        elif f.startswith("A"):
            d["up"] = True
    return dests


def db_check():
    if not DB:
        return True  # sin BD configurada (usrloc solo en memoria)
    return mysql("SELECT 1").strip() == "1"


def db_registered_count():
    if not DB:
        return None
    out = mysql(f"SELECT COUNT(*) FROM `{DB.get('name', 'kamailio')}`.location", timeout=6)
    try:
        return int(out.strip().split()[0])
    except Exception:
        return None


# ---------- auth: usuarios PBKDF2 + sesiones + anti-fuerza bruta ----------
def load_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(users, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
    return {"salt": salt.hex(), "hash": dk.hex(), "iter": PBKDF2_ITER}


def verify_password(password, rec):
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(rec["salt"]), rec.get("iter", PBKDF2_ITER))
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except Exception:
        return False


def ensure_bootstrap_user():
    """Si no hay usuarios, crea 'noc' con clave aleatoria y la guarda."""
    users = load_users()
    if users:
        return None
    pw = secrets.token_urlsafe(12)
    users["noc"] = hash_password(pw)
    save_users(users)
    cred = os.path.join(BASE, ".noc-initial-cred")  # por instancia; nunca sobrescribe
    try:
        fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"NOC_USER=noc\nNOC_PASS={pw}\n")
    except FileExistsError:
        cred = "(no guardada: ya existe " + cred + ")"
    except Exception:
        cred = "(no guardada)"
    print(f"[noc] usuario inicial: noc / {pw} -> {cred}", flush=True)
    return pw


def client_ip(handler):
    fwd = handler.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return handler.client_address[0]


def is_locked(ip):
    until = _locked.get(ip, 0)
    if until and time.time() < until:
        return True
    _locked.pop(ip, None)
    return False


def reg_fail(ip):
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    lst.append(now)
    _fails[ip] = lst
    if len(lst) >= MAX_FAILS:
        _locked[ip] = now + LOCK_SECS
        _fails.pop(ip, None)
        return True
    return False


def clear_fails(ip):
    _fails.pop(ip, None)
    _locked.pop(ip, None)


def create_session(user, ip):
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                    (tok, user, now, now, now + SESSION_TTL))
        con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        con.commit()
        con.close()
    except Exception:
        return None
    return tok


def get_session_user(tok):
    if not tok:
        return None
    now = int(time.time())
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        row = con.execute("SELECT user, expires FROM sessions WHERE token=?", (tok,)).fetchone()
        if row and row[1] > now:
            con.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now, tok))
            con.commit()
            con.close()
            return row[0]
        con.close()
    except Exception:
        pass
    return None


def destroy_session(tok):
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        con.commit()
        con.close()
    except Exception:
        pass


def parse_cookies(handler):
    out = {}
    for part in handler.headers.get("Cookie", "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---------- sqlite ----------
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS samples(
        ts INTEGER PRIMARY KEY,
        register_ps REAL, invite_ps REAL, total_ps REAL,
        bye_ps REAL, options_ps REAL, rpl_2xx_ps REAL,
        rpl_4xx_ps REAL, rpl_5xx_ps REAL,
        dialogs INTEGER, registered INTEGER,
        shm_pct REAL, load1 REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user TEXT,
        created INTEGER, last_seen INTEGER, expires INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS sys_samples(
        ts INTEGER PRIMARY KEY,
        cpu_pct REAL, mem_pct REAL, disk_pct REAL,
        net_rx_kbs REAL, net_tx_kbs REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS call_quality(
        id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, created INTEGER, ended INTEGER,
        dur INTEGER, mos REAL, mos_low REAL, jitter REAL, loss REAL, rtt REAL,
        remote TEXT, codec TEXT, pkts INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_callq_ended ON call_quality(ended)")
    con.execute("""CREATE TABLE IF NOT EXISTS alarms(
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, sev TEXT, msg TEXT,
        raised INTEGER, last_seen INTEGER, cleared INTEGER,
        ack_by TEXT, ack_ts INTEGER, notified INTEGER DEFAULT 0)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_alarms_raised ON alarms(raised)")
    con.execute("""CREATE TABLE IF NOT EXISTS kpi_5m(
        ts INTEGER PRIMARY KEY, attempts INTEGER, asr REAL, ner REAL, cps REAL)""")
    have = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
    for col, typ in EXTRA_COLS:
        if col not in have:
            con.execute(f"ALTER TABLE samples ADD COLUMN {col} {typ}")
    have = {r[1] for r in con.execute("PRAGMA table_info(call_quality)")}
    for col, typ in CALLQ_EXTRA:
        if col not in have:
            con.execute(f"ALTER TABLE call_quality ADD COLUMN {col} {typ}")
    if not con.execute("SELECT 1 FROM kpi_5m LIMIT 1").fetchone():
        # linea base inicial: agregar el historico de muestras a cubetas de 5 min
        con.execute("""INSERT OR IGNORE INTO kpi_5m
            SELECT (ts/300)*300, CAST(AVG(cps)*300 AS INTEGER), AVG(asr), AVG(ner), AVG(cps)
            FROM samples WHERE cps IS NOT NULL GROUP BY ts/300""")
    con.commit()
    con.close()


# ---------- sonda SIP pasiva (AF_PACKET + BPF en kernel) ----------
# Una sola captura alimenta varios KPIs sin tocar kamailio.cfg (solo UDP):
#  - PDD = INVITE inicial -> primer 180/183 (o 2xx sin ringback). Si el INVITE es
#    desafiado (401/407), se mide desde el INVITE con credenciales (nuevo CSeq).
#  - Respuesta final de cada INVITE (486/487/404/480/408/5xx...) y NER carrier.
#  - ACD exacto: 2xx -> BYE por Call-ID.
#  - Partes de la llamada (From / R-URI / abonado / User-Agent) para las tablas MOS.
#  - 403 enviados por el proxy: IP destino, usuario, metodo y UA del ofensor.
PDD_CORE_IPS = set(CFG["core_ips"])   # INVITE desde el core = entrante
PDD_BPF = CFG["pdd"].get("bpf") or "udp and (port 5060 or port 5070)"
PDD_TIMER_B = 32                # s sin respuesta alguna = INVITE sin respuesta
RING_MAX = 600                  # s timbrando sin respuesta final -> se descarta
META_MAX_AGE = 6 * 3600         # s, partes de llamada sin BYE
pdd = {"res": deque(maxlen=20000), "noresp": deque(maxlen=5000), "pend": {},
       "final": deque(maxlen=50000), "acd": deque(maxlen=20000),
       "status": "init", "error": "", "pkts": 0, "since": None}
sip_meta = {}                   # call-id -> partes de la llamada
sec = {"r403": deque(maxlen=200000), "ua": {}}  # (ts, ip, user, metodo); ip ofensora -> UA
_PDD_HDR = re.compile(r"^(call-id|i|cseq|to|t|from|f|user-agent|server)\s*:\s*(.*)$", re.I | re.M)
_UA_HDR = re.compile(rb"^user-agent\s*:\s*([^\r\n]*)", re.I | re.M)
_URI_USER = re.compile(r"sips?:([^@;>\s]+)@", re.I)


def _local_ips():
    out = run(["ip", "-o", "-4", "addr", "show"], timeout=3)
    return {m.split("/")[0] for m in re.findall(r"inet (\S+)", out)}


LOCAL_IPS = _local_ips()


def _pdd_hdrs(payload):
    h = {}
    for k, v in _PDD_HDR.findall(payload.split(b"\r\n\r\n", 1)[0].decode("latin-1")):
        k = {"i": "call-id", "t": "to", "f": "from"}.get(k.lower(), k.lower())
        h.setdefault(k, v.strip())
    return h


def _uri_user(s):
    m = _URI_USER.search(s or "")
    return m.group(1)[:64] if m else ""


def _pdd_socket():
    out = subprocess.run(["tcpdump", "-ddd", PDD_BPF], capture_output=True,
                         text=True, timeout=5).stdout.split("\n")
    n = int(out[0])
    code = b"".join(struct.pack("HBBI", *map(int, l.split())) for l in out[1:1 + n])
    buf = ctypes.create_string_buffer(code)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))  # ETH_P_ALL
    s.setsockopt(socket.SOL_SOCKET, 26, struct.pack("HL", n, ctypes.addressof(buf)))  # SO_ATTACH_FILTER
    s.settimeout(1.0)
    return s, buf


def pdd_loop():
    if not CFG["pdd"].get("enabled", True):
        pdd["status"] = "disabled"
        return
    while True:
        try:
            sock, _buf = _pdd_socket()
            pdd["status"], pdd["error"] = "running", ""
            pdd["since"] = pdd["since"] or time.time()
            _pdd_capture(sock)
        except Exception as e:
            pdd["status"], pdd["error"] = "error", str(e)[:200]
            time.sleep(10)


def _pdd_gc(now):
    pend = pdd["pend"]
    for cid, p in list(pend.items()):
        age = now - p["t0"]
        if p["done"]:
            if now - p["tdone"] > PDD_TIMER_B:  # se conserva para ignorar retransmisiones
                del pend[cid]
        elif not p["resp"] and age > PDD_TIMER_B:
            del pend[cid]
            pdd["noresp"].append((now, p["dir"]))
            m = sip_meta.get(cid)
            if m:
                m["end"] = now
        elif (p["resp"] and not p["rang"] and age > PDD_TIMER_B) or age > RING_MAX:
            del pend[cid]  # 401/407 sin reintento, o timbrado eterno
    for cid, m in list(sip_meta.items()):
        if (m.get("end") and now - m["end"] > 180) or now - m["t0"] > META_MAX_AGE or \
                (not m.get("ans") and cid not in pend and now - m["t0"] > 120):
            del sip_meta[cid]
    if len(sec["ua"]) > 50000:  # solo ofensores con 403 en la ultima hora
        keep = {r[1] for r in list(sec["r403"]) if now - r[0] <= 3600}
        sec["ua"] = {ip: ua for ip, ua in sec["ua"].items() if ip in keep}


def _on_invite(pl, src, now):
    h = _pdd_hdrs(pl)
    cid, cseq = h.get("call-id"), h.get("cseq", "")
    if not cid or ";tag=" in h.get("to", "").replace(" ", ""):
        return  # re-INVITE dentro de dialogo
    pend = pdd["pend"]
    p = pend.get(cid)
    if p is None:
        d = "in" if src in PDD_CORE_IPS else "out"
        pend[cid] = {"t0": now, "cseq": cseq, "dir": d, "done": False, "resp": False,
                     "rang": False, "tdone": 0}
        if cid not in sip_meta:
            frm = _uri_user(h.get("from"))
            dst = _uri_user(pl[:pl.find(b"\r\n")].decode("latin-1")) or _uri_user(h.get("to"))
            sip_meta[cid] = {"t0": now, "dir": d, "from": frm, "to": dst,
                             "sub": frm if d == "out" else dst,
                             "ua": (h.get("user-agent") or "")[:80] if d == "out" else "",
                             "ip": src if d == "out" else "", "ans": None, "end": None}
    elif p["cseq"] != cseq and not p["done"]:
        p["t0"], p["cseq"], p["resp"] = now, cseq, False  # reintento con credenciales


def _on_reply(pl, src, dst, now):
    code = pl[8:11]
    if code == b"100":
        return
    if code == b"403" and src in LOCAL_IPS:
        h = _pdd_hdrs(pl)
        meth = h.get("cseq", "").split()
        sec["r403"].append((now, dst, _uri_user(h.get("from")), meth[-1] if meth else "?"))
        sec["ua"].setdefault(dst, "")
    if b"INVITE" not in pl[:2000]:
        return
    h = _pdd_hdrs(pl)
    cid = h.get("call-id")
    p = pdd["pend"].get(cid)
    if not p or p["done"] or h.get("cseq") != p["cseq"]:
        return
    try:
        c = int(code)
    except ValueError:
        return
    p["resp"] = True
    m = sip_meta.get(cid)
    if c in (180, 183) or 200 <= c < 300:
        if not p["rang"]:
            p["rang"] = True
            pdd["res"].append((now, now - p["t0"], p["dir"]))
        if m and m["dir"] == "in" and not m["ua"] and src not in LOCAL_IPS and src not in PDD_CORE_IPS:
            m["ua"] = (h.get("user-agent") or h.get("server") or "")[:80]  # ONT / softphone
            m["ip"] = src
    if c < 200 or c in (401, 407):
        return
    p["done"], p["tdone"] = True, now
    pdd["final"].append((now, c, p["dir"], p["rang"]))
    if m:
        if c < 300:
            m["ans"] = now
        else:
            m["end"] = now


def _on_bye(pl, now):
    m = sip_meta.get(_pdd_hdrs(pl).get("call-id"))
    if m and m["ans"] and not m["end"]:
        m["end"] = now
        pdd["acd"].append((now, now - m["ans"], m["dir"]))


def _pdd_capture(sock):
    last_gc = time.time()
    while True:
        try:
            fr = sock.recv(65535)
        except socket.timeout:
            fr = b""
        now = time.time()
        if now - last_gc > 5:
            last_gc = now
            _pdd_gc(now)
        if len(fr) < 42 or fr[12:14] != b"\x08\x00":
            continue
        pdd["pkts"] += 1
        ihl = (fr[14] & 15) * 4
        pl = fr[14 + ihl + 8:]
        src = socket.inet_ntoa(fr[26:30])
        if pl.startswith(b"SIP/2.0 "):
            _on_reply(pl, src, socket.inet_ntoa(fr[30:34]), now)
            continue
        if pl.startswith(b"INVITE "):
            _on_invite(pl, src, now)
        elif pl.startswith(b"BYE "):
            _on_bye(pl, now)
        if src in sec["ua"]:  # UA del ofensor (solo IPs que ya recibieron 403)
            mu = _UA_HDR.search(pl.split(b"\r\n\r\n", 1)[0])
            if mu:
                sec["ua"][src] = mu.group(1).decode("latin-1").strip()[:80]


def _summ(v):
    if not v:
        return {"n": 0, "avg": None, "p50": None, "p95": None, "max": None}
    v = sorted(v)
    pct = lambda q: v[min(len(v) - 1, int(round(q * (len(v) - 1))))]
    return {"n": len(v), "avg": round(sum(v) / len(v), 3), "p50": round(pct(.5), 3),
            "p95": round(pct(.95), 3), "max": round(v[-1], 3)}


def pdd_stats(now, window=KPI_WINDOW):
    res = [r for r in list(pdd["res"]) if now - r[0] <= window]
    nr = [r for r in list(pdd["noresp"]) if now - r[0] <= window]
    return {"status": pdd["status"], "error": pdd["error"], "pending": len(pdd["pend"]),
            "out": _summ([r[1] for r in res if r[2] == "out"]),
            "in": _summ([r[1] for r in res if r[2] == "in"]),
            "noresp_out": sum(1 for r in nr if r[1] == "out"),
            "noresp_in": sum(1 for r in nr if r[1] == "in")}


# Desglose de respuestas finales a INVITE (una por llamada, ultimo CSeq)
OUTCOME_ORDER = ["2xx", "486", "487", "480", "404", "408", "403", "603", "4xx otros",
                 "3xx", "5xx", "6xx otros", "sin respuesta"]


def _outcome_cat(c):
    if 200 <= c < 300:
        return "2xx"
    if str(c) in OUTCOME_ORDER:
        return str(c)
    return {3: "3xx", 4: "4xx otros", 5: "5xx", 6: "6xx otros"}.get(c // 100, "4xx otros")


def outcome_stats(now, window=KPI_WINDOW):
    """Categorias de respuesta final + NER carrier (5xx, 408 sin timbrar, sin respuesta)."""
    fin = [r for r in list(pdd["final"]) if now - r[0] <= window]
    nr = [r for r in list(pdd["noresp"]) if now - r[0] <= window]
    cats = {k: [0, 0] for k in OUTCOME_ORDER}
    net = {"5xx": 0, "408": 0, "noresp": len(nr)}
    for _, c, d, rang in fin:
        cats[_outcome_cat(c)][d == "in"] += 1
        if 500 <= c < 600:
            net["5xx"] += 1
        elif c == 408 and not rang:  # 408 tras timbrar = no contesta (causa de usuario)
            net["408"] += 1
    for _, d in nr:
        cats["sin respuesta"][d == "in"] += 1
    total = len(fin) + len(nr)
    nf = sum(net.values())
    return {"window_s": window, "total": total, "net_fail": net,
            "ner": round(100.0 * (1 - nf / total), 2) if total else None,
            "cats": [{"k": k, "out": v[0], "in": v[1], "n": v[0] + v[1],
                      "pct": round(100.0 * (v[0] + v[1]) / total, 1) if total else 0}
                     for k, v in cats.items() if v[0] + v[1]]}


def acd_stats(now, window=900):
    """ACD exacto: duracion 2xx->BYE de las llamadas terminadas en la ventana."""
    v = [r[1] for r in list(pdd["acd"]) if now - r[0] <= window]
    s = _summ(v)
    s["window_s"] = window
    return s


# Seguridad: top de 403 enviados por el proxy + IPs baneadas (htable ipban / fail2ban)
_ban = {"ts": 0, "ipban": set(), "f2b": set(), "jails": []}
_sec_cache = {"ts": 0, "out": None}


def banned_ips(now):
    if now - _ban["ts"] < 30:
        return _ban
    _ban["ts"] = now
    _ban["ipban"] = set(re.findall(r"name:\s*(\S+)", run(["kamcmd", "htable.dump", "ipban"], timeout=4)))
    jails = re.search(r"Jail list:\s*(.*)", run(["fail2ban-client", "status"], timeout=4))
    _ban["jails"] = [j.strip() for j in jails.group(1).split(",") if j.strip()] if jails else []
    f2b = set()
    for j in _ban["jails"]:
        m = re.search(r"Banned IP list:\s*(.*)", run(["fail2ban-client", "status", j], timeout=4))
        if m:
            f2b.update(m.group(1).split())
    _ban["f2b"] = f2b
    return _ban


def security_stats(now, window=900, top=10):
    if _sec_cache["out"] and now - _sec_cache["ts"] < 10:
        return _sec_cache["out"]
    rows = [r for r in list(sec["r403"]) if now - r[0] <= window]
    ban = banned_ips(now)
    uas = sec["ua"]
    ips, users, agents, meths = Counter(), Counter(), Counter(), Counter()
    ip_user, ip_meth = {}, {}
    for _, ip, user, meth in rows:
        ips[ip] += 1
        meths[meth] += 1
        agents[uas.get(ip) or "(sin UA)"] += 1
        if user:
            users[user] += 1
            ip_user.setdefault(ip, Counter())[user] += 1
        ip_meth.setdefault(ip, Counter())[meth] += 1

    def bans(ip):
        return [n for n, s in (("ipban", ban["ipban"]), ("fail2ban", ban["f2b"])) if ip in s]
    n = len(rows)
    span = min(window, now - (pdd["since"] or now)) or 1  # recien arrancada: ventana parcial
    out = {"window_s": window, "span_s": int(span), "total": n, "rate": round(n / span, 2),
           "unique_ips": len(ips), "methods": dict(meths.most_common()),
           "ipban_n": len(ban["ipban"]), "f2b_n": len(ban["f2b"]), "f2b_jails": ban["jails"],
           "top_ips": [{"ip": ip, "n": c, "pct": round(100.0 * c / n, 1), "ua": uas.get(ip, ""),
                        "user": ip_user[ip].most_common(1)[0][0] if ip in ip_user else "",
                        "users": len(ip_user.get(ip, ())), "meth": ip_meth[ip].most_common(1)[0][0],
                        "ban": bans(ip)} for ip, c in ips.most_common(top)],
           "top_uas": [{"ua": ua, "n": c, "pct": round(100.0 * c / n, 1)} for ua, c in agents.most_common(top)],
           "top_users": [{"user": u, "n": c, "pct": round(100.0 * c / n, 1)} for u, c in users.most_common(top)]}
    _sec_cache.update(ts=now, out=out)
    return out


# ---------- MOS por llamada: RTPEngine NG (bencode/UDP) ----------
# list + query por llamada cada MOS_POLL s. MOS/jitter/perdida/RTT vienen de RTCP
# (sin RTCP no hay MOS). Al desaparecer la llamada se guarda en call_quality.
_ng = (CFG["rtpengine"].get("ng") or "").rsplit(":", 1)
RTPENGINE_NG = (_ng[0].replace("localhost", "127.0.0.1"), int(_ng[1])) if len(_ng) == 2 else None
RTP_SERVICE = CFG["rtpengine"].get("service") or ""
RTP_KTABLE = f"/proc/rtpengine/{CFG['rtpengine'].get('kernel_table', 0)}"
MOS_POLL = 10
MOS_BAD = float(CFG["mos_bad"])  # ITU-T G.107: <3.6 "muchos usuarios insatisfechos"
CALLQ_RETENTION_D = 7
mos = {"active": {}, "done": deque(maxlen=50000), "status": "init", "error": ""}


def _benc(o):
    if isinstance(o, int):
        return b"i%de" % o
    if isinstance(o, str):
        o = o.encode()
    if isinstance(o, bytes):
        return b"%d:%s" % (len(o), o)
    if isinstance(o, list):
        return b"l" + b"".join(map(_benc, o)) + b"e"
    return b"d" + b"".join(_benc(k) + _benc(v) for k, v in sorted(o.items())) + b"e"


def _bdec(b, i=0):
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1:j]), j + 1
    if c in (b"l", b"d"):
        i += 1
        out = [] if c == b"l" else {}
        while b[i:i + 1] != b"e":
            v, i = _bdec(b, i)
            if c == b"l":
                out.append(v)
            else:
                val, i = _bdec(b, i)
                out[v] = val
        return out, i + 1
    j = b.index(b":", i)
    n = int(b[i:j])
    return b[j + 1:j + 1 + n].decode("latin-1"), j + 1 + n


def rtpengine_ng(cmd, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        ck = secrets.token_hex(6).encode()
        s.sendto(ck + b" " + _benc(cmd), RTPENGINE_NG)
        while True:
            d = s.recv(1 << 20)
            if d.startswith(ck + b" "):
                return _bdec(d[len(ck) + 1:])[0]
    finally:
        s.close()


def _call_metrics(cid, q, now):
    """Peor pierna de la llamada: MOS min, jitter/perdida/RTT max."""
    m = {"call_id": cid, "created": q.get("created"), "mos": None, "mos_low": None,
         "jitter": None, "loss": None, "rtt": None, "remote": "", "codec": "",
         "pkts": (q.get("totals") or {}).get("RTP", {}).get("packets", 0), "seen": now}
    for v in (q.get("SSRC") or {}).values():
        if not isinstance(v, dict):
            continue
        a, lo = v.get("average MOS") or {}, v.get("lowest MOS") or {}
        if not a.get("MOS"):
            continue
        mv = a["MOS"] / 10.0
        m["mos"] = mv if m["mos"] is None else min(m["mos"], mv)
        if lo.get("MOS"):
            lv = lo["MOS"] / 10.0
            m["mos_low"] = lv if m["mos_low"] is None else min(m["mos_low"], lv)
        for key, src, f in (("jitter", "jitter", 1), ("loss", "packet loss", 1), ("rtt", "round-trip time", 1000)):
            val = a.get(src)
            if isinstance(val, int):
                m[key] = max(m[key] or 0, round(val / f, 1))
    for tg in (q.get("tags") or {}).values():
        for md in tg.get("medias") or []:
            m["codec"] = m["codec"] or md.get("codec", "")
            for st in md.get("streams") or []:
                addr = (st.get("endpoint") or {}).get("address", "")
                if addr and addr not in PDD_CORE_IPS and not m["remote"]:
                    m["remote"] = addr
    return m


def mos_loop():
    if not RTPENGINE_NG:
        mos["status"] = "disabled"
        return
    while True:
        try:
            now = time.time()
            calls = rtpengine_ng({"command": "list", "limit": "5000"}).get("calls") or []
            act = mos["active"]
            for cid in calls:
                try:
                    act[cid] = _call_metrics(cid, rtpengine_ng({"command": "query", "call-id": cid}), now)
                except Exception:
                    pass
            live = set(calls)
            ended = [c for c in act if c not in live]
            if ended:
                rows = []
                for cid in ended:
                    m = act.pop(cid)
                    if not m["pkts"]:
                        continue  # sin media: llamada no establecida
                    m["end"] = int(m["seen"])
                    m["dur"] = int(m["seen"] - (m["created"] or m["seen"]))
                    m.update(_parties(cid))
                    mos["done"].append(m)
                    rows.append((cid[:120], m["created"], m["end"], m["dur"], m["mos"], m["mos_low"],
                                 m["jitter"], m["loss"], m["rtt"], m["remote"], m["codec"], m["pkts"],
                                 m["src_user"], m["dst_user"], m["sub"], m["ua"], m["dir"]))
                db_callq_insert(rows)
            mos["status"], mos["error"] = "running", ""
        except Exception as e:
            mos["status"], mos["error"] = "error", str(e)[:200]
        time.sleep(MOS_POLL)


def _parties(cid):
    """Origen/destino/abonado/UA de la llamada segun la sonda SIP ('' si no se vio el INVITE)."""
    m = sip_meta.get(cid) or {}
    return {"src_user": m.get("from", ""), "dst_user": m.get("to", ""), "sub": m.get("sub", ""),
            "ua": m.get("ua", ""), "dir": m.get("dir", "")}


def db_callq_insert(rows):
    if not rows:
        return
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.executemany("INSERT INTO call_quality(call_id,created,ended,dur,mos,mos_low,jitter,"
                        "loss,rtt,remote,codec,pkts,src_user,dst_user,sub,ua,dir) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("DELETE FROM call_quality WHERE ended < ?",
                    (int(time.time()) - CALLQ_RETENTION_D * 86400,))
        con.commit()
        con.close()
    except Exception:
        pass


def db_calls(minutes, only_bad, order, limit=200):
    minutes = max(1, min(CALLQ_RETENTION_D * 1440, minutes))
    cols = ["call_id", "created", "ended", "dur", "mos", "mos_low", "jitter", "loss", "rtt",
            "remote", "codec", "pkts", "src_user", "dst_user", "sub", "ua", "dir"]
    q = f"SELECT {','.join(cols)} FROM call_quality WHERE ended >= ?"
    args = [int(time.time()) - minutes * 60]
    if only_bad:
        q += " AND mos IS NOT NULL AND mos < ?"
        args.append(MOS_BAD)
    q += " ORDER BY " + ("mos IS NULL, mos ASC" if order == "worst" else "ended DESC") + " LIMIT ?"
    args.append(limit)
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute(q, args).fetchall()
        tot = con.execute("SELECT COUNT(*), COUNT(mos), AVG(mos), SUM(mos < ?) FROM call_quality "
                          "WHERE ended >= ?", (MOS_BAD, args[0])).fetchone()
        con.close()
    except Exception:
        rows, tot = [], (0, 0, None, 0)
    return {"cols": cols, "rows": rows, "total": tot[0], "with_mos": tot[1],
            "avg_mos": round(tot[2], 2) if tot[2] else None, "bad": tot[3] or 0, "bad_threshold": MOS_BAD}


def mos_stats(now, window=900):
    act = list(mos["active"].values())
    am = [c["mos"] for c in act if c["mos"] is not None]
    done = [c for c in list(mos["done"]) if now - c["end"] <= window]
    dm = [c["mos"] for c in done if c["mos"] is not None]
    worst = sorted((c for c in act if c["mos"] is not None), key=lambda c: c["mos"])[:8]
    # MOS satura en 4.3 con G.711: la distribucion de jitter/perdida/RTT dice mas
    dist = {k: _summ([c[k] for c in done if c[k] is not None]) for k in ("jitter", "loss", "rtt")}
    return {"status": mos["status"], "error": mos["error"], "bad_threshold": MOS_BAD,
            "active": {"n": len(act), "n_mos": len(am), "avg": round(sum(am) / len(am), 2) if am else None,
                       "min": min(am) if am else None, "bad": sum(1 for v in am if v < MOS_BAD),
                       "rtcp_pct": round(100.0 * len(am) / len(act), 1) if act else None},
            "done": {"n": len(done), "n_mos": len(dm), "avg": round(sum(dm) / len(dm), 2) if dm else None,
                     "bad": sum(1 for v in dm if v < MOS_BAD),
                     "bad_pct": round(100.0 * sum(1 for v in dm if v < MOS_BAD) / len(dm), 1) if dm else None,
                     "rtcp_pct": round(100.0 * len(dm) / len(done), 1) if done else None,
                     "jitter": dist["jitter"], "loss": dist["loss"], "rtt": dist["rtt"],
                     "window_s": window},
            "worst_active": [{"call_id": c["call_id"], "remote": c["remote"], "codec": c["codec"],
                              "dur": int(now - (c["created"] or now)), "mos": c["mos"], "mos_low": c["mos_low"],
                              "jitter": c["jitter"], "loss": c["loss"], "rtt": c["rtt"],
                              **_parties(c["call_id"])} for c in worst]}


HIST_COLS = ["ts", "register_ps", "invite_ps", "total_ps", "bye_ps", "options_ps",
             "rpl_2xx_ps", "rpl_4xx_ps", "rpl_5xx_ps", "dialogs", "registered",
             "shm_pct", "load1"] + [c for c, _ in EXTRA_COLS]


def db_insert(ts, rates, dialogs, registered, shm_pct, load1, kpi, early, rtp_sessions, health_ok, pd, ms):
    vals = [int(ts), rates.get("register", 0), rates.get("invite", 0),
            rates.get("total_req", 0), rates.get("bye", 0),
            rates.get("options", 0), rates.get("rpl_2xx", 0),
            rates.get("rpl_4xx", 0), rates.get("rpl_5xx", 0),
            int(dialogs or 0), int(registered or 0),
            float(shm_pct or 0), float(load1 or 0),
            int(early or 0), kpi.get("cps"), kpi.get("asr"), kpi.get("ner"),
            kpi.get("acd"), kpi.get("reg_ok_pct"), rates.get("inv_2xx", 0),
            rates.get("inv_4xx", 0), rates.get("inv_5xx", 0), rates.get("auth_401", 0),
            rates.get("auth_403", 0), rates.get("drop_req", 0), int(rtp_sessions or 0),
            1 if health_ok else 0, pd["out"]["avg"], pd["out"]["p95"], pd["in"]["avg"],
            ms["active"]["avg"], ms["active"]["min"], ms["done"]["avg"], ms["done"]["bad_pct"]]
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute(f"INSERT OR REPLACE INTO samples({','.join(HIST_COLS)}) "
                    f"VALUES({','.join('?' * len(HIST_COLS))})", vals)
        con.execute("DELETE FROM samples WHERE ts < ?", (int(time.time()) - RETENTION_H * 3600,))
        con.commit()
        con.close()
    except Exception:
        pass


def db_history(minutes):
    minutes = max(1, min(1440, minutes))
    since = int(time.time()) - minutes * 60
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute(f"SELECT {','.join(HIST_COLS)} FROM samples WHERE ts >= ? ORDER BY ts",
                           (since,)).fetchall()
        con.close()
    except Exception:
        rows = []
    # decimado a max ~600 puntos
    if len(rows) > 600:
        step = len(rows) // 600 + 1
        rows = rows[::step]
    return {"cols": HIST_COLS, "rows": rows}


def db_peaks():
    """Picos y disponibilidad de las ultimas 24h (cacheado 60 s)."""
    now = time.time()
    if now - state["peaks_ts"] < 60:
        return state["peaks"]
    out = {}
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        since = int(now) - 86400
        r = con.execute("SELECT MAX(dialogs), MAX(cps), MAX(registered), MIN(NULLIF(registered,0)) "
                        "FROM samples WHERE ts >= ?", (since,)).fetchone()
        out["calls_24h"], out["cps_24h"], out["reg_max_24h"], out["reg_min_24h"] = r
        r = con.execute("SELECT ts FROM samples WHERE ts >= ? AND dialogs = ? ORDER BY ts DESC LIMIT 1",
                        (since, out["calls_24h"] or 0)).fetchone()
        out["calls_24h_ts"] = r[0] if r else None
        # 24h reales del historico persistido (no se reinicia con kamailio ni con el NOC).
        # Huecos sin muestras (NOC caido) no cuentan como disponibles: se informan como cobertura.
        r = con.execute("SELECT COUNT(*), SUM(health_ok), MIN(ts) FROM samples "
                        "WHERE ts >= ? AND health_ok IS NOT NULL", (since,)).fetchone()
        out["avail_24h"] = round(100.0 * r[1] / r[0], 3) if r[0] else None
        out["avail_since"] = r[2]
        out["avail_cover_pct"] = round(min(100.0, 100.0 * r[0] * HIST_EVERY / 86400), 1) if r[0] else 0
        out["avail_down_s"] = int((r[0] - (r[1] or 0)) * HIST_EVERY) if r[0] else None
        con.close()
    except Exception:
        pass
    state["peaks"], state["peaks_ts"] = out, now
    return out


def window_kpis(now, stats, calls, registered):
    """ASR / NER / CPS / ACD / RSR sobre KPI_WINDOW segundos (contadores acumulados)."""
    win = state["win"]
    win.append((now, {k: stats.get(k, 0) for k in WIN_KEYS}, calls, registered))
    old = next((w for w in win if now - w[0] <= KPI_WINDOW), None)
    kpi = {"cps": None, "asr": None, "ner": None, "acd": None, "reg_ok_pct": None,
           "attempts": 0, "answered": 0, "window_s": 0, "reg_delta_10m": None,
           "reg_drop_pct": None, "calls_drop_pct": None}
    if old and now - old[0] >= 20:
        dt = now - old[0]
        d = {k: max(0, stats.get(k, 0) - old[1][k]) for k in WIN_KEYS}
        att = d["dialog:processed_dialogs"]
        ans = max(0, att - d["dialog:failed_dialogs"])
        kpi.update(window_s=int(dt), attempts=att, answered=ans,
                   cps=round(att / dt, 3))
        if att > 0:
            kpi["asr"] = round(100.0 * min(ans, att) / att, 1)
            # NER: excluye causas de usuario (busy/no answer/cancel/decline); solo 5xx = red
            kpi["ner"] = round(max(0.0, 100.0 * (1 - d["core:rcv_replies_5xx_invite"] / att)), 2)
        avg_calls = sum(w[2] for w in win if now - w[0] <= KPI_WINDOW) / \
            max(1, sum(1 for w in win if now - w[0] <= KPI_WINDOW))
        if ans > 0:
            kpi["acd"] = round(avg_calls / (ans / dt), 0)  # Ley de Little
        rt = d["registrar:accepted_regs"] + d["registrar:rejected_regs"]
        if rt > 0:
            kpi["reg_ok_pct"] = round(100.0 * d["registrar:accepted_regs"] / rt, 2)
    # caidas masivas: registrados vs maximo 10 min, llamadas vs maximo 2 min
    regs = [w[3] for w in win if now - w[0] <= 600 and w[3]]
    if regs and registered:
        ref = max(regs)
        kpi["reg_delta_10m"] = registered - regs[0]
        kpi["reg_drop_pct"] = round(100.0 * (ref - registered) / ref, 1) if ref else 0
    cl = [w[2] for w in win if now - w[0] <= 120]
    if cl and max(cl) >= 20:
        kpi["calls_drop_pct"] = round(100.0 * (max(cl) - calls) / max(cl), 1)
    return kpi


# ---------- linea base: misma hora de semanas (o dias) previos ----------
BASE_SIGMA = float(CFG.get("baseline_sigma", 3.0))
_kpi5 = {"bucket": 0}
_base = {"ts": 0, "out": None}


def kpi5m_record(now, kpi):
    """Al cerrar cada cubeta de 5 min guarda el KPI de la ventana (que cubre esa cubeta)."""
    b = int(now // 300) * 300
    if _kpi5["bucket"] == b:
        return
    first, _kpi5["bucket"] = not _kpi5["bucket"], b
    if first or kpi.get("window_s", 0) < 240:
        return
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute("INSERT OR REPLACE INTO kpi_5m VALUES(?,?,?,?,?)",
                    (b - 300, kpi.get("attempts"), kpi.get("asr"), kpi.get("ner"), kpi.get("cps")))
        con.execute("DELETE FROM kpi_5m WHERE ts < ?", (b - 35 * 86400,))
        con.commit()
        con.close()
    except Exception:
        pass


def _mean_sd(v):
    m = sum(v) / len(v)
    return m, (sum((x - m) ** 2 for x in v) / max(1, len(v) - 1)) ** 0.5


def kpi_baseline(now):
    """ASR/CPS esperados a esta hora: +-30 min de la misma hora en las 4 semanas previas;
    si aun no hay historia semanal, los 7 dias previos (mezcla dias habiles y fin de semana)."""
    if now - _base["ts"] < 60:
        return _base["out"]
    out = None
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        for mode, step, ks in (("semanal", 7 * 86400, range(1, 5)), ("diaria", 86400, range(1, 8))):
            rows = []
            for k in ks:
                c = now - k * step
                rows += con.execute("SELECT asr, cps, attempts FROM kpi_5m WHERE ts BETWEEN ? AND ?",
                                    (c - 1800, c + 1800)).fetchall()
            asr = [r[0] for r in rows if r[0] is not None and (r[2] or 0) >= 20]
            cps = [r[1] for r in rows if r[1] is not None]
            if len(asr) >= 6 and len(cps) >= 6:
                am, asd = _mean_sd(asr)
                cm, csd = _mean_sd(cps)
                # piso de sigma: evita alarmar por ruido cuando la historia es muy estable
                out = {"mode": mode, "n": len(asr), "asr": round(am, 1), "asr_sd": round(max(asd, 2.0), 1),
                       "cps": round(cm, 3), "cps_sd": round(max(csd, 0.1 * cm, 0.02), 3)}
                break
        con.close()
    except Exception:
        pass
    _base.update(ts=now, out=out)
    return out


# ---------- alarmas persistentes (servidor) + notificaciones ----------
SEV_RANK = {"minor": 1, "major": 2, "critical": 3}
ALARM_CLEAR_S = 60        # s ausente antes de limpiar (histeresis anti-aleteo)
ALARM_RESTORE_GRACE = 60  # s extra tras reiniciar el NOC antes de limpiar las restauradas
NOTIFY = CFG.get("notify") or {}
alarms = {"active": {}, "lock": threading.Lock(), "flush": 0}
_notify_q = queue.Queue(maxsize=500)


def alarm_key(msg):
    return re.sub(r"[\d.,]+", "#", msg)


def alarms_load():
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute("SELECT id,key,sev,msg,raised,ack_by,ack_ts,notified FROM alarms "
                           "WHERE cleared IS NULL").fetchall()
        con.close()
    except Exception:
        rows = []
    now = time.time()
    for r in rows:
        alarms["active"][r[1]] = {"id": r[0], "key": r[1], "sev": r[2], "msg": r[3], "raised": r[4],
                                  "ack_by": r[5], "ack_ts": r[6], "notified": r[7] or 0,
                                  "last_seen": now + ALARM_RESTORE_GRACE}


def _notify(kind, a):
    if not (NOTIFY.get("telegram_token") and NOTIFY.get("telegram_chat")) and not NOTIFY.get("webhook_url"):
        return
    try:
        _notify_q.put_nowait((kind, dict(a), int(time.time())))
    except queue.Full:
        pass


def notify_loop():
    sevtxt = {"critical": "CRITICA", "major": "MAYOR", "minor": "MENOR"}
    host = socket.gethostname()
    while True:
        kind, a, ts = _notify_q.get()
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        head = {"raise": "ALARMA", "escalate": "ALARMA ESCALADA", "clear": "LIMPIA"}[kind]
        text = (f"[{head}] {sevtxt.get(a['sev'], a['sev'])} - {CFG['site_name'] or SIP_DOMAIN} ({host})\n"
                f"{a['msg']}\nDesde {time.strftime('%H:%M:%S', time.localtime(a['raised']))}"
                + (f" - limpia {when}" if kind == "clear" else ""))
        try:
            if NOTIFY.get("telegram_token") and NOTIFY.get("telegram_chat"):
                body = json.dumps({"chat_id": NOTIFY["telegram_chat"], "text": text}).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{NOTIFY['telegram_token']}/sendMessage", data=body,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
            if NOTIFY.get("webhook_url"):
                body = json.dumps({"event": kind, "id": a["id"], "severity": a["sev"], "message": a["msg"],
                                   "raised": a["raised"], "ts": ts, "site": CFG["site_name"],
                                   "host": host, "text": text}).encode()
                req = urllib.request.Request(NOTIFY["webhook_url"], data=body,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"[noc] notificacion fallida: {e}", flush=True)


def alarms_update(alerts, now):
    """Sincroniza las alertas calculadas con la tabla alarms (alta, escalado, limpieza)."""
    min_rank = SEV_RANK.get(NOTIFY.get("min_sev", "major"), 2)
    delay = float(NOTIFY.get("delay_s", 30))
    act = alarms["active"]
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
    except Exception:
        return
    with alarms["lock"]:
        seen = set()
        for a in alerts:
            k = alarm_key(a["msg"])
            seen.add(k)
            r = act.get(k)
            if r is None:
                cur = con.execute("INSERT INTO alarms(key,sev,msg,raised,last_seen) VALUES(?,?,?,?,?)",
                                  (k, a["sev"], a["msg"], int(now), int(now)))
                r = act[k] = {"id": cur.lastrowid, "key": k, "sev": a["sev"], "msg": a["msg"],
                              "raised": int(now), "ack_by": None, "ack_ts": None, "notified": 0}
            elif SEV_RANK[a["sev"]] > SEV_RANK[r["sev"]]:
                r["sev"] = a["sev"]
                if r["notified"] and SEV_RANK[a["sev"]] >= min_rank:
                    _notify("escalate", {**r, "msg": a["msg"]})
                r["ack_by"] = r["ack_ts"] = None  # escalado: requiere nuevo reconocimiento
                con.execute("UPDATE alarms SET sev=?, ack_by=NULL, ack_ts=NULL WHERE id=?", (a["sev"], r["id"]))
            r["msg"], r["last_seen"] = a["msg"], max(now, r.get("last_seen", 0))
            if not r["notified"] and SEV_RANK[r["sev"]] >= min_rank and now - r["raised"] >= delay:
                r["notified"] = 1
                _notify("raise", r)
                con.execute("UPDATE alarms SET notified=1 WHERE id=?", (r["id"],))
        for k, r in list(act.items()):
            if k not in seen and now - r["last_seen"] >= ALARM_CLEAR_S:
                con.execute("UPDATE alarms SET cleared=?, last_seen=?, msg=? WHERE id=?",
                            (int(now), int(min(now, r["last_seen"])), r["msg"], r["id"]))
                del act[k]
                if r["notified"]:
                    _notify("clear", r)
        if now - alarms["flush"] >= 30:  # persistir ultimo mensaje/visto de las activas
            alarms["flush"] = now
            con.executemany("UPDATE alarms SET last_seen=?, msg=? WHERE id=?",
                            [(int(min(now, r["last_seen"])), r["msg"], r["id"]) for r in act.values()])
            con.execute("DELETE FROM alarms WHERE cleared IS NOT NULL AND cleared < ?",
                        (int(now) - 30 * 86400,))
        con.commit()
        con.close()
        order = {"critical": 0, "major": 1, "minor": 2}
        return sorted(({"id": r["id"], "sev": r["sev"], "msg": r["msg"], "raised": r["raised"],
                        "ack_by": r["ack_by"], "ack_ts": r["ack_ts"], "present": k in seen}
                       for k, r in act.items()), key=lambda r: (order[r["sev"]], r["raised"]))


def alarm_ack(aid, user):
    with alarms["lock"]:
        r = next((r for r in alarms["active"].values() if r["id"] == aid), None)
        if not r:
            return False
        r["ack_by"], r["ack_ts"] = user, int(time.time())
        try:
            con = sqlite3.connect(DB_PATH, timeout=5)
            con.execute("UPDATE alarms SET ack_by=?, ack_ts=? WHERE id=?", (user, r["ack_ts"], aid))
            con.commit()
            con.close()
        except Exception:
            return False
    return True


def db_alarm_history(hours):
    hours = max(1, min(24 * 30, hours))
    since = int(time.time()) - hours * 3600
    cols = ["id", "sev", "msg", "raised", "cleared", "ack_by", "ack_ts"]
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute(f"SELECT {','.join(cols)} FROM alarms WHERE raised >= ? OR cleared >= ? "
                           "OR cleared IS NULL ORDER BY raised DESC LIMIT 500", (since, since)).fetchall()
        con.close()
    except Exception:
        rows = []
    return {"cols": cols, "rows": rows}


# ---------- alta disponibilidad: DMQ + VIP ----------
HA_VIP = CFG["ha"].get("vip") or ""
KEEPALIVED = CFG["ha"].get("keepalived_service") or "keepalived"


def collect_ha():
    ha = {"vip": HA_VIP, "vip_local": False, "keepalived": "unknown", "peers": []}
    out = run(["kamcmd", "dmq.list_nodes"], timeout=4)
    cur = {}
    for raw in out.splitlines():
        line = raw.strip().strip("{},")
        if not line or ":" not in line:
            if cur:
                if "host" in cur:
                    ha["peers"].append({"host": cur.get("host"), "status": cur.get("status", "?"),
                                        "local": cur.get("local") == "1"})
                cur = {}
            continue
        k, v = line.split(":", 1)
        cur[k.strip()] = v.strip()
    if cur and "host" in cur:
        ha["peers"].append({"host": cur.get("host"), "status": cur.get("status", "?"),
                            "local": cur.get("local") == "1"})
    if HA_VIP:
        ha["vip_local"] = f" {HA_VIP}/" in run(["ip", "-o", "addr", "show"], timeout=3)
        ha["keepalived"] = run(["systemctl", "is-active", KEEPALIVED], timeout=3).strip() or "unknown"
    else:
        ha["keepalived"] = "n/a"
    return ha


# ---------- hardware del servidor ----------
def read_cpu():
    """(total, idle) agregados desde /proc/stat."""
    try:
        p = open("/proc/stat").readline().split()
        vals = list(map(int, p[1:8]))
        return sum(vals), vals[3] + vals[4]
    except Exception:
        return None, None


def read_net():
    """{iface: (rx_bytes, tx_bytes)} sin loopback."""
    out = {}
    try:
        for line in open("/proc/net/dev").read().splitlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo" or iface.startswith(("veth", "docker", "br-")):
                continue
            f = rest.split()
            out[iface] = (int(f[0]), int(f[8]))
    except Exception:
        pass
    return out


def read_temps():
    temps = []
    try:
        base = "/sys/class/thermal"
        for z in sorted(os.listdir(base)):
            if not z.startswith("thermal_zone"):
                continue
            try:
                t = int(open(os.path.join(base, z, "temp")).read().strip()) / 1000.0
                typ = open(os.path.join(base, z, "type")).read().strip()
                temps.append({"zone": z, "type": typ, "c": round(t, 1)})
            except Exception:
                pass
    except Exception:
        pass
    return temps


_kmem = {"ts": 0, "out": {}}


def read_kam_mem(now):
    """Memoria real de kamailio. RSS suma la SHM mapeada en cada proceso (x144), por eso
    se usa PSS (smaps_rollup) separando la parte compartida; PKG = pkg.stats real_used."""
    if now - _kmem["ts"] < 30:
        return _kmem["out"]
    n, pss, pss_shm = 0, 0, 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if open(f"/proc/{pid}/comm").read().strip() != "kamailio":
                continue
            n += 1
            for line in open(f"/proc/{pid}/smaps_rollup"):
                if line.startswith("Pss:"):
                    pss += int(line.split()[1])
                elif line.startswith("Pss_Shmem:"):
                    pss_shm += int(line.split()[1])
        except Exception:
            pass
    pkg_used = sum(int(v) for v in re.findall(r"real_used:\s*(\d+)", run(["kamcmd", "pkg.stats"], timeout=5)))
    out = {"procs": n, "pss_mb": round(pss / 1024, 1), "pss_shm_mb": round(pss_shm / 1024, 1),
           "pss_priv_mb": round((pss - pss_shm) / 1024, 1), "pkg_used_mb": round(pkg_used / 1048576, 1)}
    _kmem.update(ts=now, out=out)
    return out


def collect_system():
    """Foto de hardware + rates CPU/red calculados con deltas."""
    global _sys_prev
    now = time.time()
    sys = {"cpu_pct": 0.0, "load": [0, 0, 0], "mem": {}, "swap_pct": 0,
           "disks": [], "disk_max_pct": 0, "net": [], "net_rx": 0, "net_tx": 0,
           "uptime_s": 0, "temps": [], "temp_max": None, "kam_procs": 0, "kam_mem": {}}
    try:
        sys["load"] = list(map(float, open("/proc/loadavg").read().split()[:3]))
    except Exception:
        pass
    try:
        sys["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        pass
    # CPU %
    tot, idle = read_cpu()
    prev = _sys_prev["cpu"]
    if tot is not None and prev:
        dt, di = tot - prev[0], idle - prev[1]
        if dt > 0:
            sys["cpu_pct"] = round(max(0.0, min(100.0, 100.0 * (1 - di / dt))), 1)
    if tot is not None:
        _sys_prev["cpu"] = (tot, idle)
    # MEM / SWAP
    try:
        mi = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")[0], line.split()[1]
            mi[k] = int(v)
        mt, ma = mi.get("MemTotal", 1), mi.get("MemAvailable", 0)
        sys["mem"] = {"total_mb": mt // 1024, "avail_mb": ma // 1024,
                      "used_mb": (mt - ma) // 1024,
                      "pct": round(100.0 * (mt - ma) / mt, 1)}
        st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        sys["swap_pct"] = round(100.0 * (st - sf) / st, 1) if st else 0
    except Exception:
        pass
    # DISCOS físicos
    try:
        seen = set()
        for line in open("/proc/mounts"):
            parts = line.split()
            if len(parts) < 3 or parts[2] not in ("ext4", "xfs", "btrfs"):
                continue
            mp = parts[1]
            if mp in seen:
                continue
            seen.add(mp)
            try:
                st = os.statvfs(mp)
                pct = round(100.0 * (1 - st.f_bavail / st.f_blocks), 1)
                sys["disks"].append({"mount": mp,
                                     "total_gb": round(st.f_blocks * st.f_frsize / 2**30, 1),
                                     "free_gb": round(st.f_bavail * st.f_frsize / 2**30, 1),
                                     "pct": pct})
            except Exception:
                pass
        if sys["disks"]:
            sys["disk_max_pct"] = max(d["pct"] for d in sys["disks"])
    except Exception:
        pass
    # RED (rates KB/s)
    cur = read_net()
    for iface, (rx, tx) in cur.items():
        r, t = 0.0, 0.0
        if iface in _sys_prev["net"]:
            prx, ptx, pts = _sys_prev["net"][iface]
            dt = max(0.5, now - pts)
            r = max(0.0, (rx - prx) / dt / 1024)
            t = max(0.0, (tx - ptx) / dt / 1024)
        _sys_prev["net"][iface] = (rx, tx, now)
        sys["net"].append({"if": iface, "rx_kbs": round(r, 1), "tx_kbs": round(t, 1)})
        sys["net_rx"] += r
        sys["net_tx"] += t
    sys["net_rx"] = round(sys["net_rx"], 1)
    sys["net_tx"] = round(sys["net_tx"], 1)
    sys["temps"] = read_temps()
    if sys["temps"]:
        sys["temp_max"] = max(t["c"] for t in sys["temps"])
    sys["kam_mem"] = read_kam_mem(now)
    sys["kam_procs"] = sys["kam_mem"].get("procs", 0)
    return sys


def db_sys_insert(ts, sys):
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        root = next((d["pct"] for d in sys["disks"] if d["mount"] == "/"),
                    sys["disk_max_pct"])
        con.execute("INSERT OR REPLACE INTO sys_samples VALUES(?,?,?,?,?,?)",
                    (int(ts), sys["cpu_pct"], sys["mem"].get("pct", 0),
                     root, sys["net_rx"], sys["net_tx"]))
        con.execute("DELETE FROM sys_samples WHERE ts < ?", (int(time.time()) - RETENTION_H * 3600,))
        con.commit()
        con.close()
    except Exception:
        pass


def db_sys_history(minutes):
    minutes = max(1, min(1440, minutes))
    since = int(time.time()) - minutes * 60
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        rows = con.execute("SELECT * FROM sys_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        con.close()
    except Exception:
        rows = []
    if len(rows) > 600:
        rows = rows[::len(rows) // 600 + 1]
    return {"cols": ["ts", "cpu_pct", "mem_pct", "disk_pct", "net_rx_kbs", "net_tx_kbs"],
            "rows": rows}


# ---------- collector ----------
def collect_rtpengine():
    global _rtp_last, _rtp_cache
    now = time.time()
    result = dict(_rtp_cache) if _rtp_cache else {
        "status": "unknown", "kernel_mode": False,
        "rtp_sessions": 0, "mos_avg": None, "errors_recent": 0,
    }
    if not RTP_SERVICE:
        result["status"] = "n/a"
        return result
    result["status"] = run(["systemctl", "is-active", RTP_SERVICE], timeout=3).strip()
    result["kernel_mode"] = os.path.exists(RTP_KTABLE)
    if result["kernel_mode"]:
        try:
            with open(RTP_KTABLE + "/list") as f:
                content = f.read()
            result["rtp_sessions"] = content.count("local inet")
        except Exception:
            result["rtp_sessions"] = 0
    if now - _rtp_last >= 10:
        try:
            logs = run(["journalctl", "-u", RTP_SERVICE, "-n", "200",
                        "--no-pager", "-o", "cat"], timeout=5)
            mos_vals = [float(m) for m in re.findall(r"Average MOS ([\d.]+)", logs)]
            result["mos_avg"] = round(sum(mos_vals[-20:]) / len(mos_vals[-20:]), 2) if mos_vals else None
            result["errors_recent"] = len([l for l in logs.splitlines()
                                           if "ERROR" in l or "BUG" in l or "CRITICAL" in l])
        except Exception:
            pass
        _rtp_last = now
    _rtp_cache = dict(result)
    return result


def collect_once():
    global _hist_last_write
    t0 = time.time()

    stats = parse_stat_lines(run(["kamcmd", "stats.get_statistics", "all"], timeout=5))
    if not stats:  # fallback a /metrics de prometheus
        _, body = http_get(METRICS_URL)
        stats = parse_metrics_prom(body or "")

    uptime = run(["kamcmd", "core.uptime"], timeout=4)
    m = re.search(r"uptime:\s*(\d+)", uptime)
    up_secs = int(m.group(1)) if m else 0

    shm = parse_stat_lines("")  # placeholder
    shm_out = run(["kamcmd", "core.shmmem"], timeout=4)
    m2 = re.findall(r"(\w+):\s*(-?\d+)", shm_out)
    shm_d = dict(m2)
    try:
        total = abs(int(shm_d.get("total", 0))) or 1  # int32 desbordado en kamcmd con 2 GB
        used = int(shm_d.get("real_used", shm_d.get("used", 0)))  # incluye fragmentacion
        shm_pct = round(100.0 * used / total, 2)
    except Exception:
        shm_pct, total, used = 0, 0, 0

    dlg = parse_stat_lines(run(["kamcmd", "dlg.stats_active"], timeout=4))
    sl = parse_stat_lines(run(["kamcmd", "sl.stats"], timeout=4))
    for k, v in sl.items():  # sl.stats usa claves numericas; normalizar
        stats[f"sl:{k}"] = v
    tm_out = run(["kamcmd", "tm.stats"], timeout=4)

    dests = dispatcher_list()
    for d in dests:
        d["ignored"] = d.get("set") in DSP_IGNORE_SETS
        d["disabled"] = d.get("flags", "").startswith("D")  # DX = deshabilitado por config, sin sondeo
    live_dests = [d for d in dests if not d["ignored"] and not d["disabled"]]
    if HEALTHZ_URL:
        hz_code, _ = http_get(HEALTHZ_URL, timeout=3)
        rz_code, _ = http_get(READYZ_URL, timeout=4)
    else:  # sin xhttp: liveness = responde RPC; readiness = BD alcanzable
        hz_code = 200 if up_secs > 0 else 0
        rz_code = 0
    db_ok = (rz_code == 200) or db_check()
    if not HEALTHZ_URL:
        rz_code = 200 if db_ok else 503

    pings = {}
    sip_ok_all = True
    ping_res = {}

    def _ping(hp):
        h, p = hp
        ping_res[f"{h}:{p}"] = sip_options_ping(h, p)

    threads = [threading.Thread(target=_ping, args=(hp,), daemon=True)
               for hp in SIP_TARGETS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.5)
    for h, p in SIP_TARGETS:
        k = f"{h}:{p}"
        ok, ms = ping_res.get(k, (False, None))
        pings[k] = {"ok": ok, "ms": ms}
        if not ok:
            sip_ok_all = False

    try:
        load1 = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        load1 = 0.0

    sysinfo = collect_system()
    hainfo = collect_ha()

    now = time.time()
    if now - state["reg_contacts_ts"] > 30:
        c = db_registered_count()
        if c is not None:
            state["reg_db"] = c
            state["reg_contacts_ts"] = now
    mem_regs = stats.get("usrloc:location_contacts")
    state["reg_contacts"] = mem_regs if mem_regs else state.get("reg_db", 0)

    # rates
    rates = {}
    prev = state["last"]
    dt = POLL_INTERVAL
    if prev:
        dt = max(0.5, now - prev["ts"])
        for skey, short in RATE_MAP.items():
            if skey in stats and skey in prev["stats"]:
                delta = stats[skey] - prev["stats"][skey]
                if delta < 0:
                    delta = 0  # reinicio de contadores
                rates[short] = round(delta / dt, 2)
    for _, short in RATE_MAP.items():
        rates.setdefault(short, 0.0)

    active_dialogs = dlg.get("ongoing", dlg.get("all", 0))
    early_dialogs = sum(dlg.get(k, 0) for k in ("starting", "connecting", "answering"))
    rtpengine_info = collect_rtpengine()
    kpi = window_kpis(now, stats, active_dialogs, state["reg_contacts"])
    kpi.update(db_peaks())
    pdd_info = pdd_stats(now)
    outcomes = outcome_stats(now)
    kpi["ner_src"] = "5xx"
    if pdd_info["status"] == "running":
        # NER carrier sobre respuestas finales capturadas (5xx + 408 sin timbrar + sin respuesta)
        kpi.update(ner=outcomes["ner"], ner_n=outcomes["total"], ner_src="carrier")
        acd = acd_stats(now)
        kpi.update(acd=acd["avg"], acd_n=acd["n"], acd_p50=acd["p50"], acd_src="exacta")
    else:
        kpi.update(ner_n=kpi["attempts"], acd_src="estimada")
    kpi5m_record(now, kpi)
    base = kpi_baseline(now)
    kpi["base"], kpi["base_sigma"] = base, BASE_SIGMA
    if base:
        if kpi.get("asr") is not None:
            kpi["asr_z"] = round((kpi["asr"] - base["asr"]) / base["asr_sd"], 1)
        if kpi.get("cps") is not None:
            kpi["cps_z"] = round((kpi["cps"] - base["cps"]) / base["cps_sd"], 1)
    mos_info = mos_stats(now)
    if mos_info["status"] == "running":  # MOS NG reemplaza al promedio de logs
        rtpengine_info["mos_avg"] = mos_info["done"]["avg"] or mos_info["active"]["avg"]
    max_rtt = max((v["ms"] for v in pings.values() if v.get("ms") is not None), default=None)
    snap = {
        "ts": int(now),
        "uptime_s": up_secs,
        "stats": stats,
        "rates": rates,
        "dialogs": dlg,
        "active_dialogs": active_dialogs,
        "early_dialogs": early_dialogs,
        "rtpengine": rtpengine_info,
        "sl": sl,
        "tm_raw": tm_out[:500],
        "shm": {"total": total, "used": used, "pct": shm_pct},
        "load1": load1,
        "system": sysinfo,
        "ha": hainfo,
        "registered": state["reg_contacts"],
        "registered_db": state.get("reg_db"),
        "kpi": kpi,
        "pdd": pdd_info,
        "outcomes": outcomes,
        "security": security_stats(now),
        "mos": mos_info,
        "sip_rtt_max": max_rtt,
        "hostname": socket.gethostname(),
        "site": CFG["site_name"], "domain": SIP_DOMAIN,
        "dispatcher": dests,
        "dispatcher_up": sum(1 for d in live_dests if d.get("up")),
        "dispatcher_total": len(live_dests),
        "dispatcher_disabled": sum(1 for d in dests if d["disabled"]),
        "health": {
            "kamailio": hz_code == 200,
            "metrics": True if stats else False,
            "db": bool(db_ok),
            "readyz": rz_code == 200,
            "sip_ping": sip_ok_all,
            "dispatcher": len(live_dests) == 0 or any(d.get("up") for d in live_dests),
            "shm_ok": shm_pct < 85,
        },
        "sip_ping": pings,
        "codes": {"healthz": hz_code, "readyz": rz_code},
    }

    # alertas estilo NOC carrier
    alerts = []
    if hz_code != 200:
        alerts.append({"sev": "critical", "msg": "Kamailio /healthz no responde (proxy caído o bloqueado)"})
    if not db_ok:
        alerts.append({"sev": "critical", "msg": "Base de datos inalcanzable (readyz + mysql fallan)"})
    if not sip_ok_all:
        bad = [k for k, v in pings.items() if not v["ok"]]
        alerts.append({"sev": "major", "msg": f"SIP OPTIONS sin respuesta en: {', '.join(bad)}"})
    down = [d["uri"] for d in live_dests if not d.get("up")]
    if down:
        alerts.append({"sev": "major", "msg": f"Dispatcher con destinos caídos: {', '.join(down)}"})
    if shm_pct >= 85:
        alerts.append({"sev": "major", "msg": f"SHM al {shm_pct}% — riesgo de rechazo de llamadas"})
    elif shm_pct >= 70:
        alerts.append({"sev": "minor", "msg": f"SHM al {shm_pct}% — vigilar crecimiento"})
    if rates.get("rpl_5xx", 0) > 0.5:
        alerts.append({"sev": "major", "msg": f"Errores 5xx sostenidos: {rates['rpl_5xx']}/s"})
    if rates.get("drop_req", 0) > 1:
        alerts.append({"sev": "minor", "msg": f"Requests dropeados: {rates['drop_req']}/s (posible pike/antiflood)"})
    # RTPEngine
    rtp = snap.get("rtpengine", {})
    if rtp.get("status") == "n/a":
        pass
    elif rtp.get("status") != "active":
        alerts.append({"sev": "critical", "msg": f"RTPEngine daemon '{rtp.get('status','?')}' — sin media proxy, llamadas sin audio"})
    elif not rtp.get("kernel_mode"):
        alerts.append({"sev": "minor", "msg": "RTPEngine en modo userspace — rendimiento degradado, verificar módulo xt_RTPENGINE"})
    md, ma = mos_info["done"], mos_info["active"]
    if mos_info["status"] == "error":
        alerts.append({"sev": "minor", "msg": f"Consulta MOS a RTPEngine falla: {mos_info['error']}"})
    if md["n_mos"] >= 10 and md["bad_pct"] >= 5:
        alerts.append({"sev": "major" if md["bad_pct"] >= 15 else "minor",
                       "msg": f"{md['bad_pct']}% de llamadas con MOS < {MOS_BAD} en 15 min ({md['bad']}/{md['n_mos']})"})
    if ma["bad"] >= 3:
        alerts.append({"sev": "minor", "msg": f"{ma['bad']} llamadas activas con MOS < {MOS_BAD} (mín {ma['min']})"})
    elif mos_info["status"] != "running" and rtp.get("mos_avg") is not None and rtp["mos_avg"] < 3.5:
        alerts.append({"sev": "major", "msg": f"MOS bajo: {rtp['mos_avg']:.2f} — calidad de voz degradada (umbral 3.5)"})
    # HA: peers DMQ + keepalived
    for p in hainfo.get("peers", []):
        if not p.get("local") and p.get("status") != "active":
            alerts.append({"sev": "major", "msg": f"DMQ peer {p.get('host')} en estado {p.get('status')} (replicación degradada)"})
    if HA_VIP and hainfo.get("keepalived") != "active":
        alerts.append({"sev": "major", "msg": f"keepalived '{hainfo.get('keepalived')}' — sin VIP/failover automático"})
    # hardware estilo NOC
    cpu, memp = sysinfo["cpu_pct"], sysinfo["mem"].get("pct", 0)
    dmax = sysinfo["disk_max_pct"]
    if cpu >= 90:
        alerts.append({"sev": "major", "msg": f"CPU al {cpu}% — saturación"})
    elif cpu >= 75:
        alerts.append({"sev": "minor", "msg": f"CPU al {cpu}% — carga alta"})
    if memp >= 90:
        alerts.append({"sev": "major", "msg": f"RAM al {memp}% — riesgo de OOM"})
    elif memp >= 80:
        alerts.append({"sev": "minor", "msg": f"RAM al {memp}% — vigilar"})
    if dmax >= 90:
        alerts.append({"sev": "critical", "msg": f"Disco al {dmax}% — riesgo de corte de logs/DB"})
    elif dmax >= 80:
        alerts.append({"sev": "major", "msg": f"Disco al {dmax}% — limpiar pronto"})
    tmax = sysinfo["temp_max"]
    if tmax is not None and tmax >= 80:
        alerts.append({"sev": "major", "msg": f"Temperatura CPU {tmax}°C"})
    elif tmax is not None and tmax >= 70:
        alerts.append({"sev": "minor", "msg": f"Temperatura CPU {tmax}°C"})
    # KPIs de servicio (requieren muestra estadistica minima)
    if kpi.get("reg_drop_pct") is not None:
        if kpi["reg_drop_pct"] >= 15:
            alerts.append({"sev": "critical", "msg": f"Caída masiva de registros: -{kpi['reg_drop_pct']}% en 10 min"})
        elif kpi["reg_drop_pct"] >= 5:
            alerts.append({"sev": "major", "msg": f"Caída de registros: -{kpi['reg_drop_pct']}% en 10 min"})
    if kpi.get("calls_drop_pct") is not None and kpi["calls_drop_pct"] >= 50:
        alerts.append({"sev": "major", "msg": f"Llamadas activas cayeron {kpi['calls_drop_pct']}% en 2 min"})
    if kpi.get("attempts", 0) >= 20:
        if kpi["asr"] is not None and kpi["asr"] < 30:
            alerts.append({"sev": "major", "msg": f"ASR bajo: {kpi['asr']}% (5 min, {kpi['attempts']} intentos)"})
        elif kpi.get("asr_z") is not None and kpi["asr_z"] <= -BASE_SIGMA:
            alerts.append({"sev": "major", "msg": f"ASR {kpi['asr']}% a {-kpi['asr_z']}σ bajo la base "
                           f"{base['mode']} ({base['asr']}% ± {base['asr_sd']})"})
    if base and base["cps"] >= 0.2 and kpi.get("cps_z") is not None and kpi["cps_z"] <= -BASE_SIGMA:
        alerts.append({"sev": "major", "msg": f"Tráfico (CPS {kpi['cps']}) a {-kpi['cps_z']}σ bajo la base "
                       f"{base['mode']} ({base['cps']} ± {base['cps_sd']})"})
    if (kpi.get("ner_n") or 0) >= 20 and kpi["ner"] is not None and kpi["ner"] < 95:
        nf = outcomes["net_fail"]
        why = (f"5xx {nf['5xx']} · 408 {nf['408']} · sin respuesta {nf['noresp']}"
               if kpi["ner_src"] == "carrier" else "fallos 5xx")
        alerts.append({"sev": "major" if kpi["ner"] < 90 else "minor",
                       "msg": f"NER degradado: {kpi['ner']}% — {why}"})
    po, pi = pdd_info["out"], pdd_info["in"]
    if pdd_info["status"] == "error":
        alerts.append({"sev": "minor", "msg": f"Medición PDD detenida: {pdd_info['error']}"})
    if po["n"] >= 10 and po["p95"] > 5:
        alerts.append({"sev": "major", "msg": f"PDD saliente p95 {po['p95']:.1f} s (umbral 5 s, {po['n']} llamadas)"})
    elif po["n"] >= 10 and po["avg"] > 3:
        alerts.append({"sev": "minor", "msg": f"PDD saliente promedio {po['avg']:.1f} s (umbral 3 s)"})
    if pi["n"] >= 10 and pi["avg"] > 2:
        alerts.append({"sev": "minor", "msg": f"PDD entrante promedio {pi['avg']:.1f} s — ONT/softphones lentos en timbrar"})
    if pdd_info["noresp_out"] + pdd_info["noresp_in"] >= 5:
        alerts.append({"sev": "minor", "msg": f"INVITE sin respuesta (32 s): {pdd_info['noresp_out']} salientes, {pdd_info['noresp_in']} entrantes en 5 min"})
    if kpi.get("reg_ok_pct") is not None and kpi["reg_ok_pct"] < 95:
        alerts.append({"sev": "minor", "msg": f"Tasa de registro exitoso {kpi['reg_ok_pct']}%"})
    snap["status"] = "critical" if any(a["sev"] == "critical" for a in alerts) else \
                     "major" if alerts else "ok"
    snap["alerts"] = alarms_update(alerts, now)
    if snap["alerts"] is None:  # sqlite no disponible: alertas del ciclo sin persistencia
        snap["alerts"] = [{"id": None, "raised": int(now), "present": True, **a} for a in alerts]

    with state["lock"]:
        state["last"] = {"ts": now, "stats": dict(stats)}
        state["snap"] = snap
        state["series"].append((int(now), dict(rates), active_dialogs))

    if now - _hist_last_write >= HIST_EVERY:
        _hist_last_write = now
        db_insert(now, rates, active_dialogs, state["reg_contacts"], shm_pct, load1,
                  kpi, early_dialogs, rtpengine_info.get("rtp_sessions"),
                  not any(a["sev"] == "critical" for a in alerts), pdd_info, mos_info)
        db_sys_insert(now, sysinfo)

    elapsed = time.time() - t0
    return elapsed


def collector_loop():
    while True:
        try:
            collect_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "kamailio-noc/1.0"

    def log_message(self, *a):
        pass

    def base(self):
        """Prefijo publico (nginx envia X-Forwarded-Prefix: /noc/)."""
        p = (self.headers.get("X-Forwarded-Prefix", "") or "").rstrip("/")
        return p

    def is_https(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def sec_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; img-src 'self' data:; frame-ancestors 'none'")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.sec_headers()
        self.end_headers()

    def serve_file(self, name, ctype):
        p = os.path.join(BASE, name)
        if not os.path.isfile(p):
            self.send_response(404)
            self.end_headers()
            return
        with open(p, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def set_session_cookie(self, tok):
        base = self.base()
        path = (base + "/") if base else "/"
        c = f"{COOKIE_NAME}={tok}; HttpOnly; SameSite=Lax; Path={path}; Max-Age={SESSION_TTL}"
        if self.is_https():
            c += "; Secure"
        self.send_header("Set-Cookie", c)

    def clear_session_cookie(self):
        base = self.base()
        path = (base + "/") if base else "/"
        self.send_response(302)
        self.send_header("Location", self.base() + "/login")
        self.send_header("Set-Cookie",
                         f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path={path}; Max-Age=0")
        self.sec_headers()
        self.end_headers()

    def do_login_post(self):
        ip = client_ip(self)
        if is_locked(ip):
            return self.serve_login("Bloqueado 15 min por demasiados intentos.")
        try:
            n = min(int(self.headers.get("Content-Length", "0")), 4096)
            body = self.rfile.read(n).decode("utf-8", "replace")
        except Exception:
            body = ""
        q = parse_qs(body)
        user = q.get("user", [""])[0].strip()[:64]
        pw = q.get("password", [""])[0][:256]
        users = load_users()
        rec = users.get(user)
        if rec and verify_password(pw, rec):
            clear_fails(ip)
            tok = create_session(user, ip)
            if not tok:
                return self.serve_login("Error interno, reintente.")
            self.send_response(302)
            self.set_session_cookie(tok)
            self.send_header("Location", self.base() + "/")
            self.sec_headers()
            self.end_headers()
        else:
            locked = reg_fail(ip)
            self.serve_login("Bloqueado 15 min por demasiados intentos." if locked
                             else "Usuario o clave incorrectos.")

    def serve_login(self, error=""):
        try:
            with open(os.path.join(BASE, "login.html")) as f:
                html = f.read()
        except Exception:
            html = "<html><body><h1>NOC</h1><p>login.html ausente</p></body></html>"
        html = html.replace("<!--ERROR-->", f'<p class="err">{error}</p>' if error else "")
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        # compat nginx /noc/ prefix
        if path == "/noc" or path == "/noc/":
            path = "/"
        elif path.startswith("/noc/"):
            path = path[4:]
        # --- gate: publico solo login + su CSS + healthz LB ---
        if path == "/login":
            return self.serve_login()
        if path == "/style.css":
            return self.serve_file("style.css", "text/css; charset=utf-8")
        if path == "/logout":
            destroy_session(parse_cookies(self).get(COOKIE_NAME, ""))
            return self.clear_session_cookie()
        user = get_session_user(parse_cookies(self).get(COOKIE_NAME, ""))
        if path == "/api/healthz":
            with state["lock"]:
                s = state["snap"]
            ok = bool(s and s["health"].get("kamailio"))
            return self.send_json({"status": "up" if ok else "down",
                                   "ts": int(time.time())}, 200 if ok else 503)
        if not user:
            if path.startswith("/api/"):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.sec_headers()
                self.end_headers()
                self.wfile.write(b'{"error":"login required"}')
                return
            return self.redirect(self.base() + "/login")
        self._user = user
        if path == "/" or path == "/index.html":
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self.serve_file("app.js", "application/javascript; charset=utf-8")
        if path == "/style.css":
            return self.serve_file("style.css", "text/css; charset=utf-8")
        if path == "/api/snapshot":
            with state["lock"]:
                s = state["snap"]
            if not s:
                return self.send_json({"error": "collecting, retry in 3s"}, 503)
            out = dict(s)
            out.pop("stats", None)  # snapshot ligero; rates ya calculados
            out.pop("tm_raw", None)
            return self.send_json(out)
        if path == "/api/history":
            q = parse_qs(u.query)
            try:
                minutes = int(q.get("minutes", ["60"])[0])
            except ValueError:
                minutes = 60
            return self.send_json(db_history(minutes))
        if path == "/api/syshistory":
            q = parse_qs(u.query)
            try:
                minutes = int(q.get("minutes", ["60"])[0])
            except ValueError:
                minutes = 60
            return self.send_json(db_sys_history(minutes))
        if path == "/api/calls":
            q = parse_qs(u.query)
            try:
                minutes = int(q.get("minutes", ["60"])[0])
            except ValueError:
                minutes = 60
            return self.send_json(db_calls(minutes, q.get("bad", ["0"])[0] == "1",
                                           q.get("order", ["recent"])[0]))
        if path == "/api/alarms":
            q = parse_qs(u.query)
            try:
                hours = int(q.get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            return self.send_json(db_alarm_history(hours))
        if path == "/api/live":
            with state["lock"]:
                pts = list(state["series"])[-120:]
            return self.send_json([{"ts": t, "rates": r, "dialogs": d} for t, r, d in pts])
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        if path.startswith("/noc/"):
            path = path[4:]
        if path == "/login":
            return self.do_login_post()
        if path == "/api/alarms/ack":
            user = get_session_user(parse_cookies(self).get(COOKIE_NAME, ""))
            # JSON obligatorio: un formulario de otro sitio no puede enviarlo sin preflight CORS
            if not user or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_json({"error": "login required"}, 401 if not user else 415)
            try:
                n = min(int(self.headers.get("Content-Length", "0")), 1024)
                aid = int(json.loads(self.rfile.read(n) or b"{}").get("id"))
            except Exception:
                return self.send_json({"error": "bad request"}, 400)
            ok = alarm_ack(aid, user)
            return self.send_json({"ok": ok}, 200 if ok else 404)
        self.send_response(404)
        self.end_headers()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--set-password":
        user = sys.argv[2]
        import getpass
        pw = getpass.getpass(f"Nueva clave para '{user}': ")
        if len(pw) < 10:
            print("ERROR: minimo 10 caracteres", file=sys.stderr)
            sys.exit(1)
        users = load_users()
        users[user] = hash_password(pw)
        save_users(users)
        print(f"OK: usuario '{user}' actualizado")
        return
    db_init()
    alarms_load()
    ensure_bootstrap_user()
    # primera muestra rapida (sin rates aun)
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()
    threading.Thread(target=pdd_loop, daemon=True).start()
    threading.Thread(target=mos_loop, daemon=True).start()
    threading.Thread(target=notify_loop, daemon=True).start()
    write_mycnf()
    port = int(os.environ.get("NOC_PORT", CFG.get("listen_port", 8899)))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"kamailio-noc en http://127.0.0.1:{port}/  (db={DB_PATH})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
