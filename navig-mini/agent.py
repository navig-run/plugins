#!/usr/bin/env python3
"""
NAVIG Mini — Agent Daemon
Lightweight HMAC-authenticated HTTP command agent for low-power devices.
Works on: Raspberry Pi, WD NAS, NAS boxes, cheap VPS, any Python 3.6+ system.
Zero external dependencies — stdlib only.

Deploy:    python3 agent.py
Install:   curl -fsSL https://navig.run/mini | sh
PyPI:      pip install navig-mini && navig-mini start

Endpoints:
  GET  /          health + uptime + stats
  GET  /ping      lightweight health check
  GET  /stats     detailed system stats (JSON)
  POST /          run allowlisted command  [X-Agent-Sig: HMAC-SHA256]
  POST /run       alias for POST /
"""
import os
import json
import shlex
import subprocess
import hashlib
import hmac as _hmac
import time
import threading
import socket
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Version ────────────────────────────────────────────────────────────────────
VERSION = "1.1.1"

# ── Load .env (stdlib only) ────────────────────────────────────────────────────
def _load_env():
    for candidate in [Path(__file__).parent / ".env", Path.home() / ".navig-mini" / ".env"]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split("#")[0].strip())
            break
_load_env()

# ── Config (all from .env / environment) ───────────────────────────────────────
_DEFAULT_SECRET  = "changeme-set-in-env"
SECRET           = os.environ.get("AGENT_SECRET", _DEFAULT_SECRET)
PORT             = int(os.environ.get("AGENT_PORT", "9191"))
BIND             = os.environ.get("AGENT_BIND", "0.0.0.0")  # set 127.0.0.1 for local-only
HOSTNAME_LABEL   = os.environ.get("AGENT_HOSTNAME", socket.gethostname())
DISK_PATH        = os.environ.get("AGENT_DISK_PATH", "/")
EXTRA_ROOT       = os.environ.get("NAS_ROOT", "")        # optional extra allowed path
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Watchdog services: JSON string → {"proc_name": "restart_command", ...}
# e.g. AGENT_WATCHDOG='{"gitea":"sh /data/start-gitea.sh","aria2c":"aria2c -D"}'
_watchdog_raw = os.environ.get("AGENT_WATCHDOG", "{}")
try:
    WATCHDOG_SERVICES = json.loads(_watchdog_raw)
except Exception:
    WATCHDOG_SERVICES = {}

# Extra safe command prefixes (space-separated)
_extra_prefixes = os.environ.get("AGENT_SAFE_PREFIXES_EXTRA", "").split()

_LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}


def _secret_is_weak() -> bool:
    """True when the HMAC secret is a known placeholder, empty, or too short to
    resist guessing. Exposing the command endpoint on a network interface with a
    weak secret is effectively an unauthenticated RCE — any placeholder from the
    public source or `.env.example` (all start with ``changeme``) could be signed
    by anyone on the network."""
    return (
        (not SECRET)
        or SECRET == _DEFAULT_SECRET
        or SECRET.lower().startswith("changeme")
        or len(SECRET) < 16
    )

# ── Telegram ───────────────────────────────────────────────────────────────────
def tg(msg: str):
    if not TELEGRAM_TOKEN:
        return
    import urllib.request
    import urllib.parse
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    msg,
        "parse_mode": "HTML",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=8)
    except Exception:
        pass

# ── System stats ───────────────────────────────────────────────────────────────
def system_stats() -> dict:
    s = {
        "hostname":   HOSTNAME_LABEL,
        "uptime_sec": int(time.time() - START_TIME),
        "ram_free_mb": "?", "ram_total_mb": "?",
        "disk_pct": "?", "disk_free": "?", "disk_path": DISK_PATH,
        "load": "?", "python": "?",
    }
    # RAM via /proc/meminfo (works on BusyBox; no -m flag bug)
    try:
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")
        s["ram_free_mb"]  = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1)) // 1024
        s["ram_total_mb"] = int(re.search(r"MemTotal:\s+(\d+)",     mem).group(1)) // 1024
    except Exception:
        pass
    # Disk using df
    try:
        df = subprocess.check_output(["df", DISK_PATH], text=True, timeout=5).splitlines()
        parts = df[1].split()
        s["disk_pct"]  = parts[4]
        s["disk_free"] = str(int(parts[3]) // 1024) + "M"
    except Exception:
        pass
    # Load average
    try:
        s["load"] = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
    except Exception:
        pass
    # Python version
    import sys
    s["python"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return s

# ── Command allowlist ──────────────────────────────────────────────────────────
_BASE_SAFE_PREFIXES = [
    "df ", "free", "ls ", "cat ", "tail ", "head ", "wc ", "du ",
    "pgrep", "ps ", "top ", "uptime", "uname", "date", "hostname",
    "ping ", "netstat", "ss ", "ifconfig", "ip ",
    "find ", "grep ",
    "yt-dlp", "aria2c",
    "kill ",  "pkill ",
    "wget ",  "curl ",
    "/usr/bin/", "/usr/local/bin/", "/opt/bin/",
]
if EXTRA_ROOT:
    _BASE_SAFE_PREFIXES += [f"sh {EXTRA_ROOT}/", f"nohup {EXTRA_ROOT}/"]
SAFE_PREFIXES = _BASE_SAFE_PREFIXES + _extra_prefixes

def is_safe(cmd: str) -> bool:
    c = cmd.strip()
    return any(c.startswith(p) for p in SAFE_PREFIXES)


def run_allowlisted(cmd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run an already-allowlisted command as an ARGV, with NO shell — so a command
    that merely *starts* with a safe prefix can't chain to an unlisted one
    (``ls / ; rm -rf …``, ``curl URL | sh``, ``$(…)``, backticks). The prefix
    allowlist + ``shell=True`` was a trivial RCE for anyone holding the HMAC
    secret. A pipeline that genuinely needs a shell goes through an allowlisted
    EXTRA_ROOT script (``sh /nas/foo.sh``) — its own shell lives inside the script.

    Raises ValueError (from shlex.split) on unbalanced quotes; the caller maps
    that to a 400.
    """
    proc = subprocess.run(
        shlex.split(cmd), shell=False, capture_output=True, timeout=timeout
    )
    # getattr throughout, matching navig.core.proc_text.decode_console_result: a real
    # CompletedProcess carries all four, but tests fake `subprocess.run` with minimal
    # stand-ins and an AttributeError here would be raised for a field only passed through.
    return subprocess.CompletedProcess(
        getattr(proc, "args", None),
        getattr(proc, "returncode", 0),
        _decode(getattr(proc, "stdout", None)),
        _decode(getattr(proc, "stderr", None)),
    )


def _decode(raw) -> str:
    """Child output -> str: UTF-8 strictly, then the console code page. Never raises.

    `text=True` would decode with the locale code page, which is right for neither a
    UTF-8-emitting tool nor a Windows console tool. Written out rather than imported
    from `navig.core.proc_text` because this agent is deliberately ZERO-DEPENDENCY,
    stdlib-only: it is deployed as a bare `python3 agent.py` onto a Pi or a NAS where
    navig itself is not installed.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(raw).decode("oem" if os.name == "nt" else "utf-8", errors="replace")

# ── HTTP Handler ───────────────────────────────────────────────────────────────
class AgentHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/ping":
            self._json(200, {"status": "ok", "uptime": int(time.time() - START_TIME)})
        elif path == "/stats":
            self._json(200, {"status": "ok", "version": VERSION, **system_stats()})
        else:
            self._json(200, {
                "status": "ok",
                "version": VERSION,
                "uptime": int(time.time() - START_TIME),
                "hostname": HOSTNAME_LABEL,
            })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        # HMAC-SHA256 auth
        sig      = self.headers.get("X-Agent-Sig", "")
        expected = _hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            self._json(403, {"error": "forbidden: invalid signature"})
            return

        try:
            data = json.loads(body)
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return

        # Support both /run and / endpoints
        cmd = data.get("cmd", "").strip()
        if not cmd:
            self._json(400, {"error": "missing 'cmd'"})
            return

        if not is_safe(cmd):
            self._json(403, {"error": f"command not in allowlist: {cmd!r}"})
            return

        try:
            timeout = int(data.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        try:
            result = run_allowlisted(cmd, timeout)
            self._json(200, {
                "cmd":    cmd,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "code":   result.returncode,
            })
        except ValueError:  # shlex.split: unbalanced quotes
            self._json(400, {"error": "malformed command (unbalanced quotes)"})
        except subprocess.TimeoutExpired:
            self._json(408, {"error": "command timed out"})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # suppress default access log
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

# ── Watchdog thread ────────────────────────────────────────────────────────────
def _watchdog_loop():
    time.sleep(120)  # grace period after boot
    while True:
        for proc_name, restart_cmd in WATCHDOG_SERVICES.items():
            try:
                r = subprocess.run(
                    ["pgrep", "-c", proc_name],
                    capture_output=True, text=True
                )
                if r.stdout.strip() == "0":
                    print(f"[watchdog] {proc_name} is down — restarting")
                    subprocess.Popen(restart_cmd, shell=True)
                    tg(f"🔄 <b>NAVIG Mini</b> auto-restarted <code>{proc_name}</code>")
            except Exception as exc:
                print(f"[watchdog] error checking {proc_name}: {exc}")
        time.sleep(60)

# ── Entry point ────────────────────────────────────────────────────────────────
START_TIME = time.time()

if __name__ == "__main__":
    if WATCHDOG_SERVICES:
        threading.Thread(target=_watchdog_loop, daemon=True).start()

    print(f"[navig-mini] v{VERSION} — {HOSTNAME_LABEL} — :{PORT}")
    print(f"[navig-mini] disk path: {DISK_PATH}  watchdog services: {list(WATCHDOG_SERVICES)}")
    print(f"[navig-mini] safe prefixes: {len(SAFE_PREFIXES)}  secret: {'*' * 8}")

    # Boot notification
    if TELEGRAM_TOKEN:
        s = system_stats()
        tg(
            f"🟢 <b>NAVIG Mini</b> v{VERSION} is online\n"
            f"\n"
            f"📡 <code>{HOSTNAME_LABEL}</code> · port <code>{PORT}</code>\n"
            f"🧠 RAM <code>{s['ram_free_mb']} MB</code> free / {s['ram_total_mb']} MB\n"
            f"💾 Disk <code>{s['disk_pct']}</code> used · {s['disk_free']} free\n"
            f"🔐 HMAC-auth · <i>agent ready</i>"
        )

    # Refuse to expose an authenticated command endpoint on a network interface
    # while the HMAC secret is weak/default — that would be a network-reachable RCE.
    if _secret_is_weak() and BIND not in _LOOPBACK_BINDS:
        print(
            f"[navig-mini] FATAL: AGENT_SECRET is unset, the shipped default, or under "
            f"16 chars — refusing to expose the command endpoint on non-loopback "
            f"{BIND}:{PORT}. Set a strong AGENT_SECRET in .env, or AGENT_BIND=127.0.0.1 "
            f"for local-only."
        )
        raise SystemExit(2)

    try:
        print(f"[navig-mini] listening on {BIND}:{PORT}  (Ctrl+C to stop)")
        HTTPServer((BIND, PORT), AgentHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n[navig-mini] stopped")
