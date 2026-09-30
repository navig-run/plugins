---
name: social-publishing
description: Publish or schedule posts to social networks (Facebook, Instagram, LinkedIn, YouTube, Threads, Pinterest) and manage a Facebook Page. Use when the user wants to post, publish, schedule, or share content to social media, or manage their Facebook page.
activation_keywords: [facebook, instagram, linkedin, pinterest]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Social Publishing

This capability is provided by the **navig-social** plugin.

- `navig social connect <network>` / `navig social status` — connect and check
  the linked social accounts (Facebook, Instagram, LinkedIn, YouTube, Threads,
  Pinterest, dev.to).
- `navig social publish ...` — publish or schedule a post (fan-out across networks).
- `navig facebook` (alias `navig fb`) — manage a Facebook Page (info, photos,
  captions, backup, delete).

Run `navig social --help` / `navig facebook --help` for the full surface.

Notes:
- Publishing is public and hard to undo — confirm the content, target network(s),
  and timing with the user before posting; bulk delete is backup-gated.
