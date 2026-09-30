"""Beats — named styles, bar-exact composition plans, and the catalogue a render leaves behind.

``navig audio beat`` is for one job: a beat somebody will rap on. That is a different
request from "make me music" — the tempo has to be a number the rapper counts, the
sections have to be whole bars, there must be no vocals, and the file has to carry its
own prompt so the keeper can be re-rendered. The three modules split that up:

- :mod:`.styles`  — the named presets (built-in genres + a project's own file).
- :mod:`.plan`    — style + overrides → the provider's ``composition_plan``.
- :mod:`.catalog` — the sidecar, the ``INDEX.md`` row, and the tempo/key post-check.
"""
