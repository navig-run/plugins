"""navig-email plugin ModuleDef — settings_schema contract coverage.

The desktop OS renders ``settings_schema`` fields on apps/email/settings and
persists values through the generic per-app settings endpoint
(``core/navig/gateway/deck/routes/app_settings.py``), which enforces the key
regex and reserves ``enabled`` — so a schema typo here would render a field
whose writes 400. These tests pin the contract on the plugin side.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Test THIS checkout's plugin, not whichever copy pip has installed editable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navig_email.plugin import _email_module_def  # noqa: E402

# Mirrors the gateway app_settings route's key validation.
KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# Mirrors the OS renderer's `fieldsFromServerSchema` known kinds.
KNOWN_KINDS = {"toggle", "select", "segmented", "number", "text"}


def test_module_def_constructs():
    m = _email_module_def()
    assert m.id == "email"
    assert "os-tile:email" in m.surfaces
    assert m.app_category == "Comms"


def test_settings_schema_matches_endpoint_contract():
    schema = _email_module_def().settings_schema
    assert schema, "email declares at least one settings field"
    keys = [f["key"] for f in schema]
    assert len(keys) == len(set(keys)), "duplicate settings keys"
    for f in schema:
        assert KEY_RE.match(f["key"]), f"key rejected by the endpoint: {f['key']}"
        assert f["key"] != "enabled", "'enabled' is reserved by the endpoint"
        assert f["kind"] in KNOWN_KINDS, f"OS renderer skips kind: {f['kind']}"
        assert isinstance(f["label"], str) and f["label"].strip()


def test_default_query_field():
    schema = _email_module_def().settings_schema or []
    field = {f["key"]: f for f in schema}["default_query"]
    assert field["kind"] == "text"
    assert isinstance(field["default"], str) and field["default"]
    assert isinstance(field.get("max_length"), int)
