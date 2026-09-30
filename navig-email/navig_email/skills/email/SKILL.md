---
name: email
description: Send, read, or search email. Use when the user wants to send or draft an email, check their inbox, or find a specific message.
activation_keywords: [email, inbox]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Email

This capability is provided by the **navig-email** plugin. Use `navig email
<command>` for email work.

- Run `navig email --help` to see the available commands (send, read/list inbox,
  search).

Notes:
- Sending an email is outward-facing and hard to undo — show the recipient,
  subject, and body to the user and get confirmation before sending.
