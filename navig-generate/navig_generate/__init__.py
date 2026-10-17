"""navig-generate — generate and edit images, video and audio.

Runs on its own (``navig-generate``) and inside navig (``navig generate``). The shared media
engine it used to house lives in ``navig-media-core`` (types, generation service, refs, store,
provider catalog, ffmpeg primitives); this package keeps the generator clients, beats,
tonality, the pixel pipeline, the deck routes and the CLI.
"""

__version__ = "0.4.1"
