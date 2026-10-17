---
name: microsoft-store
description: Search, install, update, or remove apps from the Microsoft Store on Windows. Use when the user wants to install, find, update, or uninstall a Windows Store / Microsoft Store app.
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Microsoft Store

This capability is provided by the **navig-msstore** plugin (Windows only). Use
`navig mstore <command>` to work with the Microsoft Store.

- Run `navig mstore --help` for the full toolkit (search, install, list, and more).

Notes:
- Installing or removing apps changes the system — confirm the exact app with
  the user before installing or uninstalling.
