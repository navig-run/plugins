#!/usr/bin/env python3
"""
NAVIG Mini — Monitor
Cron-based Telegram alert bot. Run every 5 min:
    */5 * * * * python3 /path/to/monitor.py

Alerts on: disk full, low RAM, service down, download activity.
All config from .env in same directory (or NAVIG Mini standard location).
Zero external dependencies — stdlib only.
"""
import subprocess
import os
import json
import time
import re
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import URLError

# ── Load .env ──────────────────────────────────────────────────────────────────
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

# ── Config ─────────────────────────────────────────────────────────────────────
TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
DISK_PATH = os.environ.get("AGENT_DISK_PATH", "/")
WATCH_DIR = os.environ.get("MONITOR_WATCH_DIR", "")   # optional dir to watch for new files

DISK_WARN_PCT  = int(os.environ.get("DISK_ALERT_THRESHOLD", "92"))
DISK_CLEAR_PCT = DISK_WARN_PCT - 4
RAM_WARN_MB    = int(os.environ.get("RAM_ALERT_THRESHOLD", "50"))
RAM_CLEAR_MB   = RAM_WARN_MB + 30

STATE_FILE = os.environ.get("MONITOR_STATE_FILE", "/tmp/navig_mini_monitor_state.json")
LOG_FILE   = os.environ.get("MONITOR_LOG_FILE", "")

# Services to check (JSON: {"service_proc": "url_or_empty"})
# e.g. MONITOR_SERVICES='{"gitea":"http://127.0.0.1:3000","aria2c":""}'
_svc_raw = os.environ.get("MONITOR_SERVICES", "{}")
try:
    MONITOR_SERVICES = json.loads(_svc_raw)
except Exception:
    MONITOR_SERVICES = {}


# ── Telegram ───────────────────────────────────────────────────────────────────
def send(msg: str):
    if not TOKEN:
        return
    try:
        url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = urlencode({"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urlopen(url, data, timeout=10)
        _log(f"SENT: {msg[:80]}")
    except URLError as exc:
        _log(f"SEND FAILED: {exc}")


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── State ──────────────────────────────────────────────────────────────────────
class StateUnreadable(RuntimeError):
    """The state file exists but couldn't be read (a transient lock / a truncated write).

    Distinct from "no state yet": collapsing that case to {} would drop the edge-trigger
    dedup flags and re-fire every alert, then persist the emptiness. main() skips the cycle
    instead. (Pure-stdlib deploy script — json_io from navig-core isn't importable here, so
    the raise-don't-return-empty contract is replicated inline.)
    """


def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists():
        return {}  # genuinely no state yet — a fresh run
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StateUnreadable(str(exc)) from exc


def save_state(s: dict):
    # Atomic write (tmp + os.replace) so a crash mid-write can't truncate the state into a
    # corrupt file that the next load would read as empty. Best-effort — never crash the
    # monitor over a state save.
    try:
        p = Path(STATE_FILE)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


# ── Disk check ─────────────────────────────────────────────────────────────────
def check_disk(state: dict, alerts: list):
    try:
        out   = subprocess.check_output(["df", DISK_PATH], text=True, timeout=5)
        parts = out.strip().splitlines()[1].split()
        pct   = int(parts[4].replace("%", ""))
        free  = str(int(parts[3]) // 1024) + "M"
        total = str(int(parts[1]) // 1024) + "M"
        used  = str(int(parts[2]) // 1024) + "M"

        if pct >= DISK_WARN_PCT and not state.get("disk_warned"):
            alerts.append(
                f"🔴 <b>DISK ALMOST FULL</b>\n"
                f"Used: {used} / {total} ({pct}%)\n"
                f"Free: <b>{free}</b>\n"
                f"Path: <code>{DISK_PATH}</code>"
            )
            state["disk_warned"] = True
        elif pct < DISK_CLEAR_PCT:
            state["disk_warned"] = False

        # Daily 08:00 report
        if int(time.strftime("%H")) == 8 and int(time.strftime("%M")) < 5:
            today = time.strftime("%Y%m%d")
            if not state.get(f"daily_{today}"):
                alerts.append(
                    f"📊 <b>Daily Report</b>\n"
                    f"💾 Disk: <b>{pct}%</b> used ({free} free)\n"
                    f"📁 <code>{DISK_PATH}</code>"
                )
                state[f"daily_{today}"] = True
    except Exception as exc:
        _log(f"disk check error: {exc}")


# ── RAM check ──────────────────────────────────────────────────────────────────
def check_ram(state: dict, alerts: list):
    try:
        mem      = Path("/proc/meminfo").read_text(encoding="utf-8")
        free_mb  = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1)) // 1024
        total_mb = int(re.search(r"MemTotal:\s+(\d+)",     mem).group(1)) // 1024

        if free_mb < RAM_WARN_MB and not state.get("ram_warned"):
            alerts.append(
                f"🔴 <b>LOW RAM</b>\n"
                f"Free: <b>{free_mb} MB</b> / {total_mb} MB"
            )
            state["ram_warned"] = True
        elif free_mb > RAM_CLEAR_MB:
            state["ram_warned"] = False
    except Exception as exc:
        _log(f"ram check error: {exc}")


# ── Service checks ─────────────────────────────────────────────────────────────
def check_services(state: dict, alerts: list):
    for proc_name, url in MONITOR_SERVICES.items():
        try:
            # Check if process running
            r = subprocess.run(
                ["pgrep", "-c", proc_name],
                capture_output=True, text=True, timeout=5
            )
            is_up = r.returncode == 0 and r.stdout.strip() not in ("", "0")

            # Optionally verify HTTP if URL given
            if is_up and url:
                try:
                    import urllib.request
                    urllib.request.urlopen(url, timeout=3)
                except Exception:
                    is_up = False

            was_down = state.get(f"{proc_name}_down", False)
            if not is_up and not was_down:
                alerts.append(f"⚠️ <b>{proc_name}</b> is <b>DOWN</b>")
                state[f"{proc_name}_down"] = True
            elif is_up and was_down:
                alerts.append(f"✅ <b>{proc_name}</b> is back <b>UP</b>")
                state[f"{proc_name}_down"] = False
        except Exception as exc:
            _log(f"service check error ({proc_name}): {exc}")


# ── Watch dir: new files ───────────────────────────────────────────────────────
def check_new_files(state: dict, alerts: list):
    if not WATCH_DIR or not Path(WATCH_DIR).is_dir():
        return
    try:
        last_seen = state.get("last_file_check", 0)
        now       = time.time()
        new_files = []

        for root, _, files in os.walk(WATCH_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime > last_seen and now - mtime < 600:
                        size = os.path.getsize(fpath) // (1024 * 1024)
                        rel  = fpath.replace(WATCH_DIR + "/", "")
                        new_files.append(f"• {rel} ({size} MB)")
                except Exception:
                    pass

        if new_files:
            lines = "\n".join(
                f"• <code>{f.replace('<','&lt;').replace('>','&gt;')}</code>"
                for f in new_files[:5]
            )
            alerts.append(f"📥 <b>New files</b> ({len(new_files)}):\n{lines}")

        state["last_file_check"] = now
    except Exception as exc:
        _log(f"file watch error: {exc}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    try:
        state = load_state()
    except StateUnreadable as exc:
        _log(f"state file unreadable ({exc}) — skipping this cycle to preserve alert dedup")
        return
    alerts = []

    check_disk(state, alerts)
    check_ram(state, alerts)
    check_services(state, alerts)
    check_new_files(state, alerts)

    save_state(state)

    _log(f"checks done — {len(alerts)} alert(s)")
    for alert in alerts:
        send(alert)


if __name__ == "__main__":
    main()
