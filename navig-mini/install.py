#!/usr/bin/env python3
"""
NAVIG Mini — Python Installer
Pure stdlib, Python 3.6+. Same as install.sh but Python-native.
Works on any system with Python even without bash.

Usage:
    python3 install.py                                # interactive
    python3 install.py --no-wizard                   # use env vars
    python3 <(wget -qO- https://navig.run/mini.py)  # one-liner

Environment:
    MINI_DIR, MINI_PORT, MINI_TOKEN, MINI_CHAT, MINI_SECRET,
    MINI_DISK_PATH, MINI_HOSTNAME, MINI_SKIP_CRON, MINI_NO_WIZARD
"""
import os, sys, json, subprocess, socket, time, shutil, stat
from pathlib import Path
from urllib.request import urlretrieve, urlopen
from urllib.parse  import urlencode
from urllib.error  import URLError

VERSION      = "1.1.1"
# Standalone-bootstrap source. Files (agent.py, monitor.py) are fetched from the
# public plugin mirror; override with INSTALL_BASE_URL for a private/dev source.
BASE_URL     = os.environ.get("INSTALL_BASE_URL",
               "https://raw.githubusercontent.com/navig-run/plugins/main/navig-mini")

# ── Terminal helpers ───────────────────────────────────────────────────────────
IS_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if IS_TTY else s
def _bold(s):  return _c("1",    s)
def _green(s): return _c("1;32", s)
def _cyan(s):  return _c("1;36", s)
def _yellow(s):return _c("1;33", s)
def _red(s):   return _c("1;31", s)

def ok(msg):   print(f"  {_green('✓')}  {msg}")
def warn(msg): print(f"  {_yellow('⚠')}  {msg}")
def err(msg):  print(f"  {_red('✗')}  {msg}", file=sys.stderr)
def step(msg): print(f"\n  {_cyan('──')}  {_bold(msg)}")
def say(msg):  print(f"  {msg}")

def ask(prompt, default=""):
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {_cyan('?')}  {prompt}{hint}: ").strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        return default

def banner():
    print(_cyan(f"""
    ╔╗╔┌─┐┬  ┬┬┌─┐  ╔╦╗┬┌┐┌┬
    ║║║├─┤└┐┌┘││ ┬  ║║║││││││
    ╝╚╝┴ ┴ └┘ ┴└─┘  ╩ ╩┴┘└┘┴─┘"""))
    print(f"\n  Lightweight AI agent — v{VERSION}")
    print("  stdlib only · zero deps · Telegram alerts\n")

# ── Utilities ──────────────────────────────────────────────────────────────────
def find_python():
    """Return best python3 path for this device."""
    candidates = [sys.executable, "python3", "python",
                  "/opt/bin/python3", "/usr/local/bin/python3"]
    for c in candidates:
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return sys.executable

def gen_secret():
    import secrets
    return secrets.token_hex(32)

def download(url, dest):
    try:
        urlretrieve(url, dest)
        return True
    except Exception as exc:
        warn(f"Download failed: {exc}")
        return False

def has_systemd():
    return (shutil.which("systemctl") is not None and
            Path("/etc/systemd/system").is_dir())

def has_cron():
    return shutil.which("crontab") is not None

# ── Persistence ────────────────────────────────────────────────────────────────
def setup_systemd(install_dir, python_cmd, port):
    svc = f"""[Unit]
Description=NAVIG Mini Agent v{VERSION}
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart={python_cmd} {install_dir}/agent.py
Restart=on-failure
RestartSec=10
WorkingDirectory={install_dir}
StandardOutput=append:{install_dir}/agent.log
StandardError=append:{install_dir}/agent.log

[Install]
WantedBy=multi-user.target
"""
    svc_path = Path("/etc/systemd/system/navig-mini.service")
    svc_path.write_text(svc, encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "navig-mini"], check=True)
    subprocess.run(["systemctl", "restart", "navig-mini"], check=True)
    ok("systemd service 'navig-mini' enabled and started")

def setup_cron(install_dir, python_cmd):
    log   = f"{install_dir}/agent.log"
    mlog  = f"{install_dir}/monitor.log"
    e_agent   = f"@reboot sleep 30 && nohup {python_cmd} {install_dir}/agent.py >> {log} 2>&1 &"
    e_monitor = f"*/5 * * * * {python_cmd} {install_dir}/monitor.py >> {mlog} 2>&1"

    try:
        existing = subprocess.check_output(["crontab", "-l"],
                                           stderr=subprocess.DEVNULL,
                                           text=True)
    except subprocess.CalledProcessError:
        existing = ""

    new = existing.rstrip("\n")
    added = []
    if f"{install_dir}/agent.py" not in existing:
        new += f"\n{e_agent}"
        added.append("agent @reboot")
    else:
        warn("cron: agent already present — skipped")
    if f"{install_dir}/monitor.py" not in existing:
        new += f"\n{e_monitor}"
        added.append("monitor */5")
    else:
        warn("cron: monitor already present — skipped")

    subprocess.run(["crontab", "-"], input=new + "\n", text=True, check=True)
    for a in added:
        ok(f"cron: {a} added")

# ── Wizard ─────────────────────────────────────────────────────────────────────
def wizard():
    no_wizard = os.environ.get("MINI_NO_WIZARD", "0") == "1"
    cfg = {
        "install_dir":  os.environ.get("MINI_DIR",       str(Path.home() / "navig-mini")),
        "port":         os.environ.get("MINI_PORT",       "9191"),
        "hostname":     os.environ.get("MINI_HOSTNAME",   socket.gethostname()),
        "disk_path":    os.environ.get("MINI_DISK_PATH",  "/"),
        "tg_token":     os.environ.get("MINI_TOKEN",      ""),
        "tg_chat":      os.environ.get("MINI_CHAT",       ""),
        "secret":       os.environ.get("MINI_SECRET",     ""),
    }

    if not no_wizard:
        print(f"\n  {_bold('Configuration Wizard')}")
        print("  (press Enter to keep defaults)\n")
        cfg["install_dir"] = ask("Install directory",  cfg["install_dir"])
        cfg["port"]        = ask("Agent port",         cfg["port"])
        cfg["hostname"]    = ask("Device label",       cfg["hostname"])
        cfg["disk_path"]   = ask("Disk path to monitor", cfg["disk_path"])
        print(f"\n  {_c('2','Telegram alerts — press Enter to skip')}")
        cfg["tg_token"]    = ask("Telegram bot token", cfg["tg_token"])
        if cfg["tg_token"]:
            cfg["tg_chat"] = ask("Telegram chat ID",   cfg["tg_chat"])

    if not cfg["secret"]:
        cfg["secret"] = gen_secret()

    return cfg

# ── .env writer ────────────────────────────────────────────────────────────────
def write_env(cfg):
    env_path = Path(cfg["install_dir"]) / ".env"
    content  = f"""# NAVIG Mini — .env
# Auto-created {time.strftime('%Y-%m-%d')}  (edit and restart agent to apply)

AGENT_PORT={cfg['port']}
AGENT_SECRET={cfg['secret']}
AGENT_HOSTNAME={cfg['hostname']}
AGENT_DISK_PATH={cfg['disk_path']}

TELEGRAM_TOKEN={cfg['tg_token']}
TELEGRAM_CHAT_ID={cfg['tg_chat']}

DISK_ALERT_THRESHOLD=92
RAM_ALERT_THRESHOLD=50

# MONITOR_SERVICES={{"nginx":"http://127.0.0.1:80","postgres":""}}
# MONITOR_WATCH_DIR=/home/user/Downloads
"""
    env_path.write_text(content, encoding="utf-8")
    env_path.chmod(0o600)
    ok(f".env written → {env_path} (mode 600)")

# ── Telegram test ──────────────────────────────────────────────────────────────
def tg_test(token, chat, hostname, port):
    if not token or not chat:
        return
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "device"
    msg = (
        f"✅ <b>NAVIG Mini</b> installed!\n\n"
        f"📡 <code>{hostname}</code>\n"
        f"🔗 <code>http://{ip}:{port}</code>\n"
        f"🔐 HMAC-auth · agent ready"
    )
    data = urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
    try:
        urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                data=data, timeout=8)
        ok("Telegram test message sent — check your phone!")
    except URLError as exc:
        warn(f"Telegram test failed: {exc}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    banner()
    python_cmd = find_python()

    step("Setup")
    cfg = wizard()
    install_dir = Path(cfg["install_dir"])

    step("Creating directory")
    install_dir.mkdir(parents=True, exist_ok=True)
    ok(f"Directory: {install_dir}")

    step("Downloading files")
    for fname in ("agent.py", "monitor.py"):
        url  = f"{BASE_URL}/{fname}"
        dest = install_dir / fname
        say(f"  {url}")
        if download(url, str(dest)):
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
            ok(f"{fname} installed")
        else:
            err(f"Failed to download {fname} — check URL or install manually")
            sys.exit(1)

    write_env(cfg)

    step("Setting up persistence")
    skip_cron = os.environ.get("MINI_SKIP_CRON", "0") == "1"
    if skip_cron:
        warn("Skipping persistence setup (MINI_SKIP_CRON=1)")
    elif has_systemd():
        try:    setup_systemd(str(install_dir), python_cmd, cfg["port"])
        except Exception as exc: warn(f"systemd setup failed: {exc} — falling back to cron")
    elif has_cron():
        setup_cron(str(install_dir), python_cmd)
    else:
        warn("No systemd or cron — start manually:")
        say(f"  nohup {python_cmd} {install_dir}/agent.py &")

    step("Starting agent")
    subprocess.run(["pkill", "-f", f"{install_dir}/agent.py"],
                   capture_output=True)
    subprocess.Popen(
        [python_cmd, str(install_dir / "agent.py")],
        stdout=open(str(install_dir / "agent.log"), "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(3)

    # Verify HTTP
    try:
        resp = json.loads(urlopen(
            f"http://127.0.0.1:{cfg['port']}/ping", timeout=4).read())
        ok(f"Agent responding on :{cfg['port']}  (uptime {resp.get('uptime',0)}s)")
    except Exception as exc:
        warn(f"Agent may need a moment: {exc}")
        say(f"  tail -20 {install_dir}/agent.log")

    if cfg["tg_token"] and cfg["tg_chat"]:
        step("Testing Telegram")
        tg_test(cfg["tg_token"], cfg["tg_chat"], cfg["hostname"], cfg["port"])

    # Summary
    print(f"\n  {_green('━' * 49)}")
    print(f"  {_green('  NAVIG Mini installed successfully!')}")
    print(f"  {_green('━' * 49)}\n")
    print(f"  Dir:    {install_dir}")
    print(f"  Port:   {cfg['port']}")
    print(f"  Config: {install_dir}/.env\n")
    print(f"  Test:   wget -qO- http://127.0.0.1:{cfg['port']}/ping")
    print(f"  Stats:  wget -qO- http://127.0.0.1:{cfg['port']}/stats")
    print(f"  Logs:   tail -f {install_dir}/agent.log")
    print(f"  Stop:   pkill -f agent.py\n")
    print("  Connect from NAVIG Big:")
    print(f"    navig config set mini.url http://DEVICE_IP:{cfg['port']}")
    print( "    navig config set mini.secret <your-secret>")
    print( "    navig mini status\n")

if __name__ == "__main__":
    main()
