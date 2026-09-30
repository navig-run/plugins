"""Lyrics that sit on a beat — the writing half of ``navig audio beat``.

* :mod:`~navig_text.lyrics.fit` — syllables per line against the time one bar gives.
* :mod:`~navig_text.lyrics.scaffold` — an empty lyric cut to a beat's *measured* bar map.
* :mod:`~navig_text.lyrics.sheet` — a text's hook + voices table → a guide-demo sheet.
* :mod:`~navig_text.lyrics.bank` — search a word bank; find spelling rhymes in it.

Everything here is standard library and reads files only: no AI, no audio, no network.
A beat's bar map comes from its JSON sidecar (``measured.bars``, written by
``navig audio beat gen``); navig-audio is used only when it is installed and a map has
to be measured from the audio itself.
"""
