"""navig-mobile device engines.

``engine.base`` holds the platform-agnostic abstractions (``Device`` protocol,
``DeviceInfo``, ``DeviceManager``) and imports no third-party libs. The
``engine.android`` and ``engine.ios`` subpackages lazily import ``adbutils`` /
``pymobiledevice3`` inside their functions and raise ``PlatformUnavailableError``
with an install hint when the relevant extra is not installed.
"""
