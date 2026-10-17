"""navig-mini — Lightweight AI agent for low-power devices."""

# THE single runtime source of the package version. `pyproject.toml` is the publish
# source and must equal this; agent.py / install.py / install.sh carry copies they can
# fall back to when they run without the package (a deployed box gets agent.py alone).
# Every one of those copies is pinned to this by tests/test_version_single_source.py,
# so a bump cannot land on one surface and not the others.
__version__ = "1.1.4"
