"""VM records, the delete guard, image checksums and extraction."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from navig_vm.engine import images, store


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "vms"
    r.mkdir()
    monkeypatch.setattr(store, "vm_root", lambda: r)
    return r


def _record(name: str = "lab") -> store.VmRecord:
    return store.VmRecord(
        name=name, image="debian-13", recipe="plain", owner="navig", cpus=2,
        memory_mb=2048, disk_gb=20, created="2026-10-09T00:00:00+00:00", password="pw",
    )


class TestStore:
    def test_names(self) -> None:
        assert store.valid_name("navig-os-lab")
        for bad in ("", "Lab", "-x", "a_b", "a/b", "x" * 50):
            assert not store.valid_name(bad)

    def test_default_root_is_vmwares_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(store.settings, "get", lambda *_a, **_k: None)
        assert store.vm_root() == Path.home() / "Documents" / "Virtual Machines"
        monkeypatch.setattr(store.settings, "get", lambda *_a, **_k: "~/vms")
        assert store.vm_root() == Path("~/vms").expanduser()

    def test_record_round_trip(self, root: Path) -> None:
        rec = _record()
        rec.dir.mkdir()
        rec.save()
        back = store.load("lab")
        assert back == rec
        assert [r.name for r in store.all_records()] == ["lab"]

    def test_destroy_guard_only_accepts_our_folders(self, root: Path, tmp_path: Path) -> None:
        rec = _record()
        rec.dir.mkdir()
        assert not store.is_ours(rec.dir), "no record yet"
        rec.save()
        assert store.is_ours(rec.dir)
        # A folder with a record that is NOT directly under the root is not ours to delete.
        stray = tmp_path / "elsewhere" / "lab"
        stray.mkdir(parents=True)
        (stray / store.RECORD).write_text("{}")
        assert not store.is_ours(stray)
        assert not store.is_ours(root)


class TestImages:
    def test_expected_sha512_needs_an_exact_name(self) -> None:
        h = "a" * 128
        sums = f"{h}  debian-13-generic-amd64.tar.xz\n{'b' * 128}  debian-13-generic-amd64.raw\n"
        assert images.expected_sha512(sums, "debian-13-generic-amd64.tar.xz") == h
        with pytest.raises(images.ImageError):
            images.expected_sha512(sums, "debian-13-generic-amd64.tar")

    def _archive(self, tmp_path: Path, payload: bytes) -> Path:
        path = tmp_path / "img.tar.xz"
        with tarfile.open(path, "w:xz") as tar:
            info = tarfile.TarInfo("disk.raw")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return path

    def test_extracts_the_disk(self, tmp_path: Path) -> None:
        payload = b"\x00" * 4096 + b"BOOT" * 128
        out = images.extract_disk(self._archive(tmp_path, payload), "disk.raw", tmp_path / "source.raw")
        assert out.read_bytes() == payload

    def test_a_missing_member_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(images.ImageError):
            images.extract_disk(self._archive(tmp_path, b"x"), "other.raw", tmp_path / "o.raw")

    def test_fetch_refuses_a_download_that_does_not_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(images, "image_cache", lambda: tmp_path)
        good = b"the real image"
        sums = f"{hashlib.sha512(good).hexdigest()}  img.tar.xz\n".encode()
        img = images.Image("t", "https://x/img.tar.xz", "https://x/SHA512SUMS", "disk.raw", "other", "t")
        monkeypatch.setattr(images, "_get", lambda url, timeout=60.0: sums)

        class _Res(io.BytesIO):
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: _Res(b"tampered"))
        with pytest.raises(images.ImageError, match="checksum mismatch"):
            images.fetch(img)
        assert not (tmp_path / "img.tar.xz").exists()

        monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: _Res(good))
        assert images.fetch(img).read_bytes() == good
        # Cached and verified: no second download.
        monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: pytest.fail("re-downloaded"))
        assert images.fetch(img).read_bytes() == good


class TestIpFromSerial:
    LINES = (
        "[   20.8] cloud-init[815]: ci-info: | ens192 | True |       10.0.0.10       | 255.255.255.0 | global | 00:0c:29:e1:98:18 |\n"
        "[   20.8] cloud-init[815]: ci-info: | ens192 | True | fe80::20c:29ff:fee1:9818/64 |       .       |  link  | 00:0c:29:e1:98:18 |\n"
        "[   20.8] cloud-init[815]: ci-info: |   lo   | True |          127.0.0.1          |   255.0.0.0   |  host  |         .         |\n"
    )

    def test_the_last_reported_address_wins(self, tmp_path: Path) -> None:
        log = tmp_path / "serial.log"
        # Two boots: the second got a new lease. Loopback is never the answer.
        log.write_text(self.LINES + self.LINES.replace("10.0.0.10", "10.0.0.15"))
        assert store.ip_from_serial(log) == "10.0.0.15"

    def test_nothing_reported_yet(self, tmp_path: Path) -> None:
        log = tmp_path / "serial.log"
        log.write_text("[    1.0] Linux version 6.12\n")
        assert store.ip_from_serial(log) is None
        assert store.ip_from_serial(tmp_path / "missing.log") is None

    def test_reads_the_real_serial_log_shape(self, tmp_path: Path) -> None:
        log = tmp_path / "serial.log"
        log.write_text(self.LINES.replace("|   lo   |", "|   lo   |"))
        assert store.ip_from_serial(log) == "10.0.0.10"


class TestPasswords:
    def test_only_layout_safe_letters(self) -> None:
        # Same key on QWERTY / AZERTY / QWERTZ, no digits (Shift on AZERTY), no l/I/1/o/0.
        for _ in range(200):
            pw = store.new_password()
            assert len(pw) == 16
            assert set(pw) <= set(store.PASSWORD_ALPHABET)
        assert not set(store.PASSWORD_ALPHABET) & set("aqwzymilo0123456789")


class TestUbuntuImage:
    def test_ubuntu_is_a_published_vmdk_checked_with_sha256(self) -> None:
        img = images.IMAGES["ubuntu-24.04"]
        assert img.is_vmdk and img.checksum == "sha256"
        assert img.sums_url.endswith("/SHA256SUMS")
        assert not images.IMAGES["debian-13"].is_vmdk

    def test_sha256_sums_are_matched_by_exact_name(self) -> None:
        h = "c" * 64
        sums = f"{'d' * 64} *noble-server-cloudimg-amd64.ova\n{h} *noble-server-cloudimg-amd64.vmdk\n"
        assert images.expected_digest(sums, "noble-server-cloudimg-amd64.vmdk", "sha256") == h
        # A 64-hex line never satisfies a sha512 lookup, and vice versa.
        with pytest.raises(images.ImageError):
            images.expected_digest(sums, "noble-server-cloudimg-amd64.vmdk", "sha512")
        with pytest.raises(images.ImageError, match="unsupported"):
            images.expected_digest(sums, "x", "md5")

    def test_fetch_verifies_a_sha256_image(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(images, "image_cache", lambda: tmp_path)
        good = b"KDMV stream-optimized disk"
        sums = f"{hashlib.sha256(good).hexdigest()} *u.vmdk\n".encode()
        img = images.Image("u", "https://x/u.vmdk", "https://x/SHA256SUMS", "", "ubuntu-64", "u", checksum="sha256")
        monkeypatch.setattr(images, "_get", lambda url, timeout=60.0: sums)

        class _Res(io.BytesIO):
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: _Res(b"tampered"))
        with pytest.raises(images.ImageError, match="checksum mismatch"):
            images.fetch(img)
        monkeypatch.setattr(images.urllib.request, "urlopen", lambda *a, **k: _Res(good))
        assert images.fetch(img).read_bytes() == good

    def test_ubuntu_default_accounts_are_not_owners(self) -> None:
        for name in ("ubuntu", "lxd", "syslog"):
            assert not store.valid_owner(name)
