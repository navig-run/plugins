# Changelog — navig-vm

## 0.1.1 — 2026-10-17

### Added
- **Ubuntu Server 24.04 LTS: `navig vm create <name> --image ubuntu-24.04`.** Ubuntu's
  published cloud VMDK is verified against its SHA-256 sums and converted with VMware's
  own `vmware-vdiskmanager` — no qemu-img. The `plain` recipe provisions it unchanged.
  `ubuntu`, `lxd` and `syslog` are refused as owner names (stock accounts on Ubuntu).

## 0.1.0 — 2026-10-09

### Added
- **`navig vm`: local VMware Workstation VMs from a verified cloud image.** `create` builds
  a Debian 13 VM (image checked against Debian's SHA-512 sums, disk grown with VMware's
  own tools, UEFI) and provisions it on first boot with a cloud-init seed; `start`,
  `stop`, `list`, `ip`, `ssh`, `exec`, `wait`, `snapshot`, `revert`, `snapshots`,
  `screenshot`, `password` and `destroy` drive it. Each VM has its own SSH key and
  known_hosts; `destroy` only deletes a folder navig created.
- **The `navig-os` recipe** builds a NAVIG OS lab machine from a signed NAVIG apt
  repository shared into the VM, through the same installer an existing Debian machine
  uses.
