"""Cross-repo hash parity + Studio post identity (G2, SOCIAL-CONTRACT §3)."""
from __future__ import annotations

import json
from pathlib import Path

from navig_social.social.content_hash import contract_hash, of_post

FIXTURE = Path(__file__).parent / "fixtures" / "contract_hash.json"


def test_contract_hash_matches_the_schema_pinned_fixture() -> None:
    """The load-bearing pin: SCHEMA's navig_contract.content_hash computed
    `expected` on 2026-10-15; our implementation must never drift from it."""
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert contract_hash(fx["manifest"]) == fx["expected"]


def test_contract_hash_is_stable_and_order_independent() -> None:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    m = fx["manifest"]
    shuffled = json.loads(json.dumps(m))
    shuffled["hashtags"] = list(reversed(shuffled["hashtags"]))
    shuffled["destinations"] = list(reversed(shuffled["destinations"]))
    shuffled["media"] = list(reversed(shuffled["media"]))
    assert contract_hash(shuffled) == contract_hash(m)


def test_contract_hash_excludes_derived_media_but_includes_built() -> None:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    m = json.loads(json.dumps(fx["manifest"]))
    # touching a derived entry's bytes does NOT void an approval (protected
    # separately by frozen shas on the SCHEMA side) …
    for e in m["media"]:
        if e["state"] == "derived":
            e["sha256"] = "deadbeef" * 8
    assert contract_hash(m) == fx["expected"]
    # … but a BUILT media change does
    for e in m["media"]:
        if e["state"] == "built":
            e["sha256"] = "beefdead" * 8
    assert contract_hash(m) != fx["expected"]


def test_of_post_is_stable_and_changes_with_content_or_targets() -> None:
    content = {"title": "t", "body": "b", "hashtags": ["x", "y"],
               "media": [], "per_network": {}}
    targets = [{"network": "telegram", "target": "@sch8ma"}]
    h1 = of_post(content, targets)
    assert h1 == of_post(dict(content), list(targets))
    assert of_post({**content, "body": "changed"}, targets) != h1
    assert of_post(content, [{"network": "x", "target": ""}]) != h1
    # order-insensitive targets
    assert of_post(content, list(reversed(targets))) == h1


def test_of_post_honors_an_explicit_contract_hash() -> None:
    pinned = "sha256:" + "a" * 64
    assert of_post({"content_hash": pinned, "body": "whatever"}, []) == pinned
