"""Base images: download once, verify against the publisher's checksums, extract the disk.

Two publisher shapes are handled: a tar archive holding a raw disk (Debian's ``disk.raw``),
and a ready VMware disk (Ubuntu's stream-optimized ``.vmdk``), which ``vmware-vdiskmanager``
converts directly — no third-party conversion tool either way.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from navig_sdk.host import cache_dir

_CHUNK = 1 << 20


@dataclass(frozen=True)
class Image:
    id: str
    url: str
    sums_url: str
    member: str  # the raw disk inside the archive; "" when the download IS the disk
    guest_os: str  # VMware's guestOS key
    description: str
    checksum: str = "sha512"  # the algorithm the sums_url file lists

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def is_vmdk(self) -> bool:
        """The download is itself a VMware disk (no archive member to extract)."""
        return not self.member and self.filename.endswith(".vmdk")


IMAGES: dict[str, Image] = {
    "debian-13": Image(
        id="debian-13",
        url="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.tar.xz",
        sums_url="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        member="disk.raw",
        # Workstation 17 has no debian13 key yet; debian12-64 is the closest match.
        guest_os="debian12-64",
        description="Debian 13 (trixie) generic cloud image, amd64, cloud-init",
    ),
    "ubuntu-24.04": Image(
        id="ubuntu-24.04",
        url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.vmdk",
        sums_url="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        member="",
        guest_os="ubuntu-64",
        description="Ubuntu Server 24.04 LTS (noble) cloud image, amd64, cloud-init",
        checksum="sha256",
    ),
}

_HEX_LEN = {"sha256": 64, "sha512": 128}


class ImageError(RuntimeError):
    """A download, checksum or extraction that did not produce a usable disk."""


def image_cache() -> Path:
    d = cache_dir() / "vm-images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def expected_digest(sums_text: str, filename: str, algorithm: str = "sha512") -> str:
    """The checksum a ``SHA*SUMS`` file lists for ``filename`` (exact name match)."""
    if algorithm not in _HEX_LEN:
        raise ImageError(f"unsupported checksum algorithm {algorithm!r}")
    pattern = rf"([0-9a-f]{{{_HEX_LEN[algorithm]}}})\s+\*?(\S+)"
    for line in sums_text.splitlines():
        m = re.fullmatch(pattern, line.strip())
        if m and m.group(2) == filename:
            return m.group(1)
    raise ImageError(f"{filename} is not listed in the published checksums")


def expected_sha512(sums_text: str, filename: str) -> str:
    """The checksum ``SHA512SUMS`` lists for ``filename`` (exact name match)."""
    return expected_digest(sums_text, filename, "sha512")


def digest_file(path: Path, algorithm: str = "sha512") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha512_file(path: Path) -> str:
    return digest_file(path, "sha512")


def _get(url: str, timeout: float = 60.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as res:  # noqa: S310 — fixed https URLs
        return res.read()


def fetch(image: Image, progress: Callable[[int, int], None] | None = None) -> Path:
    """The verified archive, downloading it if the cached copy is missing or wrong."""
    name = image.filename
    target = image_cache() / name
    want = expected_digest(_get(image.sums_url).decode("utf-8", "replace"), name, image.checksum)
    if target.is_file() and digest_file(target, image.checksum) == want:
        return target

    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(image.url, timeout=60) as res, partial.open("wb") as out:  # noqa: S310
            total = int(res.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: res.read(_CHUNK), b""):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ImageError(f"download failed: {exc}") from exc

    got = digest_file(partial, image.checksum)
    if got != want:
        partial.unlink(missing_ok=True)
        raise ImageError(f"checksum mismatch for {name}: the download is not what the publisher listed")
    partial.replace(target)
    return target


def extract_disk(archive: Path, member: str, dest: Path) -> Path:
    """Stream ``member`` out of the archive to ``dest`` (never via a path inside the tar)."""
    with tarfile.open(archive, mode="r:*") as tar:
        try:
            info = tar.getmember(member)
        except KeyError as exc:
            raise ImageError(f"{archive.name} has no {member}") from exc
        if not info.isfile():
            raise ImageError(f"{member} in {archive.name} is not a regular file")
        src = tar.extractfile(info)
        if src is None:
            raise ImageError(f"could not read {member} from {archive.name}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out, _CHUNK)
    tmp.replace(dest)
    return dest
