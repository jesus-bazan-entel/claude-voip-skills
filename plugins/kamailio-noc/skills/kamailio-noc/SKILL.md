---
name: kamailio-noc
description: Despliega o actualiza un dashboard NOC carrier-class (estilo Vodafone/AT&T) en un proxy SIP Kamailio — KPIs de servicio (llamadas activas, registrados, CPS, ASR, NER, ACD, PDD, MOS por llamada, disponibilidad), alarmas por severidad, elementos de red y gráficas con histórico 24h. Usar cuando el usuario pida monitoreo/NOC/dashboard/healthcheck/KPIs para Kamailio, medir PDD o MOS, o replicar este NOC en otro proxy.
---

# Kamailio NOC carrier-class

Backend Python **solo stdlib** + dashboard vanilla JS (sin CDN, funciona offline, CSP
estricta). Corre como `kamailio-noc.service` en `127.0.0.1:<puerto>` y se publica por nginx
en `/noc/` (HTTPS + login con sesiones PBKDF2). **No modifica `kamailio.cfg`**: todo se mide
con `kamcmd`, captura pasiva y el socket NG de RTPEngine.

## Archivos

| Ruta | Qué es |
|---|---|
| `assets/app.py` | colector + API + servidor (lee `noc.conf.json`) |
| `assets/index.html`, `app.js`, `style.css`, `login.html` | dashboard |
| `assets/noc.conf.example.json` | todas las claves de configuración |
| `assets/kamailio-noc.service`, `nginx-noc.conf` | plantillas systemd / nginx |
| `scripts/discover.py` | autodescubre la config del proxy (solo lectura) |
| `scripts/install.sh` | instala/actualiza (idempotente, no pisa config/usuarios/histórico) |
| `scripts/smoke_test.py` | verifica API, KPIs y sondas con una sesión temporal |
| `references/kpis.md` | fórmulas, fuentes y umbrales de cada KPI, línea base, sonda SIP y alarmas |
| `references/config.md` | cada clave de `noc.conf.json` y cómo degradar |
| `references/kamailio-xhttp.md` | snippet opcional `/healthz` `/readyz` `/metrics` |
| `references/troubleshooting.md` | fallos conocidos y su causa |

## Flujo de despliegue

Ejecutar en el proxy destino (root). Rutas relativas al directorio de este skill.

1. **Reconocimiento (solo lectura).** Confirmar que `kamcmd core.uptime` responde y ver
   qué hay: `kamcmd stats.get_statistics all | head`, `kamcmd dlg.stats_active`,
   `systemctl list-units 'rtpengine*' keepalived`, `ss -unlp | grep kamailio`.
   Si ya existe `kamailio-noc.service`, leer su `WorkingDirectory` — es una actualización.
2. **Descubrir config:** `python3 -I scripts/discover.py --site "<nombre>"`.
   Muestra el JSON (password enmascarada) y avisos. **Revisar con el usuario**:
   - `core_ips`: IPs del core (FreeSWITCH/SBC/carrier) — define entrante vs saliente en PDD.
     discover toma los destinos dispatcher activos; confirmar que es el flujo real.
   - `dispatcher_ignore_sets`: discover **lista** los sets sin destinos activos pero nunca
     los ignora solo. Ignorar un set oculta caídas reales → decisión explícita del usuario.
   - `sip_targets` excluye la VIP (solo existe en el MASTER) y el socket DMQ.
3. **Instalar:** `bash scripts/install.sh --dir /opt/kamailio-noc --site "<nombre>"`
   (o `--dir <WorkingDirectory actual>` si es actualización; el script aborta si detecta
   otra instalación en otro directorio). Genera `noc.conf.json` (600) si no existe,
   crea/reinicia el servicio y muestra el snippet nginx.
   El usuario inicial queda en `<dir>/.noc-initial-cred`; cambiarlo con
   `python3 <dir>/app.py --set-password noc`.
4. **Ajustar config** si hace falta (editar `<dir>/noc.conf.json`, `systemctl restart kamailio-noc`).
5. **Verificar:** `python3 -I scripts/smoke_test.py --dir <dir> --wait 60`. Debe terminar en
   `RESULTADO: OK`. ASR/NER/ACD necesitan ≥20 s de ventana; MOS por llamada necesita
   llamadas finalizadas con RTCP.
6. **nginx (pedir confirmación, es exposición externa):** incluir el snippet dentro del
   `server{}` HTTPS, `nginx -t && systemctl reload nginx`. Nunca exponer `:8899` directo.
7. Si hay un nodo HA, repetir en el peer con su propia config (sus `sip_targets` difieren).

## Reglas

- **No tocar `kamailio.cfg`** salvo pedido explícito. Los endpoints xhttp son opcionales
  (`references/kamailio-xhttp.md`); sin ellos la liveness sale de `kamcmd`.
- Reiniciar solo `kamailio-noc`, nunca Kamailio/RTPEngine, para desplegar el NOC.
- Credenciales: solo en `noc.conf.json` (600); MySQL se invoca con `--defaults-extra-file`
  (nunca password en argv). No subir `noc.conf.json`, `users.json`, `.noc-*` ni `noc_history.db`
  a repositorios.
- **No lanzar una segunda instancia en el mismo directorio** ni en otro sin `users.json`
  sin entender el bootstrap: crea un usuario inicial (en `<dir>/.noc-initial-cred`).
- Para probar cambios en una copia aislada usar otro `--dir` y otro `listen_port`,
  con `"pdd": {"enabled": false}` y sin tocar la unidad systemd.
- Sin navegador en el servidor: validar el frontend con `smoke_test.py` y pedir al usuario
  que revise visualmente.

## Lecciones aprendidas (ya resueltas en el código — no reintroducir)

- `dialog:early_dialogs` hace underflow (≈1.8e19). Llamadas activas = `dlg.stats_active`
  → `ongoing`; timbrando = `starting+connecting+answering`. `all` mezcla ambas.
- `core:rcv_replies_2xx` está dominado por los 200 OK de keepalive OPTIONS (NAT ping):
  las respuestas se grafican **solo para INVITE** (`rcv_replies_Nxx_invite`).
- Registrados: `usrloc:location_contacts` en memoria cada 2 s (MySQL cada 30 s como
  contraste). Graficar con eje ajustado, no desde 0 — si no, una caída del 5% no se ve.
- Gráficas: un solo eje Y por gráfica; magnitudes distintas → gráficas separadas
  (llamadas activas y registrados nunca juntas).
- MOS: solo existe para llamadas con RTCP (~70% típico). Mostrar "sin RTCP", no 0.
- PDD con 401/407: medir desde el INVITE con credenciales (nuevo CSeq).
- Sets dispatcher "muertos" de diseño alarman permanentemente → `dispatcher_ignore_sets`.
  Los destinos `D*` (deshabilitados por config) ya se muestran en gris sin alarmar.
- MOS de G.711 se satura en 4,3: mostrar % bajo umbral, cobertura RTCP y p95 de
  jitter/pérdida/RTT, no solo el promedio.
- NER que cuenta sólo 5xx da 100% con un destino que no responde: el NER carrier suma 408 sin
  timbrar e INVITE sin respuesta.
- Disponibilidad: calcular sobre las 24 h persistidas e informar la cobertura; no rotular "24h"
  si la historia es más corta.
- RSS de Kamailio cuenta la SHM ×N procesos (7 GB falsos con 144 procs): usar PSS.
- Alarmas en el servidor (no en el navegador): sobreviven a recargas y cambios de turno.
- `core:drop_requests` incluye los ACK a 401/407 que `sl` absorbe: no alarmar sobre él con la
  sonda activa; usar "peticiones ignoradas" (exige retransmisión del cliente).
- Toda alarma de llamadas debe nombrar el origen/peer (IP, número, salto); si no, no es accionable.
- Los 403 no implican ataque: en un despliegue real eran ~1.100 ONT con 1 usuario por IP (cuentas
  restringidas en el registrar). Mirar Top usuarios/UA antes de bloquear IPs.
