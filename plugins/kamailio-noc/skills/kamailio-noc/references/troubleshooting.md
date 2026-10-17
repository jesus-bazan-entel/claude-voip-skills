# Troubleshooting

| Síntoma | Causa probable | Acción |
|---|---|---|
| "SIN DATOS (503)" al abrir | el colector aún no tiene la primera muestra | esperar 3 s |
| health.sip_ping False | `sip_domain` no es alias local, o se sondea la VIP en el BACKUP | revisar `alias=` y quitar la VIP de `sip_targets` |
| Kamailio /healthz DOWN pero Kamailio vivo | `kamailio_http` apunta a endpoints que no existen | poner `"kamailio_http": ""` |
| Sonda PDD "error: ... tcpdump" | falta tcpdump (compila el BPF) | `apt install tcpdump` |
| Sonda PDD "Operation not permitted" | servicio no corre como root (AF_PACKET) | `User=root` o `CAP_NET_RAW` |
| PDD vacío con tráfico | SIP por TCP/TLS o puertos distintos | ajustar `pdd.bpf`; TCP/TLS no soportado |
| Todas las llamadas salen "saliente" | `core_ips` vacío/incorrecto | poner la IP del FS/SBC que entrega las entrantes |
| Sonda MOS "error: timed out" | `rtpengine.ng` incorrecto o NG solo en otra IP | ver `listen-ng` en rtpengine.conf |
| MOS "sin RTCP" en muchas llamadas | los extremos no envían RTCP | esperable; no hay forma pasiva de MOS sin RTCP |
| Registrados = 0 | Kamailio sin usrloc en memoria o stat distinta | ver `kamcmd stats.get_statistics usrloc` |
| Early dialogs ≈ 1.8e19 | underflow del contador dialog | ya corregido: usar `dlg.stats_active` |
| Alarma dispatcher permanente | sets sin tráfico por diseño | `dispatcher_ignore_sets` (confirmar con el usuario) |
| Disponibilidad 24h "—" | columna nueva sin muestras aún | se llena sola en minutos |
| Cookie/login en bucle tras nginx | falta `X-Forwarded-Prefix /noc` o no es HTTPS | usar el snippet de `assets/nginx-noc.conf` |
