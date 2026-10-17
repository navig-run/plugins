---
name: vm
description: Create and drive local virtual machines on VMware Workstation from verified cloud images. Use when the user wants a fresh Linux VM, a test machine, a lab to try NAVIG OS in, to start/stop/snapshot/revert a VM, or to run a command inside one.
activation_keywords: [vm, vmware, virtual machine, lab vm, snapshot]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Virtual machines (`navig vm`)

Local VMs on VMware Workstation, built from a verified Debian 13 cloud image and set up
on first boot by cloud-init. Every VM is a folder under the VM root with its own SSH key.

| Goal | Command |
|---|---|
| A plain Debian VM | `navig vm create <name>` |
| A NAVIG OS lab machine | `navig vm create <name> --recipe navig-os --repo <signed repo folder>` |
| Wait for first boot to finish | `navig vm wait <name>` |
| List / start / stop | `navig vm list` · `navig vm start <name>` · `navig vm stop <name>` |
| Shell / one command | `navig vm ssh <name>` · `navig vm exec <name> "<cmd>"` |
| Save / restore state | `navig vm snapshot <name> <snap>` · `navig vm revert <name> <snap>` |
| Owner password | `navig vm password <name>` |
| Delete | `navig vm destroy <name>` (asks first) |

Rules: `destroy` is irreversible — confirm with the user before passing `--yes`. Prefer
`revert` to a snapshot over rebuilding. `revert` leaves the VM powered off.
