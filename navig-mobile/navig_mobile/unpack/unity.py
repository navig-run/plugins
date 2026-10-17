"""Unity Android games: unpack, IL2CPP class layouts, data dump, media export, per-class CSV.

Game folder layout (shared by every extractor):
  apk/*.apk  obb/*.obb  android-data/   — inputs (as pulled from the device)
  unpacked/                             — raw files from the packages
  il2cpp-dump/                          — Il2CppDumper output (DummyDll, dump.cs, stringliteral.json)
  extract/{text,mono}/ + SUMMARY.txt    — text assets and decoded MonoBehaviours
  media/{textures,sprites,audio,video,fonts,meshes,loose}/ + MEDIA_SUMMARY.txt
  reports/classes/<Class>.csv           — readable fields of every game script class
"""

from __future__ import annotations

import collections
import csv
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Optional

from navig_mobile.unpack import tools

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
UI_CLASSES = {"Image", "Text", "Button", "Shadow", "Outline", "RectMask2D", "Mask", "ContentSizeFitter", "LayoutElement",
              "HorizontalLayoutGroup", "VerticalLayoutGroup", "GridLayoutGroup", "ScrollRect", "GraphicRaycaster",
              "CanvasScaler", "TextMeshProUGUI", "TextMeshPro", "TMP_SubMeshUI", "EventSystem", "StandaloneInputModule",
              "Toggle", "Slider", "Scrollbar", "InputField", "Dropdown", "RawImage", "AspectRatioFitter", "EventTrigger",
              "ToggleGroup", "unknown"}


def safe(name: str) -> str:
    """File-system safe name (also dodges Windows reserved device names and trailing dots)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "unnamed").strip().rstrip(". ")[:120] or "unnamed"
    return f"_{s}" if s.split(".")[0].upper() in RESERVED else s


def is_unity(game: Path) -> bool:
    if (game / "unpacked").exists() and any((game / "unpacked/assets/bin/Data").glob("*")):
        return True
    data = game / "data"  # PC install layout: <Game>_Data folders
    return data.exists() and any(p.is_dir() and p.name.endswith("_Data") for p in data.iterdir())


# ── unpack ──────────────────────────────────────────────────────────────────

def unpack(game: Path) -> Path:
    """Extract ``assets/`` from APKs then OBBs (OBB wins on name clashes) into ``unpacked/``.

    7-Zip instead of zipfile: some games ship entries with deliberately wrong CRCs.
    """
    seven = tools.require("7z")
    out = game / "unpacked"
    out.mkdir(parents=True, exist_ok=True)
    for pkg in sorted((game / "apk").glob("*.apk")) + sorted((game / "obb").glob("*.obb")):
        mode = "-aoa" if pkg.suffix == ".obb" else "-aos"
        subprocess.run([seven, "x", "-y", mode, f"-o{out}", str(pkg), "assets/*", "-r"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def _sources(game: Path) -> list[str]:
    srcs = [str(game / "unpacked")]
    if (game / "android-data").exists():
        srcs.append(str(game / "android-data"))
    if (game / "data").exists():  # PC install: only <Game>_Data holds asset bundles
        srcs += [str(p) for p in sorted((game / "data").glob("*_Data")) if p.is_dir()]
    return srcs


def _load_env(game: Path):
    """UnityPy environment over the game's files.

    Some builds strip the Unity version from their asset bundles, and UnityPy then refuses to parse
    them; the player's own globalgamemanagers / data.unity3d always carries it, so use that as fallback.
    """
    import UnityPy
    import UnityPy.config

    if not isinstance(getattr(UnityPy.config, "FALLBACK_UNITY_VERSION", None), str):
        candidates = [game / "unpacked/assets/bin/Data"]
        if (game / "data").exists():
            candidates += sorted(p for p in (game / "data").glob("*_Data") if p.is_dir())
        for data in candidates:
            for name in ("globalgamemanagers", "data.unity3d", "mainData", "unity default resources"):
                f = data / name
                if f.exists():
                    try:
                        UnityPy.config.FALLBACK_UNITY_VERSION = (
                            UnityPy.load(str(f)).objects[0].assets_file.unity_version)
                        break
                    except Exception:
                        continue
            else:
                continue
            break
    return UnityPy.load(*_sources(game))


# ── IL2CPP ──────────────────────────────────────────────────────────────────

def il2cpp_dump(game: Path) -> Optional[Path]:
    """Run Il2CppDumper when the build is IL2CPP. Returns the DummyDll folder or None."""
    meta = next((game / "unpacked").rglob("global-metadata.dat"), None)
    if meta is None:
        return None
    out = game / "il2cpp-dump"
    if (out / "DummyDll").is_dir():
        return out / "DummyDll"
    lib = None
    for arch in ("arm64-v8a", "armeabi-v7a"):
        for apk in sorted((game / "apk").glob("*.apk")):
            with zipfile.ZipFile(apk) as z:
                if f"lib/{arch}/libil2cpp.so" in z.namelist():
                    out.mkdir(parents=True, exist_ok=True)
                    lib = out / "libil2cpp.so"
                    lib.write_bytes(z.read(f"lib/{arch}/libil2cpp.so"))
                    break
        if lib:
            break
    if lib is None:
        return None
    dumper = tools.require("il2cppdumper")
    with open(game / "il2cpp-dump.log", "w", encoding="utf-8") as log:
        subprocess.run([dumper, str(lib), str(meta), str(out)], stdout=log, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL)
    return out / "DummyDll" if (out / "DummyDll").is_dir() else None


def _generator(env, game: Path):
    """TypeTreeGenerator fed with the DummyDll (IL2CPP builds strip MonoBehaviour type trees)."""
    dummy = game / "il2cpp-dump" / "DummyDll"
    if not dummy.is_dir():
        return None, "no IL2CPP dump (Mono build, or run il2cpp first)"
    try:
        import TypeTreeGeneratorAPI
        api_dir = os.path.dirname(TypeTreeGeneratorAPI.__file__)
        os.environ["PATH"] = api_dir + os.pathsep + os.environ["PATH"]
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(api_dir)  # its native DLL loads capstone.dll by name
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
        version = env.objects[0].assets_file.unity_version
        gen = TypeTreeGenerator(version)
        gen.load_local_dll_folder(str(dummy))
        return gen, f"type trees from Il2CppDumper DummyDll (Unity {version})"
    except Exception as exc:  # optional capability: degrade to stored type trees only
        return None, f"type tree generation unavailable: {exc}"


# ── data ────────────────────────────────────────────────────────────────────

def _ext_for(text: str) -> str:
    t = text.lstrip()
    if t[:1] in "{[":
        try:
            json.loads(t)
            return ".json"
        except ValueError:
            pass
    if t.startswith("<"):
        return ".xml"
    first = t.split("\n", 1)[0]
    return ".csv" if "\t" in first or first.count(",") >= 2 else ".txt"


def dump_data(game: Path) -> dict:
    """Text assets → extract/text, MonoBehaviours → extract/mono/<Class>/*.json."""
    out = game / "extract"
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "mono").mkdir(parents=True, exist_ok=True)
    env = _load_env(game)
    gen, note = _generator(env, game)
    env.typetree_generator = gen
    container = {}
    for path, ref in env.container.items():
        try:
            container[(ref.assets_file.name, ref.path_id)] = path
        except AttributeError:
            pass
    counts, scripts, seen = collections.Counter(), collections.Counter(), collections.Counter()
    ok = fail = 0
    for obj in env.objects:
        tname = obj.type.name
        counts[tname] += 1
        if tname == "TextAsset":
            data = obj.read()
            raw = data.m_Script
            raw = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else bytes(raw)
            cpath = container.get((obj.assets_file.name, obj.path_id))
            rel = Path(*[safe(p) for p in Path(cpath).with_suffix("").parts]) if cpath else Path(safe(data.m_Name))
            seen[str(rel)] += 1
            if seen[str(rel)] > 1:
                rel = rel.with_name(f"{rel.name}__{seen[str(rel)]}")
            target = out / "text" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                text = raw.decode("utf-8-sig")
                target.with_name(target.name + _ext_for(text)).write_text(text, encoding="utf-8")
            except UnicodeDecodeError:
                target.with_name(target.name + ".bin").write_bytes(raw)
        elif tname == "MonoBehaviour":
            # Stored type tree first; the generator only as fallback (it can fail where stored trees work).
            env.typetree_generator = None
            try:
                tree = obj.read_typetree()
            except Exception:
                tree = None
            env.typetree_generator = gen
            if gen is not None and (tree is None or len(tree) <= 4):
                try:
                    tree = obj.read_typetree()
                except Exception:
                    pass
            if tree is None:
                fail += 1
                continue
            ok += 1
            script = "unknown"
            try:
                script = obj.parse_monobehaviour_head().m_Script.deref_parse_as_object().m_ClassName
            except Exception:
                pass
            scripts[script] += 1
            name = safe(tree.get("m_Name") or script)
            d = out / "mono" / safe(script)
            d.mkdir(exist_ok=True)
            (d / f"{name}_{obj.path_id}.json").write_text(
                json.dumps(tree, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    lines = [f"game: {game.name}", "object types:"] + [f"  {n:7d} {t}" for t, n in counts.most_common()]
    lines += [note, f"MonoBehaviours decoded: {ok}, not decodable (no type tree): {fail}", "decoded by script:"]
    lines += [f"  {n:7d} {s}" for s, n in scripts.most_common()]
    (out / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"text_assets": counts["TextAsset"], "mono_decoded": ok, "mono_failed": fail, "classes": len(scripts)}


# ── media ───────────────────────────────────────────────────────────────────

LOOSE_EXT = {".mp4", ".webm", ".ogv", ".mov", ".m4v", ".ogg", ".mp3", ".wav", ".m4a", ".png", ".jpg", ".jpeg",
             ".ttf", ".otf", ".json", ".txt", ".csv", ".xml", ".bytes", ".bank", ".bnk", ".wem"}


def export_media(game: Path) -> dict:
    """Textures/sprites → PNG, audio → WAV/OGG (FMOD), video, fonts, meshes → OBJ, loose media files."""
    out = game / "media"
    used: dict[str, set] = collections.defaultdict(set)

    def target(kind, name, ext):
        d = out / kind
        d.mkdir(parents=True, exist_ok=True)
        base = stem = safe(name)
        n = 1
        while stem.lower() in used[kind]:
            n += 1
            stem = f"{base}__{n}"
        used[kind].add(stem.lower())
        return d / f"{stem}{ext}"

    counts, errors = collections.Counter(), collections.Counter()
    env = _load_env(game)
    for obj in env.objects:
        t = obj.type.name
        if t not in ("Texture2D", "Sprite", "AudioClip", "VideoClip", "Font", "Mesh", "MovieTexture"):
            continue
        try:
            data = obj.read()
            name = getattr(data, "m_Name", "") or f"{t}_{obj.path_id}"
            if t in ("Texture2D", "Sprite"):
                img = data.image
                if img is None or img.width == 0:
                    continue
                img.save(target("textures" if t == "Texture2D" else "sprites", name, ".png"))
            elif t == "AudioClip":
                samples = data.samples
                if not samples:
                    errors["AudioClip: no samples (streamed data missing or FMOD unavailable)"] += 1
                    continue
                for sname, sbytes in samples.items():
                    target("audio", Path(sname).stem if len(samples) > 1 else name, Path(sname).suffix or ".wav").write_bytes(sbytes)
            elif t == "VideoClip":
                raw = getattr(data, "m_VideoData", None)
                if not raw:
                    errors["VideoClip: data not found"] += 1
                    continue
                ext = Path(getattr(data, "m_OriginalPath", "") or "x.mp4").suffix or ".mp4"
                target("video", name, ext).write_bytes(bytes(raw))
            elif t == "MovieTexture":
                target("video", name, ".ogv").write_bytes(bytes(data.m_MovieData))
            elif t == "Font":
                raw = bytes(data.m_FontData or b"")
                if raw:
                    target("fonts", name, ".otf" if raw[:4] == b"OTTO" else ".ttf").write_bytes(raw)
            elif t == "Mesh":
                text = data.export()
                if text:
                    target("meshes", name, ".obj").write_text(text, encoding="utf-8")
            counts[t] += 1
        except (PermissionError, FileNotFoundError):
            errors[f"{t}: no stored data (built at runtime, e.g. dynamic font atlas)"] += 1
        except Exception as exc:  # one bad asset must not stop the export
            errors[f"{t}: {type(exc).__name__}: {str(exc)[:80]}"] += 1
    for root in [game / "unpacked/assets", game / "android-data"]:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in LOOSE_EXT and "bin/Data/Managed" not in f.as_posix():
                dest = out / "loose" / root.name / f.relative_to(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                counts["loose file"] += 1
    lines = [f"game: {game.name}", "exported:"] + [f"  {n:7d} {k}" for k, n in counts.most_common()]
    lines += ["skipped/errors:"] + [f"  {n:7d} {k}" for k, n in errors.most_common(20)]
    out.mkdir(exist_ok=True)
    (out / "MEDIA_SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(counts)


# ── readable class tables ───────────────────────────────────────────────────

def _is_ref(v) -> bool:
    return isinstance(v, dict) and set(v) <= {"m_FileID", "m_PathID"}


def _flatten(obj: dict, prefix: str = "", depth: int = 0) -> dict:
    row = {}
    for k, v in obj.items():
        if k in ("m_GameObject", "m_Script", "m_Enabled", "m_EditorHideFlags", "m_EditorClassIdentifier") or _is_ref(v):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            if depth < 2:
                row.update(_flatten(v, key + ".", depth + 1))
        elif isinstance(v, list):
            if not v:
                continue
            if all(not isinstance(x, (dict, list)) for x in v):
                row[key] = ";".join(str(x) for x in v)[:2000]
            elif not all(_is_ref(x) for x in v):
                row[key] = json.dumps([x for x in v if not _is_ref(x)], ensure_ascii=False, default=str)[:2000]
        else:
            row[key] = v
    return row


def classes_to_csv(game: Path) -> int:
    """One CSV per non-UI script class under reports/classes/, plus an index README."""
    mono, out = game / "extract/mono", game / "reports/classes"
    if not mono.exists():
        return 0
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for d in sorted(p for p in mono.iterdir() if p.is_dir() and p.name not in UI_CLASSES):
        rows = []
        for f in sorted(d.glob("*.json")):
            try:
                r = _flatten(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
            if len(r) > 1:
                rows.append({"_object": f.stem, **r})
        if not rows:
            continue
        cols = list(dict.fromkeys(k for r in rows for k in r))
        with open(out / f"{d.name}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        index.append((d.name, len(rows), len(cols)))
    index.sort(key=lambda x: -x[1] * x[2])
    lines = ["# Decoded script classes", "", "One CSV per game script class (UI/layout classes skipped).", "",
             "| Class | Objects | Fields |", "|---|---|---|"] + [f"| [{c}]({c}.csv) | {n} | {k} |" for c, n, k in index]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(index)
