"""Channel texts — the master → scripts sync, its lint, the terminology sweep and data.

A channel (the @sch8ma promo pipeline) keeps ONE master text file
(``out/ALL-TEXTS*.md``) and renders everything from it. This package owns the
machinery that used to live in the workspace's ``.tmp/sync_rev6.py`` and friends:

* :mod:`~navig_text.channel.parse` — read the master into structured sections.
* :mod:`~navig_text.channel.sync` — push master values into the channel's scripts,
  positional by structure, never by text generation.
* :mod:`~navig_text.channel.lint` — the rules the master's own header declares
  (unique ids, beat counts, asterisk balance, casing, no vendor names).
* :mod:`~navig_text.channel.sweep` — a literal terminology sweep (cities → Zones).
* :mod:`~navig_text.channel.data` — the live numbers the cards quote, cached and
  never silently stale.

Only code lives here; the master file and the scripts stay in the workspace.
"""
