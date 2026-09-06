# NAVIG Mini

Lightweight autonomous agent for low-power devices: Raspberry Pi, NAS, VPS, embedded Linux.

Zero mandatory dependencies — runs on any Python 3.6+ with only the stdlib.

---

## Repository Structure

```
navig-mini/
├── agent.py          # HTTP daemon (port 9191): /ping /stats /run /
├── monitor.py        # Cron-based Telegram alert monitor
├── install.sh        # curl|sh installer (BusyBox POSIX sh / bash / dash)
├── install.py        # stdlib Python installer wizard (fallback for pure-Python platforms)
├── pyproject.toml    # PyPI package definition (pip install navig-mini)
├── .env.example      # Full environment variable reference
├── CURRENT_PHASE.md  # Active development phase tracker
├── DEV_PLAN.md       # Full development plan
└── navig_mini/
    ├── __init__.py   # Package init — version 1.1.0
    └── cli.py        # navig-mini CLI: init/start/stop/restart/status/logs/version
```

NAVIG Big controller plugin (for `navig mini *` commands):
`navig-core/navig/plugins/navig_mini/plugin.py`

---

## 3 Installation Methods

### Method 1 — `curl | sh`  *(preferred on NAS / Pi / embedded)*

```sh
wget -qO- https://navig.run/mini | sh
# or
curl -fsSL https://navig.run/mini | sh

# Unattended (CI / provisioning)
MINI_NO_WIZARD=1 MINI_TOKEN=<bot_token> MINI_CHAT=<chat_id> sh install.sh

# Custom directory / airgapped
MINI_SKIP_CRON=1 MINI_DIR=/opt/navig-mini INSTALL_BASE_URL=http://local.server sh install.sh
```

Installs to `~/navig-mini/`. Persistence: systemd → cron @reboot → manual fallback.

### Method 2 — `pip install`  *(any machine with Python 3.6+)*

```sh
pip install navig-mini                           # from PyPI
pip install -e E:/projects/apps/navig/navig-mini    # dev install (this repo)

navig-mini init       # interactive wizard — creates .env in ~/navig-mini/
                      # (from a wheel this fetches the wizard from the plugins mirror;
                      #  set INSTALL_BASE_URL to use your own)
navig-mini start
navig-mini stop | restart | status | logs | version
```

### Method 3 — `navig mini *`  *(from NAVIG Big, controls remote device via HTTP/SSH)*

```sh
navig config set mini.url    http://nas.local:9191
navig config set mini.secret <hmac_secret>
navig config set mini.ssh_host my-nas

navig mini status             # show remote agent health + stats table
navig mini run "df -h"        # HMAC-signed command execution
navig mini deploy             # SSH-upload agent.py + monitor.py → restart
navig mini logs [-n 100]      # tail agent.log via SSH
navig mini restart [service]  # restart agent/monitor/named process
navig mini list --check       # list all agents with live HTTP ping
```

---

## Agent HTTP API

| Endpoint | Method | Auth        | Description              |
|----------|--------|-------------|--------------------------|
| `/`      | GET    | none        | Version, hostname, uptime |
| `/ping`  | GET    | none        | Lightweight health check `{status, uptime}` |
| `/stats` | GET    | none        | Full system stats JSON   |
| `/run`   | POST   | HMAC-SHA256 | Execute allowlisted command |

### `/stats` example response

```json
{
  "version":      "1.1.0",
  "hostname":     "nas-wd",
  "uptime_sec":   3600,
  "ram_free_mb":  512,
  "ram_total_mb": 1024,
  "disk_pct":     "42%",
  "disk_free":    "21G",
  "disk_path":    "/",
  "load":         [0.1, 0.2, 0.1],
  "python":       "3.9.2"
}
```

### `/run` POST body

```json
{"cmd": "df -h", "timeout": 30}
```
Header: `X-Agent-Sig: <hmac-sha256-hex-digest-of-body>`

Commands must match the allowlist. Extra prefixes: `AGENT_SAFE_PREFIXES_EXTRA`.

---

## Environment Variables

| Variable                    | Default                  | Description |
|-----------------------------|--------------------------|-------------|
| `AGENT_PORT`                | `9191`                   | HTTP port |
| `AGENT_SECRET`              | *(required)*             | HMAC signing key |
| `AGENT_DISK_PATH`           | `/`                      | Disk path to monitor |
| `AGENT_HOSTNAME`            | `socket.gethostname()`   | Reported hostname |
| `AGENT_WATCHDOG`            | `{}`                     | JSON `{"proc":"restart_cmd"}` service watchdog map |
| `AGENT_SAFE_PREFIXES_EXTRA` | ``                       | Space-separated extra allowlisted command prefixes |
| `TELEGRAM_BOT_TOKEN`        | *(required for monitor)* | Bot token |
| `TELEGRAM_CHAT_ID`          | *(required for monitor)* | Chat ID to send alerts to |
| `MONITOR_SERVICES`          | `{}`                     | JSON service health config |
| `MONITOR_WATCH_DIR`         | ``                       | Directory to alert on new files (e.g. Downloads) |
| `MONITOR_DISK_WARN_PCT`     | `85`                     | Disk warning threshold (%) |
| `MONITOR_RAM_WARN_MB`       | `50`                     | RAM warning threshold (MB) |

See [.env.example](.env.example) for the complete annotated reference.

---

## Security

- All `/run` requests require `X-Agent-Sig: HMAC-SHA256(secret, body)`
- Strict command allowlist — cannot execute arbitrary shell
- Binds to `0.0.0.0` — firewall port 9191 on public-facing hosts

---

## Multi-Agent Setup

```sh
navig config set mini.agents '[
  {"name":"nas",  "url":"http://nas.local:9191", "ssh_host":"my-nas"},
  {"name":"pi4",  "url":"http://pi4.local:9191", "ssh_host":"pi4"}
]'
navig mini list --check
```

---

## Target Devices

- WD My Cloud (armv7l, 509MB RAM, BusyBox ash)
- Raspberry Pi 3/4/5
- Cheap VPS (512MB RAM)
- Any embedded Linux with Python 3.6+
