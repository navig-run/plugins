---
name: calendar
description: Manage calendar events — create, list, update, or check availability. Use when the user wants to schedule a meeting, add or move a calendar event, see their agenda, or check when they are free or busy.
activation_keywords: [calendar, schedule, meeting]
# `agenda` / `reunion` are also English words, so French uses phrases (packs score on
# English messages too and must not change what English text activates).
activation_keywords_ru: [календар, встреч, расписан, агенд, "когда я свободен", "когда свободен"]
activation_keywords_fr: [calendrier, "mon agenda", "l'agenda", "une reunion", "ma reunion", "la reunion", "cette reunion", "rendez-vous", rdv]
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
