"""DeviceInventoryStore + toolchain-detection tests."""

from __future__ import annotations

from navig_mobile import tools
from navig_mobile.store import DeviceInventoryStore, get_store


def test_store_record_and_upsert_device(tmp_path):
    st = get_store(db_path=tmp_path / "mobile.db")
    st.record_device(udid="A1", platform="android", name="Pixel", model="Pixel 8",
                     os_version="15", serial="SER1")
    rows = st.list_devices()
    assert len(rows) == 1 and rows[0]["udid"] == "A1" and rows[0]["name"] == "Pixel"
    first_seen = rows[0]["first_seen"]

    # Upsert: empty name must NOT clobber the stored one (COALESCE/NULLIF).
    st.record_device(udid="A1", platform="android", name="", model="Pixel 8",
                     os_version="16", serial="")
    rows = st.list_devices()
    assert len(rows) == 1
    assert rows[0]["name"] == "Pixel"  # preserved
    assert rows[0]["os_version"] == "16"  # updated
    assert rows[0]["first_seen"] == first_seen  # not reset


def test_store_backup_catalog(tmp_path):
    st = get_store(db_path=tmp_path / "mobile.db")
    rid = st.record_backup(udid="A1", platform="android", path="/b/A1.ab",
                           encrypted=True, sha256="deadbeef", size_bytes=1234,
                           note="test")
    assert rid >= 1
    backups = st.list_backups("A1")
    assert len(backups) == 1
    b = backups[0]
    assert b["path"] == "/b/A1.ab" and b["encrypted"] == 1 and b["bytes"] == 1234
    assert st.list_backups("NOPE") == []


def test_store_is_basestore_subclass():
    from navig.store.base import BaseStore

    assert issubclass(DeviceInventoryStore, BaseStore)


def test_toolchain_detect_shape():
    tc = tools.detect()
    keys = {t.key for t in tc.tools}
    # every pillar's tool is probed
    for expected in ("adbutils", "pymobiledevice3", "adb", "fastboot", "scrcpy",
                     "usbmux", "mvt", "frida", "objection", "ileapp", "aleapp"):
        assert expected in keys, f"missing tool probe: {expected}"
    # engines are installed in the test env
    assert tc.android_ready is True
    assert tc.ios_ready is True
    # readiness maps to the underlying probe
    assert tc.android_ready == bool(tc.get("adbutils").found)


def test_toolchain_missing_tool_has_install_hint():
    tc = tools.detect()
    for t in tc.tools:
        if not t.found:
            assert t.install_hint or t.note, f"{t.key} missing guidance when absent"
