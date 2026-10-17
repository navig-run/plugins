"""Locate (and optionally fetch) the external programs the extractors call.

Resolution order per tool: ``NAVIG_UNPACK_<NAME>`` env var → PATH → ``~/.navig/tools/<dir>/``.
``setup()`` downloads the two GitHub-released tools that are rarely on PATH (vgmstream,
Il2CppDumper) into ``~/.navig/tools``; 7-Zip and ffmpeg come from the OS package manager.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

TOOLS_HOME = Path(os.environ.get("NAVIG_TOOLS_HOME", Path.home() / ".navig" / "tools"))


@dataclass(frozen=True)
class Tool:
    name: str
    exe: str            # executable name searched on PATH / inside the tools dir
    subdir: str         # folder under TOOLS_HOME when fetched by setup()
    purpose: str
    repo: str = ""      # GitHub owner/repo when setup() can fetch it
    asset: str = ""     # substring of the release asset to download (Windows build)


TOOLS = {
    "7z": Tool("7z", "7z", "7zip", "unpack APK/OBB/zip/rar (tolerates anti-tamper bad CRCs)"),
    "ffmpeg": Tool("ffmpeg", "ffmpeg", "ffmpeg", "encode audio to .ogg, decode Bink 1 video"),
    "vgmstream": Tool("vgmstream", "vgmstream-cli", "vgmstream", "decode game audio: Wwise .wem/.bnk, XACT .xwb, Bink audio",
                      repo="vgmstream/vgmstream", asset="vgmstream-win64.zip"),
    "il2cppdumper": Tool("il2cppdumper", "Il2CppDumper", "il2cppdumper", "rebuild IL2CPP class layouts (DummyDll) for Unity data",
                         repo="Perfare/Il2CppDumper", asset="net6-win"),
    "innoextract": Tool("innoextract", "innoextract", "innoextract", "unpack Inno Setup installers (PC games) without running them"),
}


def find(name: str) -> Optional[str]:
    """Absolute path of a tool, or None."""
    tool = TOOLS[name]
    env = os.environ.get(f"NAVIG_UNPACK_{name.upper()}")
    if env and Path(env).exists():
        return env
    hit = shutil.which(tool.exe)
    if hit:
        return hit
    folder = TOOLS_HOME / tool.subdir
    for cand in (folder / f"{tool.exe}.exe", folder / tool.exe):
        if cand.exists():
            return str(cand)
    return None


def require(name: str) -> str:
    path = find(name)
    if not path:
        tool = TOOLS[name]
        how = "run `navig mobile unpack setup`" if tool.repo else f"install {tool.name} and put it on PATH"
        raise FileNotFoundError(f"{tool.name} not found ({tool.purpose}) — {how}.")
    return path


def status() -> list[tuple[str, Optional[str], str]]:
    return [(t.name, find(key), t.purpose) for key, t in TOOLS.items()]


def python_deps() -> list[tuple[str, bool]]:
    """Optional Python packages of the [unpack] extra and whether they import."""
    out = []
    for mod in ("UnityPy", "TypeTreeGeneratorAPI", "lz4", "PIL", "pyaxmlparser"):
        try:
            __import__(mod)
            out.append((mod, True))
        except Exception:
            out.append((mod, False))
    return out


def setup(names: Optional[list[str]] = None) -> list[str]:
    """Download GitHub-released tools into TOOLS_HOME. Returns human-readable results."""
    if sys.platform != "win32":
        return ["setup only fetches Windows builds; on Linux/macOS install vgmstream-cli and Il2CppDumper "
                "from your package manager or their GitHub releases and put them on PATH."]
    results = []
    for key in names or [k for k, t in TOOLS.items() if t.repo]:
        tool = TOOLS[key]
        if not tool.repo:
            results.append(f"{tool.name}: not fetched automatically — install it via your package manager")
            continue
        if find(key):
            results.append(f"{tool.name}: already available at {find(key)}")
            continue
        api = f"https://api.github.com/repos/{tool.repo}/releases/latest"
        with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "navig"}), timeout=60) as r:
            release = json.load(r)
        asset = next((a for a in release.get("assets", []) if tool.asset in a["name"] and a["name"].endswith(".zip")), None)
        if asset is None:
            results.append(f"{tool.name}: no asset matching {tool.asset!r} in {release.get('tag_name')}")
            continue
        with urllib.request.urlopen(asset["browser_download_url"], timeout=600) as r:
            data = r.read()
        dest = TOOLS_HOME / tool.subdir
        dest.mkdir(parents=True, exist_ok=True)
        zipfile.ZipFile(io.BytesIO(data)).extractall(dest)
        if key == "il2cppdumper":
            _quiet_il2cppdumper(dest)
        results.append(f"{tool.name}: {release.get('tag_name')} → {dest}")
    return results


def _quiet_il2cppdumper(folder: Path) -> None:
    """Il2CppDumper waits for a key press by default; extraction needs it non-interactive with string literals."""
    cfg = folder / "config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data.update({"RequireAnyKey": False, "GenerateStruct": True, "GenerateDummyDll": True})
        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
