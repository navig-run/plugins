"""navig-media — the NAVIG media suite bundle.

A metapackage: it installs the six suite components (generate · audio · text ·
pipeline · download · social) and exposes the ``navig-media`` command, which reports
what of the studio is installed. No navig import happens at module scope — the whole
point of the bundle is that each part runs on its own.
"""

from __future__ import annotations

__version__ = "0.2.0"
