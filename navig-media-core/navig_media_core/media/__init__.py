"""The shared media foundation: asset types + generator registry, refs library, variant
store, context builder, ffmpeg primitives.

The modules here are aliased by ``navig.media.<module>``. Generation orchestration
(``generation_service``), the provider clients, beats, tonality and the pixel pipeline are
owned by ``navig-generate``; this package never imports it (ADR-0002, enforced by
``tests/test_layering.py``). ``generation_service`` here is a deprecated identity alias.
"""
