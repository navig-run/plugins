"""navig-text — AI text generation facet (music/SFX has audio; this is text).

A first-party navig plugin: `navig text gen` drafts Markdown documents via core's
AI client and registers a TEXT backend into the shared generation engine, so
`navig generate gen --modality text` works and drafted text flows into the fan-out.
"""

__version__ = "0.1.0"
