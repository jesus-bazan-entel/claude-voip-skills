# KPIs, fuentes y umbrales

Ventana deslizante de **5 min** sobre contadores acumulados de `kamcmd stats.get_statistics all`
(`window_kpis()` en `app.py`). Los KPIs basados en ratios no alarman con <20 intentos.

## Servicio

| KPI | Fuente / fórmula | Estado / alarma |
|---|---|---|
| Llamadas activas | `dlg.stats_active` → `ongoing` | informativo; pico 24h; caída ≥50% en 2 min (≥20 llamadas) → major |
| Timbrando | `starting+connecting+answering` | — |
| Registrados | `usrloc:location_contacts` (memoria, 2 s); MySQL `location` cada 30 s | caída vs máx 10 min ≥5% major, ≥15% critical |
| CPS | Δ`dialog:processed_dialogs` / Δt | pico 24h |
| ASR | (Δprocessed − Δ`dialog:failed_dialogs`) / Δprocessed; comparado con la línea base | <30% major; ≤ −`baseline_sigma` σ bajo la base major |
| NER | carrier: 1 − (5xx + 408 sin timbrar + sin respuesta 32 s) / intentos, sobre respuestas finales de la sonda SIP. 486/487/480/404/603 y 408 tras timbrar son causas de usuario. Sin sonda: 1 − Δ`rcv_replies_5xx_invite` / Δprocessed | <98% aviso, <95% minor, <90% major |
| ACD | exacto: promedio 2xx → BYE por Call-ID, llamadas terminadas en 15 min (sonda SIP). Sin sonda: Ley de Little | <30 s aviso (≥5 llamadas) |
| Registro exitoso | Δ`registrar:accepted_regs` / (accepted + rejected) | <99% aviso, <95% minor |
| PDD | captura pasiva: INVITE inicial → primer 180/183/2xx (ver abajo) | saliente prom >3 s minor, p95 >5 s major (≥10 llamadas); entrante prom >2 s minor |
| INVITE sin respuesta | ninguna respuesta tras Timer B (32 s) | ≥5 en 5 min minor |
| MOS por llamada | RTPEngine NG `query`: `average MOS` por SSRC, peor pierna | bandas G.107: ≥4.0 OK, ≥3.6 aviso, ≥3.1 mayor, <3.1 crítico; ≥5% de llamadas <3.6 en 15 min minor (≥15% major); ≥3 activas <3.6 minor |
| CPS vs base | CPS de la ventana contra la línea base | ≤ −`baseline_sigma` σ major (si la base ≥0.2 CPS) |
| Disponibilidad 24h | % de muestras (cada 5 s) sin alarma critical en las últimas 24 h de SQLite. Huecos (NOC caído) no cuentan: se muestran cobertura e inicio | <99.99% aviso, <99.9% crítico |
| Calidad de voz | % de llamadas finalizadas (15 min) con MOS < `mos_bad`; cobertura RTCP; p50/p95 de jitter, pérdida y RTT | ≥5% aviso, ≥15% mayor |
| Latencia SIP | RTT máx de OPTIONS reales a cada `sip_targets` (en Elementos de red) | >50 ms aviso, >200 ms crítico |

## Línea base (tabla `kpi_5m`, retención 35 días)

Al cerrar cada cubeta de 5 min se guardan intentos, ASR, NER y CPS. Base = ±30 min de la misma
hora en las 4 semanas previas (≥6 cubetas con ≥20 intentos); si aún no hay, los 7 días previos
("diaria", mezcla días hábiles y fin de semana). σ con piso de 2 puntos (ASR) y
max(10% de la media, 0.02) (CPS) para no alarmar por ruido. Al instalar se rellena desde `samples`.

## Sonda SIP pasiva: respuestas, ACD, partes de la llamada y 403

- Respuesta final por INVITE (última CSeq; 401/407 no cuentan) → `/api/snapshot.outcomes`.
- ACD: `2xx` marca la contestación, el primer `BYE` del Call-ID cierra la llamada.
- Partes: From user, R-URI user, abonado (From si saliente, R-URI si entrante), User-Agent
  (del INVITE si saliente, de la 18x/2xx del cliente si entrante) → columnas en `call_quality`.
  Llamadas iniciadas antes de arrancar el NOC salen como "INVITE no capturado".
- 403 cuyo origen es una IP local (respuesta del proxy): IP destino, From user y método; el UA se
  toma de las siguientes peticiones de esa IP. Top 10 IP/UA/usuario en 15 min, cruzado con
  `kamcmd htable.dump ipban` y `fail2ban-client status` (cada 30 s).

## Peticiones ignoradas vs `core:drop_requests`

`core:drop_requests` **no** indica ataque por sí solo: el módulo `sl` cuenta como drop cada ACK
a una respuesta negativa sin estado (401/407 de autenticación digest, 403/404 con
`sl_send_reply`). En un proxy con auth de INVITE es ~1 drop por llamada desafiada. Medido:
19 drops en 60 s = 17 ACK a 407/401. El contador además no guarda el origen.

Por eso, con la sonda activa, la alarma usa **peticiones ignoradas**: la petición llega al
proxy, el cliente la **retransmite** y en 8 s no sale nada con su Call-ID+CSeq (ni respuesta ni
reenvío). Exigir la retransmisión descarta los paquetes que pierde la propia sonda (sin eso
salían ~0,7 REGISTER/s falsos). Alarma minor ≥1/s en 5 min con IP, método, usuario y UA
principales. Los "ACK a 401/407 absorbidos" se muestran como explicación del contador.
Sin sonda: alarma genérica sobre `drop_requests` promediado en 5 min (no cada 2 s, que oscila).

Socket de captura con `SO_RCVBUFFORCE` 16 MB; las pérdidas del kernel (`PACKET_STATISTICS`)
se muestran en "Sonda PDD".

## Origen / peer en las alarmas de llamadas

- 5xx sostenidos y NER: IP que generó la respuesta (o `proxy`) y código, top 3.
- INVITE sin respuesta: IP a la que el proxy reenvió el INVITE (siguiente salto) y dirección.
- PDD: las 3 llamadas más lentas con From → R-URI y el salto que dio el ringback.
- Estas alarmas tienen clave fija (`key`), así el texto puede cambiar sin abrir otra alarma.

## Alarmas (tabla `alarms`, retención 30 días)

Clave = mensaje con números normalizados. Alta inmediata; severidad sólo escala (escalar
borra el ack); limpieza tras 60 s sin detectarse. Al reiniciar el NOC se restauran las activas
con 60 s extra de gracia. `POST /api/alarms/ack {"id":N}` exige sesión y
`Content-Type: application/json` (anti-CSRF). `GET /api/alarms?hours=N` = historial.

## PDD (sonda pasiva)

- `AF_PACKET` + filtro BPF compilado con `tcpdump -ddd "<pdd.bpf>"` y adjuntado con
  `SO_ATTACH_FILTER`: el kernel descarta RTP; a Python solo llega SIP (~3k pkt/s ≈ 4% de un core).
- Correlación por Call-ID + CSeq. Re-INVITEs (To con tag) se ignoran.
- 401/407 → se espera el reintento con nuevo CSeq y se mide desde ahí.
- Fallo final (486, 404, 503…) antes de timbrar → sin muestra de PDD.
- Dirección: IP origen del primer INVITE ∈ `core_ips` → entrante; si no, saliente.
- Limitación: solo SIP/UDP (no TCP/TLS) y primer fragmento IP (cabeceras SIP caben).

## MOS por llamada

- Cada 10 s: `list` + `query` por Call-ID al socket NG (bencode/UDP). ~100 llamadas en 60 ms.
- Por llamada se guarda la peor pierna: MOS promedio, MOS mínimo, jitter (ms), pérdida (%),
  RTT (ms), IP remota (primer endpoint fuera de `core_ips`), códec, paquetes RTP.
- Al desaparecer del `list` se persiste en `call_quality` (retención 7 días) → `/api/calls`.
- Duración = desde el `offer` de RTPEngine (incluye timbrado). Detección de fin ≤10 s tarde.
- Con G.711 el MOS de RTPEngine se satura en 4,3: el dashboard prioriza % bajo umbral,
  cobertura RTCP y percentiles de jitter/pérdida/RTT.

## Plataforma (alarmas heredadas)

`/healthz` caído o liveness kamcmd → critical · BD inalcanzable → critical · OPTIONS sin
respuesta → major · destinos dispatcher caídos (salvo sets ignorados) → major ·
SHM (`real_used`) ≥85% major / ≥70% minor · 5xx >0.5/s major · drops pike/antiflood >1/s minor ·
RTPEngine inactivo critical / userspace minor · DMQ peer no activo major · keepalived
caído major · CPU/RAM ≥90% major, ≥75/80% minor · disco ≥90% critical, ≥80% major ·
temperatura ≥80 °C major.

## Memoria de Kamailio

RSS suma la SHM mapeada en cada proceso (×N procesos) y la infla. Se usa PSS de
`/proc/<pid>/smaps_rollup` separando `Pss_Shmem` (compartida) de la privada, más el PKG en uso
(suma de `real_used` de `kamcmd pkg.stats`). Se refresca cada 30 s.
