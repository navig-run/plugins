"""Turn a raw disk image into something VMware can boot, with no conversion tools.

A VMDK can be a few lines of text that point at a flat extent — any raw file. So a
cloud image's ``disk.raw`` becomes readable by writing a descriptor next to it; VMware's
own ``vmware-vdiskmanager`` then converts it to a growable disk and grows it.
"""

from __future__ import annotations

import secrets
from pathlib import Path

SECTOR = 512
_HEADS = 255
_SECTORS_PER_TRACK = 63


def flat_descriptor(extent_name: str, size_bytes: int, adapter: str = "lsilogic") -> str:
    """The text of a ``monolithicFlat`` VMDK whose single extent is ``extent_name``."""
    if size_bytes <= 0 or size_bytes % SECTOR:
        raise ValueError(f"a raw disk must be a positive multiple of {SECTOR} bytes, got {size_bytes}")
    if any(c in extent_name for c in '"\n\r/\\'):
        raise ValueError(f"extent must be a plain file name next to the descriptor: {extent_name!r}")
    sectors = size_bytes // SECTOR
    cylinders = max(1, min(sectors // (_HEADS * _SECTORS_PER_TRACK), 65535))
    return (
        "# Disk DescriptorFile\n"
        "version=1\n"
        f"CID={secrets.randbits(32):08x}\n"
        "parentCID=ffffffff\n"
        'createType="monolithicFlat"\n'
        "\n"
        "# Extent description\n"
        f'RW {sectors} FLAT "{extent_name}" 0\n'
        "\n"
        "# The Disk Data Base\n"
        "#DDB\n"
        'ddb.virtualHWVersion = "19"\n'
        f'ddb.adapterType = "{adapter}"\n'
        f'ddb.geometry.cylinders = "{cylinders}"\n'
        f'ddb.geometry.heads = "{_HEADS}"\n'
        f'ddb.geometry.sectors = "{_SECTORS_PER_TRACK}"\n'
        f'ddb.longContentID = "{secrets.token_hex(16)}"\n'
    )


def write_flat_descriptor(raw: Path, descriptor: Path) -> Path:
    """Write ``descriptor`` (a .vmdk) pointing at ``raw``, which must sit in the same folder."""
    if raw.parent.resolve() != descriptor.parent.resolve():
        raise ValueError("the raw image and its descriptor must be in the same folder")
    descriptor.write_text(flat_descriptor(raw.name, raw.stat().st_size), encoding="ascii", newline="\n")
    return descriptor
