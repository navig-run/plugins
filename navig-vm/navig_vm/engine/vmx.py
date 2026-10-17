"""Render a VMware ``.vmx`` for a Linux guest booted from a cloud image."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VmSpec:
    name: str
    cpus: int = 4
    memory_mb: int = 8192
    disk_file: str = "disk.vmdk"
    seed_iso: str | None = "seed.iso"
    guest_os: str = "debian12-64"
    # Host folder exposed to the guest as .host:/<guest name> (read-only).
    shared_folder: Path | None = None
    shared_name: str = "navig-repo"
    extra: dict[str, str] = field(default_factory=dict)


def _quote(value: str) -> str:
    """vmx values are quoted; `|` escapes a byte as hex (VMware's own scheme)."""
    return '"' + value.replace("|", "|7C").replace('"', "|22").replace("\n", "|0A") + '"'


def render(spec: VmSpec) -> str:
    if spec.cpus < 1 or spec.memory_mb < 512:
        raise ValueError("a VM needs at least 1 CPU and 512 MB of memory")
    keys: dict[str, str] = {
        ".encoding": "UTF-8",
        "config.version": "8",
        "virtualHW.version": "19",
        "displayName": spec.name,
        "guestOS": spec.guest_os,
        # UEFI: the boot path the future ISO uses too. Secure Boot off for a lab image.
        "firmware": "efi",
        "uefi.secureBoot.enabled": "FALSE",
        "numvcpus": str(spec.cpus),
        "memsize": str(spec.memory_mb),
        # The PCI topology Workstation gives every VM it creates. PVSCSI and vmxnet3 are
        # PCIe devices: without these root ports power-on fails with "No PCIe slot
        # available for SCSI0" (measured — vmrun reports only "Unknown error").
        "pciBridge0.present": "TRUE",
        **{
            f"pciBridge{n}.{k}": v
            for n in (4, 5, 6, 7)
            for k, v in (("present", "TRUE"), ("virtualDev", "pcieRootPort"), ("functions", "8"))
        },
        "vmci0.present": "TRUE",
        "hpet0.present": "TRUE",
        # Workstation adds a floppy drive unless told not to, then posts a "could not
        # connect to floppy A:" warning at every power-on.
        "floppy0.present": "FALSE",
        # The guest's serial console, to a file: cloud images send the kernel, systemd and
        # cloud-init to ttyS0, so this is the whole first boot as text (`navig vm console`)
        # — the only view of an unattended provisioning that is not a screenshot.
        "serial0.present": "TRUE",
        "serial0.fileType": "file",
        "serial0.fileName": "serial.log",
        "serial0.yieldOnMsrRead": "TRUE",
        "scsi0.present": "TRUE",
        "scsi0.virtualDev": "pvscsi",
        "scsi0:0.present": "TRUE",
        "scsi0:0.fileName": spec.disk_file,
        "ethernet0.present": "TRUE",
        "ethernet0.connectionType": "nat",
        "ethernet0.virtualDev": "vmxnet3",
        "ethernet0.addressType": "generated",
        "usb.present": "TRUE",
        "ehci.present": "TRUE",
        # A desktop session under Wayland wants 3D; without it GNOME falls back to software.
        "mks.enable3d": "TRUE",
        "svga.graphicsMemoryKB": "262144",
        "tools.syncTime": "TRUE",
        "tools.upgrade.policy": "manual",
    }
    if spec.seed_iso:
        keys.update({
            "sata0.present": "TRUE",
            "sata0:1.present": "TRUE",
            "sata0:1.deviceType": "cdrom-image",
            "sata0:1.fileName": spec.seed_iso,
            "sata0:1.startConnected": "TRUE",
        })
    if spec.shared_folder is not None:
        keys.update({
            "isolation.tools.hgfs.disable": "FALSE",
            "sharedFolder.option": "alwaysEnabled",
            "sharedFolder.maxNum": "1",
            "sharedFolder0.present": "TRUE",
            "sharedFolder0.enabled": "TRUE",
            "sharedFolder0.readAccess": "TRUE",
            "sharedFolder0.writeAccess": "FALSE",
            "sharedFolder0.hostPath": str(spec.shared_folder),
            "sharedFolder0.guestName": spec.shared_name,
            "sharedFolder0.expiration": "never",
        })
    keys.update(spec.extra)
    return "".join(f"{k} = {_quote(v)}\n" for k, v in keys.items())


def parse(text: str) -> dict[str, str]:
    """The key/value pairs of a .vmx (enough to read back what ``render`` wrote)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition("=")
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace("|0A", "\n").replace("|22", '"').replace("|7C", "|")
        out[key.strip()] = value
    return out
