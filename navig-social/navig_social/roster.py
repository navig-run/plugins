"""Space-bound presence tracking: registry roster -> dated snapshot -> deltas.

``navig social stats <platform> <handle>`` answers *"what is this handle's count
right now"*. This module answers the question a portfolio owner actually asks:
*"what are ALL my accounts, and what moved since last time?"*

It reads a navig space's account registry, crawls every account through the same
:mod:`navig_social.stats` engine, writes a dated snapshot into the space, and diffs
it against the previous one -- so a verdict is never made on numbers of unknown age.

The stats engine deliberately stays space-free (it was extracted from the retired
navig-presence plugin precisely to drop the space coupling). This module is the
**optional** space layer on top of it: import it only when you want history.

Layout inside a space (both created on demand):

    <space>/.navig/memory/account-registry.json     the roster (public metadata only)
    <space>/analytics/raw/_snapshots/presence-YYYY-MM-DD.json

The snapshot schema is the one already written by hand into those directories, so
existing snapshots stay readable and a new one slots in beside them unchanged.
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Iterable

from navig_social.stats import StatResult, cdp_stop, crawl_account, platform_capability

REGISTRY_REL = (".navig", "memory", "account-registry.json")
SNAPSHOTS_REL = ("analytics", "raw", "_snapshots")
SNAPSHOT_PREFIX = "presence-"
SNAPSHOT_SUFFIX = ".json"

#: StatResult.status -> the snapshot's `counts` bucket. Kept explicit so a status the
#: engine adds later shows up as `failed` rather than being silently miscounted as ok.
_COUNT_BUCKET = {
    "ok": "ok",
    "needs-login": "needs_login",
    "needs-cdp": "needs_login",
    "needs-token": "needs_login",
    "error": "failed",
    "no-handle": "skipped",
    "unsupported": "skipped",
}


# --------------------------------------------------------------------------- paths

def resolve_space_dir(space: str) -> Path:
    """The directory of a named space.

    Uses ``discover_space_paths`` rather than ``resolve_space`` so an unknown name
    raises instead of quietly pointing at a directory that does not exist, where we
    would then write a snapshot nobody ever finds.

    A folder path works too, with or without navig: a space is just a folder with ``.navig/``
    in it, and on its own that folder is the only way to name one.
    """
    folder = Path(space).expanduser()
    if folder.is_dir():
        return folder.resolve()
    try:
        from navig.spaces.contracts import normalize_space_name
        from navig.spaces.resolver import discover_space_paths
    except ImportError:
        raise ValueError(
            f"no folder {space!r} here — pass the space's folder path (space names need navig)"
        ) from None

    cfg = (discover_space_paths() or {}).get(normalize_space_name(space))
    root = str(getattr(cfg, "path", "") or "") if cfg is not None else ""
    if not root:
        raise ValueError(f"space not found: {space!r} (see `navig space list`)")
    return Path(root)


def registry_path(space_dir: Path) -> Path:
    return Path(space_dir).joinpath(*REGISTRY_REL)


def snapshots_dir(space_dir: Path) -> Path:
    return Path(space_dir).joinpath(*SNAPSHOTS_REL)


def snapshot_path(space_dir: Path, on: _date) -> Path:
    return snapshots_dir(space_dir) / f"{SNAPSHOT_PREFIX}{on.isoformat()}{SNAPSHOT_SUFFIX}"


# --------------------------------------------------------------------------- roster

def load_roster(
    space_dir: Path,
    *,
    brand: str | None = None,
    platform: str | None = None,
) -> list[dict]:
    """Every account in the space's registry, optionally narrowed to one brand/platform.

    Accounts marked ``snapshot_status: "dead"`` are skipped -- a handle that no longer
    resolves must not be re-crawled on every run, nor counted as a fresh failure.
    """
    path = registry_path(space_dir)
    if not path.exists():
        raise FileNotFoundError(f"no account registry at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for acc in data.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        if (acc.get("snapshot_status") or "").lower() == "dead":
            continue
        if brand and (acc.get("brand") or "").lower() != brand.lower():
            continue
        if platform and (acc.get("platform") or "").lower() != platform.lower():
            continue
        out.append(acc)
    return out


def crawl_roster(
    accounts: Iterable[dict],
    *,
    allow_cdp: bool = True,
    do_login: bool = True,
    profile: str = "social-stats",
    on_result=None,
) -> list[tuple[dict, StatResult]]:
    """Crawl a roster, pairing every account with its result.

    Tears down any ``navig cdp`` browser this call launched exactly once at the end --
    never ``--all``, so a browser another session is driving is left alone.
    """
    paired: list[tuple[dict, StatResult]] = []
    used_cdp = False
    try:
        for acc in accounts:
            plat = (acc.get("platform") or "").strip().lower()
            if allow_cdp and platform_capability(plat).startswith("cdp"):
                used_cdp = True
            res = crawl_account(acc, allow_cdp=allow_cdp, do_login=do_login, profile=profile)
            paired.append((acc, res))
            if on_result is not None:
                on_result(acc, res)
    finally:
        if used_cdp:
            cdp_stop(profile)
    return paired


# --------------------------------------------------------------------------- snapshots

def snapshot_payload(
    paired: list[tuple[dict, StatResult]],
    *,
    generated: _date,
    source: str = "navig social presence crawl",
) -> dict:
    """Build the on-disk snapshot document from crawl results."""
    counts = {"ok": 0, "needs_login": 0, "failed": 0, "skipped": 0}
    results = []
    for acc, res in paired:
        counts[_COUNT_BUCKET.get(res.status, "failed")] += 1
        row = {
            "brand": acc.get("brand"),
            "platform": res.platform or acc.get("platform"),
            "handle": res.handle or acc.get("handle"),
            "followers": res.followers,
            "status": res.status,
            "source": res.source or "",
        }
        if res.extra:
            row["extra"] = res.extra
        if res.error:
            row["error"] = res.error
        results.append(row)
    return {
        "generated": generated.isoformat(),
        "source": source,
        "counts": counts,
        "results": results,
    }


def write_snapshot(space_dir: Path, payload: dict, *, on: _date) -> Path:
    path = snapshot_path(space_dir, on)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def list_snapshots(space_dir: Path) -> list[Path]:
    """Every snapshot in the space, oldest first.

    Sorted by the date in the filename, not by mtime -- re-running an old date must not
    reorder history.
    """
    d = snapshots_dir(space_dir)
    if not d.exists():
        return []
    found = [p for p in d.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_SUFFIX}") if p.is_file()]
    return sorted(found, key=lambda p: p.name)


def read_snapshot(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def previous_snapshot(space_dir: Path, *, excluding: Path | None = None) -> Path | None:
    """The newest snapshot other than ``excluding`` -- the one to diff against."""
    snaps = [
        p for p in list_snapshots(space_dir)
        if excluding is None or p.name != Path(excluding).name
    ]
    return snaps[-1] if snaps else None


# --------------------------------------------------------------------------- deltas

def _key(row: dict) -> tuple[str, str, str]:
    return (
        (row.get("brand") or "").lower(),
        (row.get("platform") or "").lower(),
        (row.get("handle") or "").lstrip("@").lower(),
    )


def index_counts(payload: dict) -> dict[tuple[str, str, str], int]:
    """(brand, platform, handle) -> followers, for rows that actually carry a number.

    Rows without a number are omitted rather than stored as 0, so a failed fetch can
    never be read back later as "dropped to zero".
    """
    out: dict[tuple[str, str, str], int] = {}
    for row in payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        val = row.get("followers")
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        out[_key(row)] = val
    return out


def deltas(previous: dict | None, current: dict) -> list[dict]:
    """Movement between two snapshots, largest absolute change first.

    An account missing from ``previous`` yields ``delta=None`` (new to tracking), never
    a delta equal to its entire count.
    """
    prev = index_counts(previous) if previous else {}
    rows = []
    for row in current.get("results") or []:
        if not isinstance(row, dict):
            continue
        now = row.get("followers")
        now = now if (isinstance(now, int) and not isinstance(now, bool)) else None
        was = prev.get(_key(row))
        rows.append({
            "brand": row.get("brand"),
            "platform": row.get("platform"),
            "handle": row.get("handle"),
            "followers": now,
            "previous": was,
            "delta": (now - was) if (now is not None and was is not None) else None,
            "status": row.get("status"),
            "error": row.get("error"),
        })
    rows.sort(key=lambda r: (r["delta"] is None, -abs(r["delta"] or 0)))
    return rows


def series(
    space_dir: Path,
    *,
    brand: str | None = None,
    platform: str | None = None,
) -> list[dict]:
    """One row per snapshot date per account: the full history, oldest first."""
    out = []
    for path in list_snapshots(space_dir):
        try:
            payload = read_snapshot(path)
        except (OSError, ValueError):
            continue
        when = payload.get("generated") or path.stem.replace(SNAPSHOT_PREFIX, "")
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            if brand and (row.get("brand") or "").lower() != brand.lower():
                continue
            if platform and (row.get("platform") or "").lower() != platform.lower():
                continue
            val = row.get("followers")
            if isinstance(val, bool) or not isinstance(val, int):
                continue
            out.append({
                "date": when,
                "brand": row.get("brand"),
                "platform": row.get("platform"),
                "handle": row.get("handle"),
                "followers": val,
            })
    return out


# --------------------------------------------------------------------------- registry write-back

def update_registry(
    space_dir: Path,
    paired: list[tuple[dict, StatResult]],
    *,
    on: _date,
) -> int:
    """Write successful counts back into the registry. Returns the number of rows changed.

    Only ``ok`` results are written: a failed or login-walled fetch must never overwrite
    the last number that was actually observed, or a single bad run erases the history
    the registry carries for every walled platform.
    """
    path = registry_path(space_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    by_key: dict[tuple[str, str, str], StatResult] = {}
    for acc, res in paired:
        if res.status != "ok" or res.followers is None:
            continue
        by_key[_key(acc)] = res

    changed = 0
    for acc in data.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        res = by_key.get(_key(acc))
        if res is None:
            continue
        if acc.get("public_followers") == res.followers and acc.get("last_snapshot") == on.isoformat():
            continue
        acc["public_followers"] = res.followers
        acc["last_snapshot"] = on.isoformat()
        if res.source:
            acc["source"] = res.source
        acc["snapshot_status"] = "fetched"
        changed += 1

    if changed:
        data["generated"] = on.isoformat()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed
