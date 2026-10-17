"""Where VMs live, and the small record navig keeps next to each one."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from navig_sdk import settings
from navig_sdk.host import atomic_write_text, set_owner_only_file_permissions

RECORD = "navig-vm.json"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def vm_root() -> Path:
    """``plugins.vm.dir`` (``navig config``), else VMware's own default folder."""
    configured = settings.get("vm", "dir")
    root = Path(str(configured)).expanduser() if configured else Path.home() / "Documents" / "Virtual Machines"
    return root


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name))


# cloud-init prints its network table on the serial console at every boot:
#   ci-info: | ens192 | True | 10.0.0.15 | 255.255.255.0 | global | 00:0c:29:… |
_CI_INFO_IPV4 = re.compile(
    r"ci-info: \|\s+(?!lo\b)(\S+)\s+\|\s+True\s+\|\s+((?:\d{1,3}\.){3}\d{1,3})\s+\|"
)


def ip_from_serial(log: Path) -> str | None:
    """The guest's IPv4 as cloud-init last reported it on the serial console, or None.

    VMware Tools reports the IP only while it runs — and on first boot apt is busy
    reinstalling it — but the guest printed its address the moment it got one.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _CI_INFO_IPV4.findall(text)
    return found[-1][1] if found else None


# Accounts and groups a Debian system already has (base-passwd and the cloud image).
# `useradd <one of these>` fails inside the guest — "operator" is a stock group (GID 37) —
# where nobody sees it: cloud-init logs a warning and the machine has no owner, no SSH.
RESERVED_ACCOUNTS = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp",
    "proxy", "www-data", "backup", "list", "irc", "gnats", "nobody", "nogroup", "adm", "tty",
    "disk", "kmem", "dialout", "fax", "voice", "cdrom", "floppy", "tape", "sudo", "audio",
    "dip", "operator", "src", "shadow", "utmp", "video", "sasl", "plugdev", "staff", "users",
    "systemd-journal", "systemd-network", "systemd-timesync", "messagebus", "sshd", "_apt",
    "input", "render", "kvm", "netdev", "debian", "ubuntu", "lxd", "syslog", "navig-kiosk", "navig-launch", "nopasswdlogin",
    # NOT an owner: navig treats a process running as a user named `navig` as its legacy
    # system-service account and moves its config to /etc/navig (navig.platform.paths
    # _is_system_service) — the daemon then dies on a directory it cannot create, and the
    # owner's CLI would read a different config from the daemon's.
    "navig",
})


def valid_owner(name: str) -> bool:
    return valid_name(name) and name not in RESERVED_ACCOUNTS


# Letters on the same key in QWERTY, AZERTY and QWERTZ, minus look-alikes (i l o). A
# password is typed at a login screen whose layout may not match the keyboard in front of
# you: `5NXlK1iWQAyD` failed at GDM for an AZERTY user (digits need Shift there, A/Q/W
# move, and l/I/1 look alike). 16 of 17 letters ≈ 65 bits — plenty for a lab VM.
PASSWORD_ALPHABET = "bcdefghjknprstuvx"


def new_password(length: int = 16) -> str:
    import secrets

    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def default_owner() -> str:
    """This computer's own user name, when it makes a valid guest account; else ``pilot``."""
    import getpass

    try:
        candidate = re.sub(r"[^a-z0-9-]", "", getpass.getuser().lower()).lstrip("-")
    except Exception:  # noqa: BLE001 — no user name to borrow is not an error
        candidate = ""
    return candidate if valid_owner(candidate) else "pilot"


@dataclass
class VmRecord:
    name: str
    image: str
    recipe: str
    owner: str
    cpus: int
    memory_mb: int
    disk_gb: int
    created: str
    # The guest owner's password. The record is owner-only on disk; the guest only ever
    # received its SHA-512 hash.
    password: str = field(repr=False, default="")
    repo: str = ""
    # A password being set by `password --reset`, until the VM is seen to accept it.
    pending_password: str = field(repr=False, default="")

    @property
    def dir(self) -> Path:
        return vm_root() / self.name

    @property
    def vmx(self) -> Path:
        return self.dir / f"{self.name}.vmx"

    @property
    def ssh_key(self) -> Path:
        return self.dir / "id_ed25519"

    @property
    def known_hosts(self) -> Path:
        return self.dir / "known_hosts"

    def save(self) -> None:
        path = self.dir / RECORD
        atomic_write_text(path, json.dumps(asdict(self), indent=2) + "\n")
        set_owner_only_file_permissions(path)


def load(name: str) -> VmRecord | None:
    path = vm_root() / name / RECORD
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return VmRecord(**{k: data[k] for k in VmRecord.__dataclass_fields__ if k in data})
    except (OSError, ValueError, TypeError):
        return None


def all_records() -> list[VmRecord]:
    root = vm_root()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / RECORD).is_file():
            rec = load(child.name)
            if rec is not None:
                out.append(rec)
    return out


def is_ours(path: Path) -> bool:
    """A folder navig created: directly under the VM root AND holding a navig record.

    The only kind of folder ``destroy`` may delete — a typo must never reach anything else.
    """
    try:
        resolved = path.resolve()
        return resolved.parent == vm_root().resolve() and (resolved / RECORD).is_file()
    except OSError:
        return False
