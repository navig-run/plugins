---
name: calendar
description: Manage calendar events — create, list, update, or check availability. Use when the user wants to schedule a meeting, add or move a calendar event, see their agenda, or check when they are free or busy.
activation_keywords: [calendar, schedule, meeting]
metadata:
  version: 1.0.0
  toolsAllowed:
    - bash_exec
---

# Calendar

This capability is provided by the **navig-calendar** plugin. Use `navig
calendar <command>` for calendar work.

- Run `navig calendar --help` to see the available commands (list events, create
  an event, check availability, and more).

Notes:
- Confirm the date, time, timezone, and attendees with the user before creating
  or moving an event. Prefer `navig calendar` over editing calendar files directly.
