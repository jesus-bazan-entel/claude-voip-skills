# noc.conf.json

Fichero junto a `app.py` (o ruta en `NOC_CONFIG`), permisos 600. Claves ausentes toman el
valor por defecto de `DEFAULT_CONFIG` en `app.py`. Reiniciar `kamailio-noc` tras editar.

| Clave | Uso | Si no aplica |
|---|---|---|
| `site_name` | nombre mostrado en la cabecera | hostname |
| `sip_domain` | R-URI del OPTIONS de healthcheck (debe ser alias local → `200 Keepalive`/`200 OK`) | `localhost` |
| `sip_targets` | `[[ip, puerto], ...]` listeners UDP a sondear; **sin VIP** | `[]` = sin sonda OPTIONS |
| `kamailio_http` | base de los endpoints xhttp `/healthz` `/readyz` `/metrics` | `""` → liveness `kamcmd core.uptime`, readiness = BD |
| `db` | `{host, port, user, password, name}` MySQL de usrloc | `null` → sin conteo BD, health.db siempre OK |
| `ha.vip`, `ha.keepalived_service` | VIP keepalived (rol MASTER/BACKUP, alarma keepalived) | `vip: ""` → nodo único |
| `core_ips` | IPs del core: INVITE desde ellas = entrante (PDD) y se excluyen como "remoto" en MOS | `[]` → todo saliente |
| `dispatcher_ignore_sets` | sets visibles en tabla pero sin alarma ni conteo UP/total | `[]` |
| `rtpengine.ng` | `host:puerto` del `listen-ng` | `""` → sin MOS por llamada |
| `rtpengine.service` | unidad systemd (estado, logs) | `""` → RTPEngine oculto, sin alarmas |
| `rtpengine.kernel_table` | `table =` de rtpengine.conf (`/proc/rtpengine/<n>`) | 0 |
| `pdd.enabled`, `pdd.bpf` | sonda PDD y filtro BPF (puertos SIP) | `enabled: false` |
| `mos_bad` | umbral de MOS "malo" | 3.6 |
| `listen_port` | puerto local del servicio (`NOC_PORT` lo sobreescribe) | 8899 |
