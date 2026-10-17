"""The media modules that stay in navig-generate: beats, tonality and the pixel pipeline.

The shared engine (types, generation service, refs library, variant store, provider catalog,
ffmpeg primitives) was extracted to ``navig-media-core``. ``navig.media.<module>`` aliases
each module to whichever package owns it.
"""
