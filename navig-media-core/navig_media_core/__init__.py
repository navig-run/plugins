"""navig-media-core — the shared media foundation NAVIG's media packages run on.

A library, not a program: no CLI, no entry points, and it imports without navig installed
(navig's own services are reached through guarded, function-local imports by the modules
that need them). Extracted from ``navig-generate`` so generate, audio, text and pipeline
share one copy instead of reaching into a sibling plugin. It never imports those plugins:
generation orchestration lives in ``navig_generate.media.generation_service`` (ADR-0002).

Public paths never moved: ``navig.media.<module>``, ``navig.store.generated_media`` and
``navig.tools.media_providers`` are identity aliases of the modules here, exactly as they
were of their ``navig_generate`` homes.
"""

__version__ = "0.2.0"
