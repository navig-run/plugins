"""Content-recognition pass for a photo library.

Adds the signals EXIF cannot give you — who is in a photo, where it was taken,
what is in it, and *when* it was actually shot — to any folder that
``navig explore probe`` has already walked.

Everything is keyed on **content hash, not path**, so labels survive the
reorganisations this package itself proposes. Indexing is strictly read-only;
only the ``--apply`` verbs move files, and every one of those writes an undo log.
"""
from __future__ import annotations

__all__ = ["catalog", "classify", "dates", "embed", "faces", "pipeline"]
