#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# NAVIG Mini — Lightweight AI Agent Installer
# Works on:  Raspberry Pi · WD NAS · Synology · VPS · any POSIX + Python 3.6+
#
# Usage (one-liner):
#   wget -qO- https://navig.run/mini | sh
#   curl -fsSL https://navig.run/mini | sh
#
# Unattended (env vars):
#   MINI_TOKEN=<tg_token> MINI_CHAT=<chat_id> MINI_NO_WIZARD=1 sh install.sh
#
# Options via env:
#   MINI_DIR          Install directory (default: ~/navig-mini)
#   MINI_PORT         Agent port (default: 9191)
#   MINI_TOKEN        Telegram bot token
#   MINI_CHAT         Telegram chat ID
#   MINI_SECRET       HMAC secret (auto-generated if empty)
#   MINI_DISK_PATH    Disk path to monitor (default: /)
#   MINI_HOSTNAME     Device label in messages (default: hostname)
#   MINI_SKIP_CRON    1 = skip cron setup
#   MINI_NO_WIZARD    1 = non-interactive (use env vars)
#   INSTALL_BASE_URL  Override file download base URL
# ─────────────────────────────────────────────────────────────────────────────
set -e

NAVIG_MINI_VERSION="1.1.1"
INSTALL_BASE_URL="${INSTALL_BASE_URL:-https://raw.githubusercontent.com/navig-run/plugins/main/navig-mini}"

# ── Portability ───────────────────────────────────────────────────────────────
# BusyBox-safe: no set -o pipefail, no [[, no read -p, printf over echo -e
_say()  { printf '  %s\n' "$*"; }
_ok()   { printf '  \033[1;32m✓\033[0m  %s\n' "$*"; }
_warn() { printf '  \033[1;33m⚠\033[0m  %s\n' "$*"; }
_err()  { printf '  \033[1;31m✗\033[0m  %s\n' "$*" >&2; }
_ask()  { printf '  \033[0;36m?\033[0m  %s ' "$*"; }
_step() { printf '\n  \033[1;36m──\033[0m  %s\n' "$*"; }

_banner() {
    printf '\033[1;36m\n'
    printf '    ┌─┐  ┌┐┌┌─┐┬  ┬┬┌─┐  ┌┬┐┬┌┐┌┬\n'
    printf '    │││  ║║║├─┤└┐┌┘││ ┬  │││││││││\n'
    printf '    └─┘  ╝╚╝┴ ┴ └┘ ┴└─┘  ┴ ┴┴┘└┘┴─┘\n'
    printf '\033[0m\n'
    printf '  Lightweight AI agent for low-power devices  v%s\n' "$NAVIG_MINI_VERSION"
    printf '  10 KB daemon · stdlib only · zero deps · Telegram alerts\n\n'
}

# ── Prerequisites ─────────────────────────────────────────────────────────────
_find_python() {
    for candidate in python3 python /opt/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ver=$("$candidate" -c "import sys; print(sys.version_info.major*10+sys.version_info.minor)" 2>/dev/null)
            if [ -n "$ver" ] && [ "$ver" -ge 36 ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

_find_fetch() {
    if command -v curl >/dev/null 2>&1; then
        echo "curl -fsSL"
    elif command -v wget >/dev/null 2>&1; then
        echo "wget -qO-"
    elif command -v busybox >/dev/null 2>&1 && busybox wget --help >/dev/null 2>&1; then
        echo "busybox wget -qO-"
    else
        echo ""
    fi
}

_download() {
    url="$1"; dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    elif command -v busybox >/dev/null 2>&1; then
        busybox wget -qO "$dest" "$url"
    else
        _err "No download tool found (need curl or wget)"; exit 1
    fi
}

_gen_secret() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null && return
    fi
    # BusyBox fallback
    head -c 32 /dev/urandom | od -A n -t x1 | tr -d ' \n' | head -c 64
    echo
}

# ── Persistence: systemd vs cron ─────────────────────────────────────────────
_setup_systemd() {
    install_dir="$1"; python_cmd="$2"; port="$3"
    service_file="/etc/systemd/system/navig-mini.service"
    cat > "$service_file" << SVCEOF
[Unit]
Description=NAVIG Mini Agent v${NAVIG_MINI_VERSION}
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart=${python_cmd} ${install_dir}/agent.py
Restart=on-failure
RestartSec=10
WorkingDirectory=${install_dir}
StandardOutput=append:${install_dir}/agent.log
StandardError=append:${install_dir}/agent.log

[Install]
WantedBy=multi-user.target
SVCEOF
    systemctl daemon-reload
    systemctl enable navig-mini
    systemctl restart navig-mini
    _ok "systemd service enabled: navig-mini"
}

_setup_cron() {
    install_dir="$1"; python_cmd="$2"
    log_file="${install_dir}/agent.log"
    cron_agent="@reboot sleep 30 && nohup ${python_cmd} ${install_dir}/agent.py >> ${log_file} 2>&1 &"
    cron_monitor="*/5 * * * * ${python_cmd} ${install_dir}/monitor.py >> ${install_dir}/monitor.log 2>&1"

    # Preserve existing crontab, append new entries (idempotent)
    existing=$(crontab -l 2>/dev/null || true)
    new_crontab="${existing}"
    if printf '%s' "$existing" | grep -qF "navig-mini/agent.py"; then
        _warn "cron entry for agent already exists — skipping"
    else
        new_crontab="${new_crontab}
${cron_agent}"
        _ok "cron: agent @reboot added"
    fi
    if printf '%s' "$existing" | grep -qF "navig-mini/monitor.py"; then
        _warn "cron entry for monitor already exists — skipping"
    else
        new_crontab="${new_crontab}
${cron_monitor}"
        _ok "cron: monitor every 5 min added"
    fi
    printf '%s\n' "$new_crontab" | crontab -
}

# ── Write .env ────────────────────────────────────────────────────────────────
_write_env() {
    env_file="$1"
    cat > "$env_file" << ENVEOF
# NAVIG Mini — .env
# Auto-created by installer on $(date +%Y-%m-%d)
# Edit and restart agent: pkill -f agent.py && nohup python3 ${install_dir}/agent.py &

AGENT_PORT=${agent_port}
AGENT_SECRET=${agent_secret}
AGENT_HOSTNAME=${agent_hostname}
AGENT_DISK_PATH=${disk_path}

# Telegram alerts (optional — leave blank to disable)
TELEGRAM_TOKEN=${tg_token}
TELEGRAM_CHAT_ID=${tg_chat}

# Disk/RAM alert thresholds
DISK_ALERT_THRESHOLD=92
RAM_ALERT_THRESHOLD=50

# Services to watch (JSON: {"proc_name": "http_url_or_empty"})
# MONITOR_SERVICES={"nginx":"http://127.0.0.1:80","postgres":""}

# Watch directory for new file alerts (optional)
# MONITOR_WATCH_DIR=/home/user/Downloads
ENVEOF
    chmod 600 "$env_file"
}

# ── Telegram test ────────────────────────────────────────────────────────────
_tg_test() {
    token="$1"; chat="$2"; hostname="$3"
    if [ -z "$token" ] || [ -z "$chat" ]; then return 0; fi
    "$PYTHON_CMD" - << PYEOF
import urllib.request, urllib.parse, sys
token = "$token"
chat  = "$chat"
host  = "$hostname"
url   = f"https://api.telegram.org/bot{token}/sendMessage"
msg   = (
    f"✅ <b>NAVIG Mini</b> installed!\n"
    f"\n"
    f"📡 <code>{host}</code> — agent ready\n"
    f"🔗 <code>http://\$(hostname -I | awk '{{print \$1}}'):$agent_port</code>\n"
    f"📖 <code>navig-mini --help</code> for commands"
)
data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
try:
    urllib.request.urlopen(url, data=data, timeout=8)
    print("OK")
except Exception as e:
    print(f"WARN: {e}", file=sys.stderr)
PYEOF
}

# ── Download agent files ─────────────────────────────────────────────────────
_fetch_files() {
    install_dir="$1"
    _say "Downloading agent files..."
    _download "${INSTALL_BASE_URL}/agent.py"   "${install_dir}/agent.py"
    _download "${INSTALL_BASE_URL}/monitor.py" "${install_dir}/monitor.py"
    chmod +x "${install_dir}/agent.py" "${install_dir}/monitor.py"
    _ok "agent.py + monitor.py downloaded"
}

# ── Wizard ────────────────────────────────────────────────────────────────────
_wizard() {
    NO_WIZARD="${MINI_NO_WIZARD:-0}"

    install_dir="${MINI_DIR:-${HOME}/navig-mini}"
    agent_port="${MINI_PORT:-9191}"
    tg_token="${MINI_TOKEN:-}"
    tg_chat="${MINI_CHAT:-}"
    agent_secret="${MINI_SECRET:-}"
    disk_path="${MINI_DISK_PATH:-/}"
    agent_hostname="${MINI_HOSTNAME:-$(hostname 2>/dev/null || echo device)}"

    if [ "$NO_WIZARD" != "1" ]; then
        printf '\n  \033[1mConfiguration Wizard\033[0m\n'
        printf '  (press Enter to keep defaults)\n\n'

        _ask "Install directory [${install_dir}]:"; read -r ans
        [ -n "$ans" ] && install_dir="$ans"

        _ask "Agent port [${agent_port}]:"; read -r ans
        [ -n "$ans" ] && agent_port="$ans"

        _ask "Device label [${agent_hostname}]:"; read -r ans
        [ -n "$ans" ] && agent_hostname="$ans"

        _ask "Disk path to monitor [${disk_path}]:"; read -r ans
        [ -n "$ans" ] && disk_path="$ans"

        printf '\n  \033[2mTelegram alerts (optional — press Enter to skip)\033[0m\n'
        _ask "Telegram bot token:"; read -r ans
        [ -n "$ans" ] && tg_token="$ans"

        if [ -n "$tg_token" ]; then
            _ask "Telegram chat ID:"; read -r ans
            [ -n "$ans" ] && tg_chat="$ans"
        fi
    fi

    # Auto-generate secret if not set
    if [ -z "$agent_secret" ]; then
        agent_secret=$(_gen_secret)
    fi

    # Export for use in calling scope
    INSTALL_DIR="$install_dir"
    AGENT_PORT="$agent_port"
    AGENT_HOSTNAME="$agent_hostname"
    DISK_PATH="$disk_path"
    TG_TOKEN="$tg_token"
    TG_CHAT="$tg_chat"
    AGENT_SECRET="$agent_secret"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    _banner

    # --- Check Python
    _step "Checking prerequisites"
    PYTHON_CMD=$(_find_python) || { _err "Python 3.6+ not found. Install Python first."; exit 1; }
    _ok "Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

    # --- Wizard
    _step "Setup"
    _wizard
    install_dir="$INSTALL_DIR"
    agent_port="$AGENT_PORT"
    agent_secret="$AGENT_SECRET"
    agent_hostname="$AGENT_HOSTNAME"
    disk_path="$DISK_PATH"
    tg_token="$TG_TOKEN"
    tg_chat="$TG_CHAT"

    # --- Create directories
    _step "Creating install directory"
    mkdir -p "$install_dir"
    _ok "Directory: $install_dir"

    # --- Download or copy files
    _step "Installing files"
    _fetch_files "$install_dir"

    # --- Write .env
    _write_env "${install_dir}/.env"
    _ok ".env written (permissions: 600)"

    # --- Setup persistence
    _step "Setting up persistence"
    if [ "${MINI_SKIP_CRON:-0}" != "1" ]; then
        if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ] && systemctl --version >/dev/null 2>&1; then
            _setup_systemd "$install_dir" "$PYTHON_CMD" "$agent_port"
        elif crontab -l >/dev/null 2>&1 || true; then
            _setup_cron "$install_dir" "$PYTHON_CMD"
        else
            _warn "No systemd or cron found — start manually:"
            _say "  nohup $PYTHON_CMD $install_dir/agent.py &"
        fi
    else
        _warn "Skipping cron setup (MINI_SKIP_CRON=1)"
    fi

    # --- Start agent now
    _step "Starting agent"
    pkill -f "${install_dir}/agent.py" 2>/dev/null || true
    nohup "$PYTHON_CMD" "${install_dir}/agent.py" >> "${install_dir}/agent.log" 2>&1 < /dev/null &
    sleep 3

    # Verify HTTP
    if command -v wget >/dev/null 2>&1; then
        resp=$(wget -qO- "http://127.0.0.1:${agent_port}/ping" 2>/dev/null || echo "")
    elif command -v curl >/dev/null 2>&1; then
        resp=$(curl -sf "http://127.0.0.1:${agent_port}/ping" 2>/dev/null || echo "")
    else
        resp=""
    fi

    if printf '%s' "$resp" | grep -q '"ok"'; then
        _ok "Agent responding on :${agent_port}  — ${resp}"
    else
        _warn "Agent may need a moment — check: tail -20 ${install_dir}/agent.log"
    fi

    # --- Telegram test
    if [ -n "$tg_token" ] && [ -n "$tg_chat" ]; then
        _step "Testing Telegram"
        result=$(_tg_test "$tg_token" "$tg_chat" "$agent_hostname" 2>&1)
        if printf '%s' "$result" | grep -q "^OK"; then
            _ok "Telegram test message sent — check your phone!"
        else
            _warn "Telegram test: $result"
        fi
    fi

    # --- Summary
    printf '\n'
    printf '  \033[1;32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n'
    printf '  \033[1;32m  NAVIG Mini installed successfully!\033[0m\n'
    printf '  \033[1;32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n\n'
    printf '  Install dir:   %s\n' "$install_dir"
    printf '  Agent port:    %s\n' "$agent_port"
    printf '  Config file:   %s/.env\n\n' "$install_dir"
    printf '  Test:    wget -qO- http://127.0.0.1:%s/ping\n' "$agent_port"
    printf '  Stats:   wget -qO- http://127.0.0.1:%s/stats\n' "$agent_port"
    printf '  Logs:    tail -f %s/agent.log\n' "$install_dir"
    printf '  Stop:    pkill -f agent.py\n\n'
    printf '  To connect from NAVIG Big:\n'
    printf '    navig config set mini.url http://DEVICE_IP:%s\n' "$agent_port"
    printf '    navig config set mini.secret <your-secret>\n'
    printf '    navig mini status\n\n'
}

main "$@"
