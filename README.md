# claude-voip-skills

Skills de [Claude Code](https://claude.com/claude-code) para operar plataformas VoIP.

| Plugin | Skill | Qué hace |
|---|---|---|
| `kamailio-noc` | `kamailio-noc` | Despliega un dashboard NOC carrier-class en un proxy Kamailio: llamadas activas, registrados, CPS, ASR, NER, ACD, **PDD** (captura pasiva), **MOS por llamada** (RTPEngine NG), disponibilidad, alarmas por severidad, HA (DMQ/keepalived), RTPEngine y hardware. Sin dependencias (Python stdlib + JS vanilla) y sin tocar `kamailio.cfg`. |

## Instalación

Como marketplace de plugins (recomendado):

```
/plugin marketplace add jesus-bazan-entel/claude-voip-skills
/plugin install kamailio-noc@claude-voip-skills
```

O manual, copiando el skill a tu directorio de skills:

```bash
git clone https://github.com/jesus-bazan-entel/claude-voip-skills.git
cp -r claude-voip-skills/plugins/kamailio-noc/skills/kamailio-noc ~/.claude/skills/
```

## Uso

En el proxy Kamailio (como root), pedir a Claude por ejemplo:

> despliega el NOC de monitoreo en este proxy Kamailio

El skill guía el flujo: reconocimiento → `discover.py` (autodescubre listeners, dominio,
BD de usrloc, VIP, core, RTPEngine) → revisión de la config con el usuario → `install.sh`
→ `smoke_test.py` → publicación por nginx (con confirmación).

También se puede usar sin Claude:

```bash
cd plugins/kamailio-noc/skills/kamailio-noc
python3 scripts/discover.py --site "mi-operador"          # revisar
sudo bash scripts/install.sh --dir /opt/kamailio-noc --site "mi-operador"
sudo python3 scripts/smoke_test.py --dir /opt/kamailio-noc --wait 60
```

## Requisitos del proxy

Debian/Ubuntu con Kamailio 5.x (`kamcmd`, módulos `dialog`, `usrloc`, `registrar`;
`dispatcher`/`dmq` opcionales), Python ≥ 3.9, `tcpdump` (para el filtro BPF del PDD),
cliente `mysql` si usrloc usa MySQL, RTPEngine opcional (MOS), nginx con HTTPS para publicar.

## Seguridad

- Credenciales solo en `noc.conf.json` (600); MySQL vía `--defaults-extra-file`.
- Login con sesiones (PBKDF2-SHA256, bloqueo por fuerza bruta), CSP estricta, solo HTTPS.
- `.gitignore` excluye config, usuarios e histórico: no subas datos de un sitio al repo.
