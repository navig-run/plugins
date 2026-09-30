"""Chromium-family (Chrome/Edge/Brave) extension malware/PUP scanning and
Chrome profile-index recovery — stdlib only.

The extension scanner reads each profile's ``Secure Preferences`` (which embeds
every installed extension's manifest + install metadata), scores each against
malware/PUP heuristics, and dedupes by id across profiles. The recovery routine
rebuilds ``Local State -> profile.info_cache`` from each profile's own
``Preferences`` when Chrome's profile picker collapses to just "Default".

Nothing here writes to a profile's data. Only ``recover_profiles(apply=True)``
writes — and only to ``Local State`` (the index), after a backup, and only when
the browser is fully closed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── browser user-data locations ─────────────────────────────────────────────

_BROWSERS = {
    "chrome": ("Google", "Chrome"),
    "edge": ("Microsoft", "Edge"),
    "brave": ("BraveSoftware", "Brave-Browser"),
}
_PROFILE_RE = re.compile(r"^(Default|Profile \d+)$")
_BROAD_HOST = re.compile(r"<all_urls>|\*://\*/|https?://\*/")
# Chrome Manifest `location` enum
_LOC = {
    1: "webstore/internal", 2: "external_pref", 3: "external_registry",
    4: "UNPACKED(devmode)", 5: "component", 6: "external_pref_dl",
    7: "external_policy_dl", 8: "command_line", 9: "ext_component", 10: "external_policy",
}


def user_data_dir(browser: str = "chrome") -> Path | None:
    """Default per-browser User Data directory for the current OS/user."""
    vendor = _BROWSERS.get(browser.lower())
    if not vendor:
        return None
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        p = base.joinpath(*vendor, "User Data")
    elif sys.platform == "darwin":
        names = {"chrome": "Google/Chrome", "edge": "Microsoft Edge",
                 "brave": "BraveSoftware/Brave-Browser"}
        p = Path.home() / "Library" / "Application Support" / names.get(browser.lower(), "Google/Chrome")
    else:
        names = {"chrome": "google-chrome", "edge": "microsoft-edge",
                 "brave": "BraveSoftware/Brave-Browser"}
        p = Path.home() / ".config" / names.get(browser.lower(), "google-chrome")
    return p if p.exists() else None


def list_profiles(ud: Path) -> list[str]:
    """Profile directory names (Default, Profile 1, …) that have a Preferences file."""
    out: list[str] = []
    for d in ud.iterdir() if ud.exists() else []:
        if d.is_dir() and _PROFILE_RE.match(d.name) and (d / "Preferences").exists():
            out.append(d.name)
    out.sort(key=lambda n: (n != "Default", int(n.split(" ")[1]) if " " in n else 0))
    return out


def browser_running(browser: str = "chrome") -> bool:
    """True if the browser process is running (its data files are then locked)."""
    exe = {"chrome": "chrome.exe", "edge": "msedge.exe", "brave": "brave.exe"}.get(browser.lower(), "chrome.exe")
    try:
        if sys.platform.startswith("win"):
            # BYTES, not text=True: tasklist writes the OEM code page while text=True
            # decodes with the ANSI one, and its output carries non-ASCII (the localized
            # header). An .exe name is ASCII, so match without decoding — otherwise a
            # strict encoding= would raise here, this except: would swallow it, and the
            # caller would be told the browser is closed while it is open, which is
            # precisely when its data files are locked.
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                                 capture_output=True, timeout=15).stdout
            return exe.lower().encode() in out.lower()
        base = exe.replace(".exe", "")
        return subprocess.run(["pgrep", "-x", base], capture_output=True).returncode == 0
    except Exception:
        return False


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── extension malware/PUP scan ──────────────────────────────────────────────

@dataclass
class ExtFinding:
    ext_id: str
    name: str
    band: str            # CRITICAL | HIGH | MEDIUM | LOW
    score: int
    flags: list[str]
    source: str          # install location (webstore/UNPACKED/external_registry/…)
    from_webstore: object
    manifest_version: object
    profiles: list[str] = field(default_factory=list)


# extensions whose id is a known malware/PUP family (extend as needed)
_KNOWN_BAD = {
    "edkbpkanapinjifakjogefooogoclehg": "saveVPN / Savematic (adware + RCE backdoor)",
}


def _score_ext(ext_id: str, ext: dict) -> tuple[int, list[str], str]:
    m = ext.get("manifest") or {}
    perms = [p for p in (m.get("permissions") or []) if isinstance(p, str)]
    hosts = list(m.get("host_permissions") or []) + [p for p in perms if "://" in p or p == "<all_urls>"]
    broad = "<all_urls>" in perms or any(_BROAD_HOST.search(str(h)) for h in hosts)
    cs = m.get("content_scripts") or []
    cs_broad = any(any(_BROAD_HOST.search(str(x)) for x in (c.get("matches") or [])) for c in cs)

    flags: list[str] = []
    score = 0
    has = perms.__contains__
    if ext_id in _KNOWN_BAD:
        flags.append(f"KNOWN-MALWARE: {_KNOWN_BAD[ext_id]}"); score += 20
    if has("proxy"):                       flags.append("proxy(traffic-MITM)"); score += 5
    if has("debugger"):                    flags.append("debugger"); score += 5
    if has("cookies") and broad:           flags.append("cookies+allsites"); score += 4
    if (has("webRequest") or has("webRequestBlocking")) and broad:
        flags.append("webRequest+allsites"); score += 4
    if has("management"):                  flags.append("management(controls other exts)"); score += 3
    if has("nativeMessaging"):             flags.append("nativeMessaging"); score += 3
    if cs_broad:                           flags.append("contentscript-allsites"); score += 1
    if has("privacy"):                     flags.append("privacy"); score += 1
    if has("browsingData"):                flags.append("browsingData"); score += 1
    if m.get("manifest_version") == 2:     flags.append("MV2(legacy)"); score += 1
    if ext.get("from_webstore") is False:  flags.append("NOT-from-webstore"); score += 3
    if ext.get("location") == 4:           flags.append("UNPACKED-devmode"); score += 3
    upd = m.get("update_url") or ""
    if upd and "google.com" not in upd:    flags.append("non-google update_url"); score += 3
    csp = m.get("content_security_policy")
    csp_s = csp if isinstance(csp, str) else (csp or {}).get("extension_pages", "") if isinstance(csp, dict) else ""
    if isinstance(csp_s, str) and "unsafe-eval" in csp_s:
        flags.append("unsafe-eval"); score += 2
    if re.search(r"\bvpn\b|coupon|savematic|cashback|shopping.?assistant", m.get("name") or "", re.I):
        flags.append("name-suspicious"); score += 2
    return score, flags, _LOC.get(ext.get("location"), str(ext.get("location")))


def _band(score: int) -> str:
    return "CRITICAL" if score >= 8 else "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW"


def scan_extensions(ud: Path, min_score: int = 3) -> tuple[list[ExtFinding], int, int, int, int]:
    """Return ``(findings>=min_score sorted by score desc, total_installed, unique,
    n_profiles, unreadable)``.

    ``unreadable`` counts profiles whose ``Secure Preferences`` EXISTS but could not be
    parsed — the file was locked (the browser is running), permission was denied, or it
    was corrupt. Such a profile silently contributes ZERO extensions, so a caller that
    ignored this count would report a green "clean" over data it never actually read (a
    running browser or a hostile extension corrupting the store could hide behind that
    false clean). A *missing* ``Secure Preferences`` is NOT counted: a profile with no
    extensions legitimately has no store, so counting it would cry wolf on every scan."""
    by_id: dict[str, ExtFinding] = {}
    total = 0
    unreadable = 0
    profs = list_profiles(ud)
    for prof in profs:
        sp = ud / prof / "Secure Preferences"
        if not sp.exists():
            continue  # no extension store for this profile — nothing to scan, not an error
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            unreadable += 1  # exists but unreadable — a real gap in coverage, NOT "clean"
            continue
        settings = ((data.get("extensions") or {}).get("settings")) or {}
        for ext_id, ext in settings.items():
            m = ext.get("manifest") or {}
            if not m or m.get("theme") or ext.get("location") in (5, 9):
                continue  # skip themes + built-in component extensions
            total += 1
            if ext_id not in by_id:
                score, flags, source = _score_ext(ext_id, ext)
                by_id[ext_id] = ExtFinding(
                    ext_id=ext_id, name=m.get("name") or "(no name)", band=_band(score),
                    score=score, flags=flags, source=source,
                    from_webstore=ext.get("from_webstore"),
                    manifest_version=m.get("manifest_version"),
                )
            by_id[ext_id].profiles.append(prof)
    findings = sorted((f for f in by_id.values() if f.score >= min_score),
                      key=lambda f: f.score, reverse=True)
    return findings, total, len(by_id), len(profs), unreadable


# ── Chrome profile-index recovery ───────────────────────────────────────────

@dataclass
class RecoveryPlan:
    user_data: Path
    kept: list[str]
    readd: list[tuple[str, str, str]]  # (dir, name, email)
    indexed_before: int
    indexed_after: int


def _entry_from_prefs(ud: Path, dir_name: str, existing: dict | None, bucket: int) -> dict:
    prefs = _read_json(ud / dir_name / "Preferences")
    prof = prefs.get("profile") or {}
    acct = (prefs.get("account_info") or [{}])[0] if prefs.get("account_info") else {}
    avatar_idx = prof.get("avatar_index") if isinstance(prof.get("avatar_index"), int) else 26
    try:
        mtime = (ud / dir_name).stat().st_mtime
    except OSError:
        mtime = 0
    base = dict(existing) if isinstance(existing, dict) else {}
    return {
        "name": base.get("name") or prof.get("name") or dir_name,
        "is_using_default_name": base.get("is_using_default_name", False),
        "is_using_default_avatar": base.get("is_using_default_avatar", False),
        "avatar_icon": base.get("avatar_icon") or f"chrome://theme/IDR_PROFILE_AVATAR_{avatar_idx}",
        "user_name": base.get("user_name") or acct.get("email") or "",
        "gaia_id": base.get("gaia_id") or acct.get("gaia") or "",
        "gaia_name": base.get("gaia_name") or acct.get("full_name") or "",
        "gaia_given_name": base.get("gaia_given_name") or acct.get("given_name") or "",
        "is_ephemeral": False,
        "is_consented_primary_account": base.get("is_consented_primary_account", bool(acct.get("email"))),
        "background_apps": base.get("background_apps", False),
        "metrics_bucket_index": base.get("metrics_bucket_index", bucket),
        "active_time": base.get("active_time", mtime),
    }


def plan_recovery(ud: Path) -> RecoveryPlan:
    ls = _read_json(ud / "Local State")
    cache = (ls.get("profile") or {}).get("info_cache") or {}
    dirs = list_profiles(ud)
    kept, readd = [], []
    for name in dirs:
        if name in cache:
            kept.append(name)
        else:
            e = _entry_from_prefs(ud, name, None, 0)
            readd.append((name, e["name"], e["user_name"]))
    return RecoveryPlan(ud, kept, readd, len(cache), len(dirs))


def recover_profiles(ud: Path, apply: bool, backup_dir: Path, browser: str = "chrome") -> tuple[RecoveryPlan, Path | None]:
    """Rebuild info_cache from on-disk Preferences. apply=False = dry-run (no write).
    Returns (plan, backup_path_or_None). Raises RuntimeError if apply and the browser is open.

    ``browser`` selects the process to check for the open-browser guard — Edge/Brave
    use the same Local State structure, so recovery works for them too, but the guard
    MUST check the matching process, else a write lands under a live browser and gets
    overwritten (or corrupted) on its next flush."""
    plan = plan_recovery(ud)
    if not apply:
        return plan, None
    if browser_running(browser):
        raise RuntimeError(f"{browser.capitalize()} is running — close it fully before --apply (it would overwrite the fix).")

    ls_path = ud / "Local State"
    ls = _read_json(ls_path)
    ls.setdefault("profile", {})
    cache_before = ls["profile"].get("info_cache") or {}
    used = {e.get("metrics_bucket_index") for e in cache_before.values() if isinstance(e, dict)}
    nxt = 0

    def next_bucket() -> int:
        nonlocal nxt
        while nxt in used:
            nxt += 1
        used.add(nxt)
        return nxt

    new_cache: dict[str, dict] = {}
    for name in list_profiles(ud):
        existing = cache_before.get(name)
        bucket = existing.get("metrics_bucket_index") if isinstance(existing, dict) and existing.get("metrics_bucket_index") is not None else next_bucket()
        new_cache[name] = _entry_from_prefs(ud, name, existing, bucket)

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    backup = backup_dir / f"Local State.{stamp}.bak"
    shutil.copyfile(ls_path, backup)

    ls["profile"]["info_cache"] = new_cache
    ls["profile"]["profiles_order"] = list_profiles(ud)
    # Atomic write: a torn Local State corrupts the profile index worse than the
    # collapse we're fixing. Write a sibling temp, then os.replace() (same-dir rename);
    # on any failure the original is untouched and we leave no half-written temp behind.
    tmp = ls_path.with_name(ls_path.name + ".navig-tmp")
    try:
        tmp.write_text(json.dumps(ls), encoding="utf-8")
        os.replace(tmp, ls_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return plan, backup
