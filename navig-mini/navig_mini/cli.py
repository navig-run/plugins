#!/usr/bin/env python3
"""
navig-mini CLI — manage the local agent.
Entry point: navig-mini (via pyproject.toml [project.scripts])

Commands:
    navig-mini init     Interactive setup wizard
    navig-mini start    Start the agent daemon
    navig-mini stop     Stop the agent daemon
    navig-mini restart  Restart the agent daemon
    navig-mini status   Show agent health
    navig-mini logs     Tail agent log
    navig-mini run CMD  Run a command via agent
"""
import sys, os, subprocess, json, tempfile, time
from pathlib import Path
from urllib.request import urlopen

# -- Locate install dir --------------------------------------------------------
def _find_install_dir():
    for candidate in [
        Path.home() / "navig-mini",
        Path("/opt/navig-mini"),
        Path("/usr/local/share/navig-mini"),
        Path(__file__).parent.parent,
    ]:
        if (candidate / "agent.py").exists():
            return candidate
    return Path.home() / "navig-mini"

# -- Config from .env ----------------------------------------------------------
def _load_env(install_dir):
    env = {}
    for p in [Path(install_dir) / ".env", Path.home() / ".navig-mini" / ".env"]:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.split("#")[0].strip()
            break
    return env

# -- HTTP client ---------------------------------------------------------------
def _ping(port, timeout=3):
    try:
        return json.loads(urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout).read())
    except Exception:
        return None

def _stats(port, timeout=5):
    try:
        return json.loads(urlopen(f"http://127.0.0.1:{port}/stats", timeout=timeout).read())
    except Exception:
        return None

# -- Commands ------------------------------------------------------------------
# Same default install.sh and install.py use, and overridable the same way. `navig-mini`
# installed from a wheel has no sibling install.py — the package ships `navig_mini/` only —
# so the wizard is fetched from here instead of declared unavailable.
INSTALL_BASE_URL = os.environ.get(
    "INSTALL_BASE_URL",
    "https://raw.githubusercontent.com/navig-run/plugins/main/navig-mini",
)


def cmd_init():
    """Run the interactive setup wizard.

    Two shapes, because there are two ways this package arrives:

    * `curl | sh` or a source checkout — install.py sits beside the package; run it.
    * `pip install navig-mini` — it does NOT. `[tool.setuptools.packages.find]` ships
      `navig_mini/` alone, and install.py/agent.py/monitor.py live at the project root, so
      `Path(__file__).parent.parent` is site-packages and the file is simply not there.
      The README advertises this exact path as Method 2, and it used to print
      "install.py not found — run: curl … https://get.navig.run/mini | sh" and exit 1 —
      pointing at a host with NO DNS RECORD, so the suggested recovery was dead too.

    The wizard is now fetched from the same URL install.sh already downloads agent.py and
    monitor.py from. That is the project's existing trust model for this installer, not a
    new one, and INSTALL_BASE_URL overrides it for an airgapped or self-hosted mirror.
    """
    local = Path(__file__).parent.parent / "install.py"
    if local.exists():
        os.execv(sys.executable, [sys.executable, str(local)])

    url = f"{INSTALL_BASE_URL}/install.py"
    print(f"  fetching the setup wizard from {url}")
    try:
        payload = urlopen(url, timeout=30).read()  # noqa: S310 - https, overridable base
    except Exception as exc:  # noqa: BLE001 - any failure here is the same dead end
        print(f"  could not download the wizard: {exc}")
        print("  Set INSTALL_BASE_URL to a reachable mirror, or install from source:")
        print("    git clone https://github.com/navig-run/plugins && "
              "python plugins/navig-mini/install.py")
        sys.exit(1)

    if not payload.lstrip().startswith((b"#!", b'"""', b"#", b"import", b"from")):
        # A proxy or a 404 page rendered as HTML would otherwise be handed to the
        # interpreter. Refuse rather than exec whatever came back.
        print("  the downloaded wizard does not look like Python — refusing to run it.")
        sys.exit(1)

    tmp = Path(tempfile.gettempdir()) / "navig-mini-install.py"
    tmp.write_bytes(payload)
    os.execv(sys.executable, [sys.executable, str(tmp)])

def cmd_start(install_dir, python_cmd, env):
    port = env.get("AGENT_PORT", "9191")
    if _ping(port):
        print(f"  Agent already running on :{port}")
        return
    agent = Path(install_dir) / "agent.py"
    log   = Path(install_dir) / "agent.log"
    subprocess.Popen(
        [python_cmd, str(agent)],
        stdout=open(str(log), "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(2)
    r = _ping(port, 5)
    if r:
        print(f"  ✓ Agent started on :{port} (uptime {r.get('uptime',0)}s)")
    else:
        print(f"  ⚠ Agent starting — check: tail -20 {log}")

def cmd_stop(install_dir):
    agent = Path(install_dir) / "agent.py"
    result = subprocess.run(["pkill", "-f", str(agent)], capture_output=True)
    if result.returncode == 0:
        print("  ✓ Agent stopped")
    else:
        print("  ⚠ Agent not found (already stopped?)")

def cmd_status(env):
    port = env.get("AGENT_PORT", "9191")
    s    = _stats(port)
    if not s:
        print(f"  ✗ Agent not responding on :{port}")
        sys.exit(1)
    print(f"  ✓ NAVIG Mini v{s.get('version','?')} — {s.get('hostname','?')}")
    print(f"    Port:    :{port}")
    print(f"    Uptime:  {s.get('uptime_sec','?')}s")
    print(f"    RAM:     {s.get('ram_free_mb','?')} MB free / {s.get('ram_total_mb','?')} MB")
    print(f"    Disk:    {s.get('disk_pct','?')} used  (free: {s.get('disk_free','?')})")
    print(f"    Load:    {s.get('load','?')}")
    print(f"    Python:  {s.get('python','?')}")

def cmd_logs(install_dir, n=50):
    log = Path(install_dir) / "agent.log"
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        print("\n".join(lines[-n:]))
    else:
        print(f"  No log file at {log}")

def cmd_restart(install_dir, python_cmd, env):
    cmd_stop(install_dir)
    time.sleep(1)
    cmd_start(install_dir, python_cmd, env)

def _usage():
    print("""  navig-mini — Lightweight AI agent for low-power devices

  Commands:
    init        Interactive setup wizard
    start       Start the agent daemon
    stop        Stop the agent daemon  
    restart     Restart the agent daemon
    status      Show health and system stats
    logs [-n N] Tail agent log (default: last 50 lines)
    version     Print version

  Options:
    --dir PATH  Override install directory
    --help      Show this help
""")

# -- Entry point ---------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if not args or "--help" in args or "-h" in args:
        _usage(); return

    # Parse --dir flag
    install_dir = None
    if "--dir" in args:
        idx = args.index("--dir")
        install_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx+2:]
    else:
        install_dir = _find_install_dir()

    env        = _load_env(install_dir)
    python_cmd = sys.executable
    cmd        = args[0] if args else "status"

    if cmd == "init":
        cmd_init()
    elif cmd == "start":
        cmd_start(install_dir, python_cmd, env)
    elif cmd == "stop":
        cmd_stop(install_dir)
    elif cmd in ("restart", "reload"):
        cmd_restart(install_dir, python_cmd, env)
    elif cmd == "status":
        cmd_status(env)
    elif cmd == "logs":
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
        cmd_logs(install_dir, n)
    elif cmd == "version":
        from navig_mini import __version__
        print(f"navig-mini {__version__}")
    else:
        _usage()
        sys.exit(1)

if __name__ == "__main__":
    main()
