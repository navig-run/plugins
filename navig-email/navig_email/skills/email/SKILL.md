---
name: email
description: Send, read, or search email. Use when the user wants to send or draft an email, check their inbox, or find a specific message.
activation_keywords: [email, inbox]
# Per-language packs (word-start stems; see core/navig/agent/skills_context.py). No bare
# `почт`: it is a prefix of "почти" (almost), so the forms are listed explicitly.
activation_keywords_ru: [письм, почта, почту, почты, почтой, почтов, емейл, емаил, имейл, эмейл, мейл, мыло, мыла, мылу, мылом]
activation_keywords_fr: [mail, "e-mail", courriel, courrier, "boite de reception", "boite mail"]
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
