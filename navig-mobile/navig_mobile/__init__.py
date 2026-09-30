"""navig-mobile — pro-grade Android + iOS device operations for NAVIG.

A first-party navig plugin. The base package imports NO third-party libraries at
module load (keeps ``navig help`` fast); the Android/iOS engines are imported
lazily only when a device is actually touched.
"""

from __future__ import annotations

__version__ = "0.1.0"
