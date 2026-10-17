"""`navig vm` commands: what they refuse, and what they ask VMware to do."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from navig_vm.commands import vm as cmd
from navig_vm.engine import store, vmware

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "vms"
    r.mkdir()
    monkeypatch.setattr(store, "vm_root", lambda: r)
    return r


class _FakeVMware:
    def __init__(self, running: bool = False) -> None:
        self.calls: list[tuple] = []
        self._running = running
        self.vdiskmanager = Path("vdisk")

    def is_running(self, vmx: Path) -> bool:
        return self._running

    def running(self) -> list[Path]:
        return []

    def __getattr__(self, name):
        def record(*a, **k):
            self.calls.append((name, a, k))
        return record


def _make(root: Path, name: str = "lab") -> store.VmRecord:
    rec = store.VmRecord(
        name=name, image="debian-13", recipe="plain", owner="navig", cpus=2,
        memory_mb=2048, disk_gb=20, created="2026-10-09T00:00:00+00:00", password="pw",
    )
    rec.dir.mkdir()
    rec.save()
    return rec


class TestCreateRefuses:
    @pytest.mark.parametrize("name", ["Bad", "a_b", "../x"])
    def test_a_bad_name(self, name: str, root: Path) -> None:
        r = runner.invoke(cmd.vm_app, ["create", name])
        assert r.exit_code == 1

    @pytest.mark.parametrize("owner", ["root", "operator", "staff", "users", "navig-kiosk", "navig"])
    def test_an_owner_debian_already_has(self, owner: str, root: Path) -> None:
        # `useradd operator` fails INSIDE the guest (Debian ships an `operator` group), where
        # cloud-init only logs a warning and the VM ends up with no owner and no SSH.
        r = runner.invoke(cmd.vm_app, ["create", "lab", "--owner", owner])
        assert r.exit_code == 1
        assert not (root / "lab").exists()

    def test_the_default_owner_is_this_users_name_or_pilot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("getpass.getuser", lambda: "AdaL")
        assert store.default_owner() == "adal"
        # A host name that would make a bad guest account falls back, never fails.
        for host_user in ("root", "navig", "Ünïcode", "", "x" * 60):
            monkeypatch.setattr("getpass.getuser", lambda u=host_user: u)
            assert store.valid_owner(store.default_owner()), host_user

    def test_navig_os_without_a_repo(self, root: Path) -> None:
        r = runner.invoke(cmd.vm_app, ["create", "lab", "--recipe", "navig-os"])
        assert r.exit_code == 1

    def test_navig_os_with_a_folder_that_is_not_a_repo(self, root: Path, tmp_path: Path) -> None:
        r = runner.invoke(cmd.vm_app, ["create", "lab", "--recipe", "navig-os", "--repo", str(tmp_path)])
        assert r.exit_code == 1

    def test_an_existing_folder(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (root / "lab").mkdir()
        monkeypatch.setattr(cmd, "_vmware", lambda: _FakeVMware())
        r = runner.invoke(cmd.vm_app, ["create", "lab"])
        assert r.exit_code == 1


class TestLifecycle:
    def test_start_asks_vmware_with_the_window(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _make(root)
        fake = _FakeVMware()
        monkeypatch.setattr(cmd, "_vmware", lambda: fake)
        r = runner.invoke(cmd.vm_app, ["start", "lab"])
        assert r.exit_code == 0, r.output
        assert fake.calls == [("start", (rec.vmx,), {"gui": True})]

    def test_unknown_vm(self, root: Path) -> None:
        r = runner.invoke(cmd.vm_app, ["start", "ghost"])
        assert r.exit_code == 1

    def test_list_without_vmware_still_lists(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _make(root)

        def no_vmware():
            raise vmware.VMwareError("not installed")

        monkeypatch.setattr(cmd.VMware, "locate", staticmethod(no_vmware))
        r = runner.invoke(cmd.vm_app, ["list", "--json"])
        assert r.exit_code == 0, r.output
        assert '"running": false' in r.output and '"name": "lab"' in r.output

    def test_password(self, root: Path) -> None:
        _make(root)
        r = runner.invoke(cmd.vm_app, ["password", "lab"])
        assert r.output.strip() == "pw"


class TestDestroy:
    def test_deletes_only_after_confirmation(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _make(root)
        monkeypatch.setattr(cmd, "_vmware", lambda: _FakeVMware())
        r = runner.invoke(cmd.vm_app, ["destroy", "lab"], input="n\n")
        assert r.exit_code == 1
        assert rec.dir.exists()
        r = runner.invoke(cmd.vm_app, ["destroy", "lab"], input="y\n")
        assert r.exit_code == 0, r.output
        assert not rec.dir.exists()

    def test_never_a_folder_outside_the_root(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _make(root)
        monkeypatch.setattr(store, "is_ours", lambda _p: False)
        r = runner.invoke(cmd.vm_app, ["destroy", "lab", "--yes"])
        assert r.exit_code == 1
        assert rec.dir.exists()


class TestSsh:
    def test_uses_the_vms_own_key_and_known_hosts(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _make(root)
        monkeypatch.setattr(cmd.shutil, "which", lambda _n: "/usr/bin/ssh")
        argv = cmd._ssh_argv(rec, "192.168.1.5", ["uptime"])
        assert argv[argv.index("-i") + 1] == str(rec.ssh_key)
        assert f"UserKnownHostsFile={rec.known_hosts}" in argv
        assert argv[-2:] == ["navig@192.168.1.5", "uptime"]

    def test_push_copies_with_the_same_key_and_known_hosts(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _make(root)
        monkeypatch.setattr(cmd.shutil, "which", lambda _n: "/usr/bin/scp")
        argv = cmd._scp_argv(rec, "192.168.1.5", ["a.whl", "b.sh"], "/tmp/ci/")
        assert argv[argv.index("-i") + 1] == str(rec.ssh_key)
        assert f"UserKnownHostsFile={rec.known_hosts}" in argv
        assert argv[-3:] == ["a.whl", "b.sh", "navig@192.168.1.5:/tmp/ci/"]

    def test_push_refuses_a_missing_file_before_touching_the_vm(self, root: Path, tmp_path: Path) -> None:
        _make(root)
        r = runner.invoke(cmd.vm_app, ["push", "lab", str(tmp_path / "nope.whl")])
        assert r.exit_code != 0
        assert "Not a file" in r.output


class TestVmrunParsing:
    def test_error_lines_are_failures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            vmware, "run_text",
            lambda argv, timeout=None: SimpleNamespace(returncode=255, stdout="Error: The virtual machine is not powered on", stderr=""),
        )
        vm = vmware.VMware(vmrun=tmp_path / "vmrun")
        with pytest.raises(vmware.VMwareError, match="not powered on"):
            vm.stop(tmp_path / "x.vmx")
        assert vm.guest_ip(tmp_path / "x.vmx") is None

    def test_running_list(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        out = "Total running VMs: 2\nC:\\vms\\a\\a.vmx\nC:\\vms\\b\\b.vmx\n"
        monkeypatch.setattr(vmware, "run_text", lambda argv, timeout=None: SimpleNamespace(returncode=0, stdout=out, stderr=""))
        names = [p.name for p in vmware.VMware(vmrun=tmp_path / "vmrun").running()]
        assert len(names) == 2

    def test_a_child_that_outlives_the_tool_does_not_hang_the_read(self) -> None:
        # `vmrun start … gui` leaves the VMware window running with the caller's output
        # handles. Reading through pipes waited for that window to close (create hung for
        # as long as the VM ran). The grandchild here plays the window.
        import sys
        import time

        script = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); "
            "print('Error: still reported')"
        )
        started = time.monotonic()
        proc = vmware.run_text([sys.executable, "-c", script], timeout=15)
        assert time.monotonic() - started < 10
        assert proc.stdout.strip() == "Error: still reported"

    def test_find_tool_honours_the_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        exe = tmp_path / ("vmrun.exe" if vmware.sys.platform == "win32" else "vmrun")
        exe.write_text("")
        monkeypatch.setattr(vmware.shutil, "which", lambda _n: None)
        monkeypatch.setenv("NAVIG_VMWARE_DIR", str(tmp_path))
        assert vmware.find_tool("vmrun") == exe


class TestGuestIp:
    @pytest.mark.parametrize("out", ["unknown", "Error: VMware Tools are not running", "", "fe80::1"])
    def test_anything_but_an_ipv4_is_no_address(self, out: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # While the guest boots, Tools answers "unknown" with exit 0.
        monkeypatch.setattr(vmware, "run_text", lambda argv, timeout=None: SimpleNamespace(returncode=0, stdout=out, stderr=""))
        assert vmware.VMware(vmrun=tmp_path / "vmrun").guest_ip(tmp_path / "x.vmx") is None

    def test_an_ipv4_is_returned(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(vmware, "run_text", lambda argv, timeout=None: SimpleNamespace(returncode=0, stdout="10.0.0.42\n", stderr=""))
        assert vmware.VMware(vmrun=tmp_path / "vmrun").guest_ip(tmp_path / "x.vmx") == "10.0.0.42"


class TestPasswordReset:
    """The VM's answer decides — never chpasswd's exit status (it can apply AND fail)."""

    def _run(self, root: Path, monkeypatch: pytest.MonkeyPatch, guest: dict) -> tuple:
        _make(root)  # recorded password: "pw"
        monkeypatch.setattr(cmd, "_vmware", lambda: _FakeVMware(running=True))
        monkeypatch.setattr(cmd, "_ip_or_fail", lambda *_a, **_k: "10.0.0.5")
        monkeypatch.setattr(cmd.shutil, "which", lambda _n: "/usr/bin/ssh")
        monkeypatch.setattr(store, "new_password", lambda: "bcdfghjknprstuvx")

        def fake_run(argv, input=b"", **_kw):
            # What a Linux guest sees: bytes. A CR here becomes part of a password.
            assert isinstance(input, bytes) and b"\r" not in input, input
            remote = argv[-1]
            text = input.decode()
            if remote.endswith("chpasswd"):
                _old, line = text.split("\n")[:2]
                guest["password"] = line.split(":", 1)[1]
                return SimpleNamespace(returncode=1, stdout=b"", stderr=b"chpasswd: odd failure")
            ok = text.split("\n")[0] == guest["password"]
            return SimpleNamespace(returncode=0 if ok else 1, stdout=b"", stderr=b"")

        monkeypatch.setattr(cmd.subprocess, "run", fake_run)
        result = runner.invoke(cmd.vm_app, ["password", "lab", "--reset"])
        return result, store.load("lab")

    def test_applied_despite_a_failing_exit_is_success(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guest = {"password": "pw"}
        result, rec = self._run(root, monkeypatch, guest)
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "bcdfghjknprstuvx"
        assert rec.password == "bcdfghjknprstuvx" and rec.pending_password == ""

    def test_a_wrong_recorded_password_changes_nothing(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        guest = {"password": "something-else"}
        result, rec = self._run(root, monkeypatch, guest)
        assert result.exit_code == 1
        assert guest["password"] == "something-else"  # never sent chpasswd
        assert rec.password == "pw"
