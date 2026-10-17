#!/usr/bin/env bash
# Instala/actualiza el Kamailio NOC en un proxy. Idempotente.
#   install.sh [--dir /opt/kamailio-noc] [--port 8899] [--site nombre] [--no-start]
# - Copia assets (no pisa noc.conf.json, users.json ni noc_history.db existentes).
# - Si no hay noc.conf.json, lo genera con discover.py (revisar antes de producción).
# - Crea/actualiza kamailio-noc.service. NO toca kamailio.cfg ni nginx (imprime el snippet).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ASSETS="$HERE/../assets"
DIR=/opt/kamailio-noc PORT=8899 SITE="" START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --site) SITE="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: ejecutar como root (AF_PACKET, kamcmd, systemctl)" >&2; exit 1; }

UNIT=/etc/systemd/system/kamailio-noc.service
if [ -f "$UNIT" ]; then
  CUR="$(sed -n 's/^WorkingDirectory=//p' "$UNIT")"
  if [ -n "$CUR" ] && [ "$CUR" != "$DIR" ]; then
    echo "ERROR: kamailio-noc ya está instalado en $CUR. Usar --dir $CUR para actualizarlo" >&2
    echo "       (o migrar a mano). No se sobrescribe otra instalación." >&2
    exit 1
  fi
fi

echo "== Pre-requisitos"
fail=0
python3 -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null && echo "  ok  python3 >= 3.9" || { echo "  FALTA python3 >= 3.9"; fail=1; }
for b in kamcmd systemctl; do command -v "$b" >/dev/null && echo "  ok  $b" || { echo "  FALTA $b"; fail=1; }; done
command -v tcpdump >/dev/null && echo "  ok  tcpdump (compila el filtro BPF del PDD)" || echo "  aviso: sin tcpdump -> PDD deshabilitado en runtime (apt install tcpdump)"
command -v mysql >/dev/null && echo "  ok  mysql client" || echo "  aviso: sin cliente mysql -> sin conteo de location desde BD"
kamcmd core.uptime >/dev/null 2>&1 && echo "  ok  kamcmd responde" || { echo "  FALTA: kamcmd no responde (¿Kamailio arriba? ¿ctl.so?)"; fail=1; }
[ $fail -eq 0 ] || { echo "Corregir pre-requisitos y reintentar." >&2; exit 1; }

echo "== Copiando a $DIR"
install -d -m 755 "$DIR"
for f in app.py index.html app.js style.css login.html noc.conf.example.json; do
  install -m 644 "$ASSETS/$f" "$DIR/$f"
done

if [ ! -f "$DIR/noc.conf.json" ]; then
  echo "== Generando noc.conf.json (autodescubrimiento)"
  python3 -I "$HERE/discover.py" --port "$PORT" ${SITE:+--site "$SITE"} --write "$DIR/noc.conf.json"
  echo ">> REVISAR $DIR/noc.conf.json (core_ips, dispatcher_ignore_sets) antes de dar por buena la instalación."
else
  echo "== Conservando $DIR/noc.conf.json existente"
  PORT="$(python3 -c "import json;print(json.load(open('$DIR/noc.conf.json')).get('listen_port', $PORT))")"
fi
chmod 600 "$DIR/noc.conf.json"

echo "== systemd"
sed "s#__NOC_DIR__#$DIR#g" "$ASSETS/kamailio-noc.service" > "$UNIT"
systemctl daemon-reload
systemctl enable kamailio-noc.service >/dev/null 2>&1
if [ $START -eq 1 ]; then
  systemctl restart kamailio-noc.service
  for i in $(seq 1 30); do
    curl -fs "http://127.0.0.1:$PORT/api/healthz" >/dev/null 2>&1 && break; sleep 1
  done
  systemctl is-active --quiet kamailio-noc.service && echo "  kamailio-noc activo en 127.0.0.1:$PORT" \
    || { echo "  ERROR: el servicio no arrancó; ver journalctl -u kamailio-noc" >&2; exit 1; }
  if [ -f "$DIR/.noc-initial-cred" ]; then
    echo "  Usuario inicial en $DIR/.noc-initial-cred (600). Cambiar: python3 $DIR/app.py --set-password noc"
  fi
fi

echo "== nginx (no se modifica automáticamente). Snippet para el server{} HTTPS:"
sed "s#__NOC_PORT__#$PORT#g" "$ASSETS/nginx-noc.conf"
