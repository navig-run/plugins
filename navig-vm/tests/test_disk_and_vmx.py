"""The VMDK descriptor and the .vmx."""

from __future__ import annotations

from pathlib import Path

import pytest

from navig_vm.engine import disk, vmx


class TestFlatDescriptor:
    def test_points_at_the_raw_file_with_its_sector_count(self) -> None:
        text = disk.flat_descriptor("source.raw", 3 * 1024**3)
        assert 'createType="monolithicFlat"' in text
        assert f'RW {3 * 1024**3 // 512} FLAT "source.raw" 0' in text

    def test_refuses_a_size_that_is_not_whole_sectors(self) -> None:
        with pytest.raises(ValueError):
            disk.flat_descriptor("x.raw", 1000)

    @pytest.mark.parametrize("bad", ['a"b.raw', "../x.raw", "dir/x.raw", "x\n.raw"])
    def test_extent_must_be_a_plain_neighbour(self, bad: str) -> None:
        with pytest.raises(ValueError):
            disk.flat_descriptor(bad, 512)

    def test_writes_next_to_the_raw(self, tmp_path: Path) -> None:
        raw = tmp_path / "source.raw"
        raw.write_bytes(b"\0" * 4096)
        out = disk.write_flat_descriptor(raw, tmp_path / "source.vmdk")
        assert 'RW 8 FLAT "source.raw" 0' in out.read_text()


class TestVmx:
    def test_uefi_pvscsi_nat_and_the_seed(self) -> None:
        keys = vmx.parse(vmx.render(vmx.VmSpec(name="lab")))
        assert keys["firmware"] == "efi"
        assert keys["scsi0.virtualDev"] == "pvscsi"
        assert keys["scsi0:0.fileName"] == "disk.vmdk"
        assert keys["ethernet0.connectionType"] == "nat"
        assert keys["sata0:1.fileName"] == "seed.iso"
        assert keys["numvcpus"] == "4" and keys["memsize"] == "8192"

    def test_pcie_root_ports_exist_for_pvscsi_and_vmxnet3(self) -> None:
        # Without them Workstation refuses power-on: "No PCIe slot available for SCSI0".
        keys = vmx.parse(vmx.render(vmx.VmSpec(name="lab")))
        ports = [n for n in range(8) if keys.get(f"pciBridge{n}.virtualDev") == "pcieRootPort"]
        assert ports == [4, 5, 6, 7]
        assert all(keys[f"pciBridge{n}.present"] == "TRUE" for n in (0, *ports))

    def test_serial_console_goes_to_a_file_and_no_floppy(self) -> None:
        keys = vmx.parse(vmx.render(vmx.VmSpec(name="lab")))
        assert keys["serial0.fileType"] == "file" and keys["serial0.fileName"] == "serial.log"
        assert keys["floppy0.present"] == "FALSE"

    def test_shared_folder_only_when_given_and_read_only(self, tmp_path: Path) -> None:
        assert "sharedFolder0.hostPath" not in vmx.parse(vmx.render(vmx.VmSpec(name="a")))
        keys = vmx.parse(vmx.render(vmx.VmSpec(name="a", shared_folder=tmp_path)))
        assert keys["sharedFolder0.hostPath"] == str(tmp_path)
        assert keys["sharedFolder0.writeAccess"] == "FALSE"

    def test_values_round_trip_through_vmware_escaping(self) -> None:
        name = 'odd "name" | with pipe'
        assert vmx.parse(vmx.render(vmx.VmSpec(name=name)))["displayName"] == name

    def test_refuses_a_machine_that_cannot_boot(self) -> None:
        with pytest.raises(ValueError):
            vmx.render(vmx.VmSpec(name="a", memory_mb=128))
