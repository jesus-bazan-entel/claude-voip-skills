#!/usr/bin/env python3
"""Autodescubre la configuracion del NOC en un proxy Kamailio -> noc.conf.json.

Lee (solo lectura): kamailio.cfg, keepalived.conf, rtpengine.conf, `kamcmd dispatcher.list`,
`systemctl`. No modifica nada salvo el fichero de salida (--write, permisos 600).

Uso:
  discover.py                       # imprime JSON (password enmascarada) + avisos
  discover.py --write /opt/kamailio-noc/noc.conf.json
  discover.py --cfg /etc/kamailio/kamailio.cfg --site "operador.pe"
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def strip_comments(txt):
    """Quita comentarios '#' de kamailio.cfg conservando directivas '#!'."""
    out = []
    for line in txt.splitlines():
        s = line.lstrip()
        if s.startswith("#!"):
            out.append(line)
        elif s.startswith("#") or s.startswith("//"):
            continue
        else:
            out.append(re.sub(r"\s#(?!!).*$", "", line))
    return "\n".join(out)


def parse_kamailio(cfg_txt, warn):
    txt = strip_comments(cfg_txt)
    defines = dict(re.findall(r'#!(?:define|subst)\s+(\w+)\s+"?([^"\n]+)"?', txt))
    res = {}

    # listeners UDP (sin loopback); se excluyen el socket DMQ y la VIP (solo vive en el MASTER)
    listens = re.findall(r"^\s*listen\s*=\s*udp:([\d.]+):(\d+)", txt, re.M)
    dmq = re.search(r'modparam\("dmq",\s*"server_address",\s*"sip:([\d.]+):(\d+)"', txt)
    dmq_sock = (dmq.group(1), dmq.group(2)) if dmq else None
    res["listens"] = [(h, int(p)) for h, p in listens
                      if not h.startswith("127.") and (h, p) != dmq_sock]
    if not res["listens"]:
        warn.append("No hay listen=udp:IP:PUERTO explícitos; completar sip_targets a mano.")

    aliases = re.findall(r'^\s*alias\s*=\s*"?([\w.\-]+)"?', txt, re.M)
    res["domain"] = aliases[0] if aliases else ""
    if not aliases:
        warn.append("Sin alias= en kamailio.cfg: sip_domain vacío (el OPTIONS usará 'localhost').")

    # BD de usrloc: modparam("usrloc","db_url", DEFINE|"mysql://...")
    m = re.search(r'modparam\("usrloc",\s*"db_url",\s*"?([^")]+)"?\)', txt)
    url = ""
    if m:
        url = defines.get(m.group(1).strip(), m.group(1).strip())
    if not url.startswith("mysql://"):
        cands = [v for v in defines.values() if v.startswith("mysql://")]
        url = next((v for v in cands if v.rstrip("/").endswith("/kamailio")), cands[0] if cands else "")
    um = re.match(r"mysql://([^:@/]+)(?::([^@/]*))?@([^:/]+)(?::(\d+))?/(\w+)", url)
    res["db"] = ({"host": um.group(3), "port": int(um.group(4) or 3306), "user": um.group(1),
                  "password": um.group(2) or "", "name": um.group(5)} if um else None)
    if not um:
        warn.append("No se encontró db_url MySQL de usrloc: db=null (sin contador desde BD).")

    # xhttp con /healthz propio (no es estandar de Kamailio)
    http = re.search(r"^\s*listen\s*=\s*tcp:127\.0\.0\.1:(\d+)", txt, re.M)
    has_hz = "event_route[xhttp:request]" in txt and "/healthz" in txt
    res["kamailio_http"] = f"http://127.0.0.1:{http.group(1)}" if (http and has_hz) else ""
    if not res["kamailio_http"]:
        warn.append("Sin endpoints xhttp /healthz: liveness por kamcmd (ver references/kamailio-xhttp.md).")
    return res


def parse_dispatcher(warn):
    out = sh(["kamcmd", "dispatcher.list"])
    sets, cur = {}, None
    for line in out.splitlines():
        m = re.search(r"ID:\s*(\d+)", line)
        if m:
            cur = int(m.group(1))
        m = re.search(r"URI:\s*sip:([\d.]+)", line)
        if m and cur is not None:
            sets.setdefault(cur, []).append({"ip": m.group(1), "flags": ""})
        m = re.search(r"FLAGS:\s*(\S+)", line)
        if m and cur is not None and sets.get(cur):
            sets[cur][-1]["flags"] = m.group(1)
    if not out:
        warn.append("kamcmd dispatcher.list sin salida (¿sin módulo dispatcher o sin permisos?).")
    core = sorted({d["ip"] for s in sets.values() for d in s if d["flags"].startswith("A")})
    dead = sorted(k for k, v in sets.items() if v and all(not d["flags"].startswith("A") for d in v))
    if dead:
        warn.append(f"Sets dispatcher sin destinos activos: {dead}. Si NO llevan tráfico en "
                    "producción, añadirlos a dispatcher_ignore_sets (confirmar con el usuario).")
    return core, dead


def parse_keepalived():
    txt = read("/etc/keepalived/keepalived.conf")
    m = re.search(r"virtual_ipaddress\s*\{\s*([\d.]+)", txt)
    return m.group(1) if m else ""


def parse_rtpengine(warn):
    units = sh(["systemctl", "list-unit-files", "--no-legend", "rtpengine*"])
    svc = ""
    for name in ("rtpengine-daemon.service", "rtpengine.service", "ngcp-rtpengine-daemon.service"):
        if name in units:
            svc = name.replace(".service", "")
            break
    conf = ""
    for p in ("/etc/rtpengine/rtpengine.conf", "/etc/rtpengine.conf"):
        conf = read(p)
        if conf:
            break
    ng = re.search(r"^\s*listen-ng\s*=\s*(\S+)", conf, re.M)
    table = re.search(r"^\s*table\s*=\s*(-?\d+)", conf, re.M)
    if not svc:
        warn.append("RTPEngine no instalado: rtpengine deshabilitado (sin MOS por llamada).")
    ngv = ng.group(1) if ng else ("127.0.0.1:2223" if svc else "")
    if ngv and ":" not in ngv:
        ngv = "127.0.0.1:" + ngv
    return {"ng": ngv.replace("localhost", "127.0.0.1"), "service": svc,
            "kernel_table": int(table.group(1)) if table and int(table.group(1)) >= 0 else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="/etc/kamailio/kamailio.cfg")
    ap.add_argument("--site", default="")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--write", metavar="PATH")
    a = ap.parse_args()

    warn = []
    cfg_txt = read(a.cfg)
    if not cfg_txt:
        sys.exit(f"No se pudo leer {a.cfg}")
    k = parse_kamailio(cfg_txt, warn)
    core, _dead = parse_dispatcher(warn)
    if not core:
        warn.append("Sin destinos dispatcher activos: core_ips vacío (PDD contará todo como saliente).")
    vip = parse_keepalived()
    k["listens"] = [(h, p) for h, p in k["listens"] if h != vip]  # la VIP solo existe en el MASTER
    ports = sorted({p for _, p in k["listens"]}) or [5060]
    conf = {
        "site_name": a.site or k["domain"] or socket.gethostname(),
        "sip_domain": k["domain"],
        "sip_targets": [[h, p] for h, p in k["listens"]],
        "kamailio_http": k["kamailio_http"],
        "db": k["db"],
        "ha": {"vip": vip, "keepalived_service": "keepalived"},
        "core_ips": core,
        "dispatcher_ignore_sets": [],
        "rtpengine": parse_rtpengine(warn),
        "pdd": {"enabled": True, "bpf": "udp and (" + " or ".join(f"port {p}" for p in ports) + ")"},
        "mos_bad": 3.6,
        "listen_port": a.port,
    }
    if not conf["ha"]["vip"]:
        warn.append("Sin keepalived/VIP: nodo único (sin alarmas de failover).")

    if a.write:
        fd = os.open(a.write, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(conf, f, indent=2)
        print(f"Escrito {a.write} (600)")
    shown = json.loads(json.dumps(conf))
    if shown.get("db"):
        shown["db"]["password"] = "****"
    print(json.dumps(shown, indent=2))
    for w in warn:
        print("AVISO:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
