"""Pull a game off a device and run every applicable extractor on it.

``pull()`` copies APK + splits, OBB data files and ``/sdcard/Android/data/<pkg>`` into a game folder;
``detect()`` names the engine(s); ``run()`` executes the matching steps and writes ``REPORT.md``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from navig_mobile.unpack import kairo, media, tools, unity, xnb

TEXT_EXT = {".json", ".xml", ".csv", ".tsv", ".txt", ".lua", ".ini", ".cfg", ".yaml", ".yml", ".sql", ".tsx", ".tmx",
            ".atlas", ".properties", ".md", ".html", ".js", ".po", ".strings", ".loc"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga", ".ktx", ".pvr", ".astc", ".svg"}
AUDIO_EXT = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus"}
CONTAINER_AUDIO = {".wem": False, ".bnk": True, ".fsb": True, ".xwb": True, ".bank": True, ".awb": True, ".acb": True}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".ogv", ".m4v", ".bik", ".bk2", ".usm"}
FONT_EXT = {".ttf", ".otf", ".fnt"}
MODEL_EXT = {".obj", ".fbx", ".gltf", ".glb", ".dae", ".g3db", ".g3dj"}
BIG_UNKNOWN = 8 * 1024 * 1024  # unknown blobs this large are scanned for embedded Ogg audio
# carve_ogg reads the WHOLE blob into memory — a 35 GB nested .rarball OOM-killed the batch
# twice before this cap. Real Ogg-bearing blobs (banks, streams) never approach it.
MAX_CARVE = 512 * 1024 * 1024
SKIP_CARVE_EXT = {".7z", ".zip", ".rar", ".iso", ".tar", ".gz", ".bz2", ".xz", ".pak", ".bundle"}

Log = Callable[[str], None]


def _adb(serial: str, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, **kw)


def pull(serial: str, package: str, game: Path, log: Log = print) -> dict:
    """Copy everything the device holds for ``package`` into ``game`` (read-only on the device)."""
    game.mkdir(parents=True, exist_ok=True)
    (game / "apk").mkdir(exist_ok=True)
    paths = [ln.split(":", 1)[1].strip() for ln in _adb(serial, "shell", "pm", "path", package).stdout.splitlines()
             if ln.startswith("package:")]
    if not paths:
        raise FileNotFoundError(f"{package} is not installed on {serial}")
    for p in paths:
        _adb(serial, "pull", p, str(game / "apk" / Path(p).name))
    version = next((ln.strip() for ln in _adb(serial, "shell", "dumpsys", "package", package).stdout.splitlines()
                    if "versionName=" in ln), "")
    result = {"package": package, "version": version.replace("versionName=", ""), "apks": len(paths)}
    for remote, local in ((f"/sdcard/Android/obb/{package}", "obb"), (f"/sdcard/Android/data/{package}", "android-data")):
        if _adb(serial, "shell", "ls", remote).returncode == 0:
            tmp = game / f".{local}-pull"
            _adb(serial, "pull", remote, str(tmp))
            src = next(tmp.iterdir(), None) if tmp.exists() else None
            if src is not None:
                dest = game / local
                if dest.exists():
                    shutil.rmtree(dest)
                if local == "obb":  # flatten: obb/<files>
                    dest.mkdir()
                    for f in src.rglob("*"):
                        if f.is_file():
                            f.replace(dest / f.name)
                else:
                    src.replace(dest)
            shutil.rmtree(tmp, ignore_errors=True)
            result[local] = sum(f.stat().st_size for f in (game / local).rglob("*") if f.is_file()) if (game / local).exists() else 0
    (game / "SOURCE.json").write_text(json.dumps({**result, "serial": serial, "pulled": time.strftime("%Y-%m-%d %H:%M")},
                                                 indent=2), encoding="utf-8")
    log(f"pulled {package}: {result}")
    return result


def detect(game: Path) -> list[str]:
    u = game / "unpacked/assets"
    engines = []
    if (u / "bin/Data").exists():
        engines.append("unity")
    if xnb.is_xnb_game(game):
        engines.append("xnb")
    if (u / "www/index.html").exists():
        engines.append("html5")
    if (game / "data").exists():  # PC install: <Game>_Data folders, loose Content/*.xnb
        if any(p.is_dir() and p.name.endswith("_Data") for p in (game / "data").iterdir()):
            engines.append("unity")
        if next((game / "data").rglob("*.xnb"), None) is not None:
            engines.append("xnb")
    if any(game.joinpath("apk").glob("*.apk")):
        import zipfile
        for apk in game.joinpath("apk").glob("*.apk"):
            names = zipfile.ZipFile(apk).namelist()
            if any("libgodot" in n for n in names) or any(n.endswith(".pck") for n in names):
                engines.append("godot")
            if any("libgdx" in n for n in names):
                engines.append("libgdx")
            if any("libUE4" in n or "libUnreal" in n for n in names):
                engines.append("unreal")
    return sorted(set(engines)) or ["unknown"]


def unpack_all(game: Path) -> Path:
    """Full extraction of every APK/OBB (not only assets/) into ``unpacked/`` — for non-Unity engines."""
    seven = tools.require("7z")
    out = game / "unpacked"
    for pkg in sorted((game / "apk").glob("*.apk")) + sorted((game / "obb").glob("*")):
        mode = "-aoa" if pkg.parent.name == "obb" else "-aos"
        subprocess.run([seven, "x", "-y", mode, f"-o{out}", str(pkg)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def generic(game: Path, log: Log = print, *, roots: Optional[list[Path]] = None,
            skip_video: bool = False) -> dict:
    """Engine-agnostic pass: sort every file by type into extract/ and media/, convert what is convertible.

    ``roots`` overrides the scanned inputs (PC pipeline passes ``data/``); ``skip_video`` leaves
    video files out of the export entirely.
    """
    roots = [r for r in (roots if roots is not None else [game / "unpacked", game / "android-data"])
             if r.exists()]
    counts: dict[str, int] = {}

    def put(kind: str, f: Path, root: Path, base: Path):
        dest = base / kind / root.name / f.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        counts[kind] = counts.get(kind, 0) + 1

    audio_containers: list[Path] = []
    big_unknown: list[Path] = []
    for root in roots:
        for f in root.rglob("*"):
            if not f.is_file() or "/lib/" in f.as_posix() or f.suffix.lower() in (".dex", ".so", ".arsc"):
                continue
            ext = f.suffix.lower()
            if skip_video and ext in VIDEO_EXT:
                counts["video skipped"] = counts.get("video skipped", 0) + 1
                continue
            if ext in TEXT_EXT:
                put("text", f, root, game / "extract")
            elif ext in IMAGE_EXT:
                put("images", f, root, game / "media")
            elif ext in AUDIO_EXT:
                put("audio", f, root, game / "media")
            elif ext in VIDEO_EXT:
                put("video", f, root, game / "media")
            elif ext in FONT_EXT:
                put("fonts", f, root, game / "media")
            elif ext in MODEL_EXT:
                put("models", f, root, game / "media")
            elif ext in CONTAINER_AUDIO:
                audio_containers.append(f)
            elif ext == ".dds":
                counts["dds"] = counts.get("dds", 0) + 1
            elif f.stat().st_size >= BIG_UNKNOWN:
                counts["big unknown files"] = counts.get("big unknown files", 0) + 1
                if ext not in SKIP_CARVE_EXT and f.stat().st_size <= MAX_CARVE:
                    big_unknown.append(f)
    for root in roots:
        ok, bad = media.dds_to_png(root, game / "media/images" / root.name)
        if ok or bad:
            counts["dds -> png"] = counts.get("dds -> png", 0) + ok
    if audio_containers and tools.find("vgmstream") and tools.find("ffmpeg"):
        for ext, subsongs in CONTAINER_AUDIO.items():
            if any(f.suffix.lower() == ext for f in audio_containers):
                for root in roots:
                    res = media.convert_audio(root, game / "media/audio-converted" / root.name, f"*{ext}", subsongs)
                    counts[f"audio {ext} -> ogg"] = counts.get(f"audio {ext} -> ogg", 0) + res["inputs"]
    carved = 0
    for blob in big_unknown:
        n = media.carve_ogg(blob, game / "media/audio-carved" / blob.stem)
        carved += n
        if not n:
            shutil.rmtree(game / "media/audio-carved" / blob.stem, ignore_errors=True)
    if carved:
        counts["ogg streams carved from archives"] = carved
    counts["ogg-carve candidates (8–512 MB, non-archive)"] = len(big_unknown)
    log(f"generic: {counts}")
    return counts


def run(game: Path, only: Optional[str] = None, log: Log = print) -> dict:
    """Detect and extract. ``only`` = 'data' | 'media' to run half the work."""
    game = game.resolve()
    report: dict = {"game": game.name}
    if not (game / "unpacked").exists():
        log("unpack")
        unity.unpack(game)
    engines = detect(game)
    report["engines"] = engines
    log(f"engines: {', '.join(engines)}")
    if "unity" in engines:
        if only != "media":
            if unity.il2cpp_dump(game):
                log("il2cpp: class layouts rebuilt")
            log("unity: data")
            report["unity_data"] = unity.dump_data(game)
            report["classes_csv"] = unity.classes_to_csv(game)
            if kairo.is_kairosoft(game):
                report["kairosoft"] = kairo.unpack_dir(game / "extract/text", game / "unpacked-kairo")
        if only != "data":
            log("unity: media")
            report["unity_media"] = unity.export_media(game)
    if "xnb" in engines:
        content = game / "unpacked/assets/Content"
        log("xnb: tables, textures")
        report["xnb"] = xnb.export(content, game / "media")
        banks = list(content.rglob("*.xwb"))
        if banks and only != "data":
            report["xnb_audio"] = media.convert_audio(content, game / "media/audio", "*.xwb", subsongs=True)
    if not {"unity", "xnb"} & set(engines):
        log("generic: unpacking every package fully")
        unpack_all(game)
        report["generic"] = generic(game, log)
    elif "html5" in engines:
        report["generic"] = generic(game, log)
    _write_report(game, report)
    return report


def _write_report(game: Path, report: dict) -> None:
    src = json.loads((game / "SOURCE.json").read_text(encoding="utf-8")) if (game / "SOURCE.json").exists() else {}
    lines = [f"# {game.name} — extraction report", "",
             f"Package: `{src.get('package', '?')}` {src.get('version', '')} · engines: {', '.join(report.get('engines', []))}", "",
             "| Step | Result |", "|---|---|"]
    for k, v in report.items():
        if k in ("game", "engines"):
            continue
        lines.append(f"| {k} | {json.dumps(v, ensure_ascii=False)[:600]} |")
    lines += ["", "Folders: `extract/` data · `media/` images, audio, video, fonts, models · `reports/classes/` "
              "readable tables (Unity) · `unpacked/` raw files. Browse with `navig explore " + str(game) + "`."]
    (game / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
