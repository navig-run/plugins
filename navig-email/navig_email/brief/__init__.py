"""The mailroom briefing: facts extracted from newsletters, merged across emails, delivered.

Not an email summariser. Stage 1 (:mod:`.extract`) turns each source email into individual
items — a product, an event, a job, an article — with the exact fields the source states.
Stage 2 (:mod:`.synthesize`) merges the same item seen in several emails, drops what an earlier
briefing already delivered, ranks what is left, and keeps every item's source references.
:mod:`.schedule` decides when a briefing is due (and postpones a thin one); :mod:`.run`
delivers it and, only after a verified delivery, lets :mod:`navig_email.cleanup` archive the
sources.
"""
