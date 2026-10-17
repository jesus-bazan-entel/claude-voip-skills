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
| ASR | (Δprocessed − Δ`dialog:failed_dialogs`) / Δprocessed | <45% aviso, <30% major |
| NER | 1 − Δ`core:rcv_replies_5xx_invite` / Δprocessed (excluye busy/no-answer/cancel/decline) | <98% aviso, <95% minor, <90% major |
| ACD | Ley de Little: llamadas activas promedio ÷ (contestadas/s) | <30 s aviso |
| Registro exitoso | Δ`registrar:accepted_regs` / (accepted + rejected) | <99% aviso, <95% minor |
| PDD | captura pasiva: INVITE inicial → primer 180/183/2xx (ver abajo) | saliente prom >3 s minor, p95 >5 s major (≥10 llamadas); entrante prom >2 s minor |
| INVITE sin respuesta | ninguna respuesta tras Timer B (32 s) | ≥5 en 5 min minor |
| MOS por llamada | RTPEngine NG `query`: `average MOS` por SSRC, peor pierna | bandas G.107: ≥4.0 OK, ≥3.6 aviso, ≥3.1 mayor, <3.1 crítico; ≥5% de llamadas <3.6 en 15 min minor (≥15% major); ≥3 activas <3.6 minor |
| Disponibilidad 24h | % de muestras (cada 5 s) sin alarma critical (`samples.health_ok`) | <99.99% aviso, <99.9% crítico |
| Latencia SIP | RTT máx de OPTIONS reales a cada `sip_targets` | >50 ms aviso, >200 ms crítico |

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

## Plataforma (alarmas heredadas)

`/healthz` caído o liveness kamcmd → critical · BD inalcanzable → critical · OPTIONS sin
respuesta → major · destinos dispatcher caídos (salvo sets ignorados) → major ·
SHM ≥85% major / ≥70% minor · 5xx >0.5/s major · drops pike/antiflood >1/s minor ·
RTPEngine inactivo critical / userspace minor · DMQ peer no activo major · keepalived
caído major · CPU/RAM ≥90% major, ≥75/80% minor · disco ≥90% critical, ≥80% major ·
temperatura ≥80 °C major.
