#!/usr/bin/env python3
"""Prueba de humo del NOC instalado: API, KPIs, sondas, estáticos.

Crea una sesión temporal (5 min) directamente en noc_history.db, consulta la API
local y la borra al terminar. No necesita la clave del usuario.

  smoke_test.py [--dir /opt/kamailio-noc] [--wait 60]
Sale con código 1 si algo crítico falla.
"""
import argparse
import json
import secrets
import sqlite3
import sys
import time
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/opt/kamailio-noc")
    ap.add_argument("--wait", type=int, default=45, help="segundos para acumular muestras")
    a = ap.parse_args()
    conf = json.load(open(f"{a.dir}/noc.conf.json"))
    base = f"http://127.0.0.1:{conf.get('listen_port', 8899)}"
    db = f"{a.dir}/noc_history.db"
    tok = "smoketest" + secrets.token_hex(12)
    now = int(time.time())
    con = sqlite3.connect(db, timeout=5)
    con.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (tok, "smoke", now, now, now + 300))
    con.commit()
    fails, warns = [], []

    def get(path):
        req = urllib.request.Request(base + path, headers={"Cookie": f"noc_session={tok}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    try:
        time.sleep(max(0, a.wait))
        for p in ("/", "/app.js", "/style.css"):
            code, _ = get(p)
            (fails if code != 200 else []).append(f"{p} HTTP {code}")
        s = json.loads(get("/api/snapshot")[1])
        h = s.get("health", {})
        for k in ("kamailio", "metrics", "db", "sip_ping"):
            if not h.get(k):
                fails.append(f"health.{k} = False")
        k = s.get("kpi", {})
        print(f"estado={s['status']}  llamadas={s.get('active_dialogs')}  timbrando={s.get('early_dialogs')}  "
              f"registrados={s.get('registered')}  (BD {s.get('registered_db')})")
        print(f"CPS={k.get('cps')}  ASR={k.get('asr')}%  NER={k.get('ner')}%  ACD={k.get('acd')}s  "
              f"RSR={k.get('reg_ok_pct')}%  ventana={k.get('window_s')}s")
        pd, mo = s.get("pdd", {}), s.get("mos", {})
        print(f"PDD sonda={pd.get('status')} saliente={pd.get('out')} entrante={pd.get('in')}")
        print(f"MOS sonda={mo.get('status')} activas={mo.get('active')} finalizadas={mo.get('done')}")
        oc, sc = s.get("outcomes") or {}, s.get("security") or {}
        print(f"NER fuente={k.get('ner_src')} n={k.get('ner_n')}  ACD fuente={k.get('acd_src')} n={k.get('acd_n')}  "
              f"base={k.get('base')}")
        print(f"respuestas INVITE 5 min: {oc.get('total')} {[(c['k'], c['n']) for c in oc.get('cats', [])]}")
        print(f"403: {sc.get('rate')}/s  IPs={sc.get('unique_ips')}  ipban={sc.get('ipban_n')}  "
              f"fail2ban={sc.get('f2b_jails')}")
        km = (s.get("system") or {}).get("kam_mem") or {}
        print(f"kamailio memoria: PSS {km.get('pss_mb')} MB (SHM {km.get('pss_shm_mb')}) PKG {km.get('pkg_used_mb')} MB")
        for key in ("outcomes", "security"):
            if key not in s:
                fails.append(f"snapshot sin '{key}' (app.py desactualizado)")
        if pd.get("status") == "running" and k.get("ner_src") != "carrier":
            fails.append("sonda SIP activa pero NER no usa respuestas capturadas")
        if pd.get("status") == "error":
            fails.append(f"sonda PDD: {pd.get('error')}")
        if mo.get("status") == "error":
            fails.append(f"sonda MOS: {mo.get('error')}")
        if not s.get("registered"):
            warns.append("registrados = 0 (¿usrloc vacío o stats sin usrloc:location_contacts?)")
        if s.get("early_dialogs", 0) > 10**9:
            fails.append("early_dialogs con underflow")
        for al in s.get("alerts", []):
            print(f"  alarma #{al.get('id')} {al['sev']}: {al['msg']}" + (f" (ack {al['ack_by']})" if al.get("ack_by") else ""))
            if al.get("id") is None:
                fails.append("alarmas sin id: la tabla alarms de sqlite no está disponible")
        ah = json.loads(get("/api/alarms?hours=24")[1])
        print(f"historial de alarmas 24 h: {len(ah.get('rows', []))}")
        hist = json.loads(get("/api/history?minutes=5")[1])
        if not hist.get("rows"):
            warns.append("histórico vacío (normal si el servicio arrancó hace <10 s)")
        calls = json.loads(get("/api/calls?minutes=60")[1])
        print(f"llamadas finalizadas 60 min: {calls.get('total')} (con MOS {calls.get('with_mos')})")
    except Exception as e:
        fails.append(f"excepción: {e}")
    finally:
        con.execute("DELETE FROM sessions WHERE token=?", (tok,))
        con.commit()
        con.close()
    for w in warns:
        print("AVISO:", w)
    for f in fails:
        print("FALLO:", f)
    print("RESULTADO:", "OK" if not fails else "FALLÓ")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
