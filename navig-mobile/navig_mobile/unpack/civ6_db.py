"""Civilization VI: rebuild the gameplay + text database from extracted game data (any platform/edition).

Replays the game's own load process: gameplay schema -> Base data -> every DLC/expansion
UpdateDatabase/UpdateText action whose criteria match the Gathering Storm ruleset, ordered by
LoadOrder then file Priority. Entry point: build(data_dir, out_db).
"""
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

DATA = Path(".")
OUT = Path("civ6_gameplay.sqlite")
GAME_LABEL = "Civilization VI"

# Target game state: Gathering Storm with every leader available, no scenario active.
GAMECORE = "Expansion2"
RULESET = "RULESET_EXPANSION_2"

errors = []


def read_xml(path):
    raw = path.read_bytes()
    # A few Firaxis files carry a BOM or stray bytes before the declaration.
    raw = raw[raw.find(b"<"):]
    return ET.fromstring(raw)


def make_hash(s):
    """Engine type hash (FNV-1a 32-bit, signed) used by the Kinds/Types triggers."""
    h = 0x811C9DC5
    for b in (s or "").encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def coerce(v):
    if v is None:
        return None
    lv = v.strip().lower()
    if lv == "":  # Firaxis data uses "" for "no reference" (e.g. StrategicResource="")
        return None
    if lv == "true":
        return 1
    if lv == "false":
        return 0
    return v


def row_values(el):
    vals = {k: coerce(v) for k, v in el.attrib.items()}
    for child in el:
        if isinstance(child.tag, str):
            vals[child.tag] = coerce(child.text if child.text is not None else "")
    return vals


def where_clause(vals):
    if not vals:
        return "1=1", []
    return " AND ".join(f'"{k}" IS ?' for k in vals), list(vals.values())


def apply_xml(db, path, text_lang=None):
    """Apply a GameInfo/GameData XML file. text_lang marks a localization file."""
    try:
        root = read_xml(path)
    except ET.ParseError as e:
        errors.append(f"PARSE {path.relative_to(DATA)}: {e}")
        return
    for table in root:
        if not isinstance(table.tag, str):
            continue
        tname = table.tag
        if text_lang is not None:
            tname = "LocalizedText"
        for op in table:
            if not isinstance(op.tag, str):
                continue
            try:
                if op.tag in ("Row", "Replace"):
                    vals = row_values(op)
                    if text_lang is not None:
                        vals = {"Language": vals.get("Language", text_lang), "Tag": vals.get("Tag"),
                                "Text": vals.get("Text"), "Gender": vals.get("Gender"),
                                "Plurality": vals.get("Plurality"), "Source": str(path.relative_to(DATA))}
                    if not vals:
                        continue
                    cols = ",".join(f'"{k}"' for k in vals)
                    verb = "INSERT OR REPLACE" if (op.tag == "Replace" or text_lang is not None) else "INSERT"
                    db.execute(f'{verb} INTO "{tname}" ({cols}) VALUES ({",".join("?" * len(vals))})',
                               list(vals.values()))
                elif op.tag == "Update":
                    w = op.find("Where")
                    s = op.find("Set")
                    wv = row_values(w) if w is not None else {}
                    sv = row_values(s) if s is not None else {}
                    if text_lang is not None and "Language" not in wv:
                        wv["Language"] = text_lang
                    if not sv:
                        continue
                    wc, wp = where_clause(wv)
                    sets = ",".join(f'"{k}"=?' for k in sv)
                    db.execute(f'UPDATE "{tname}" SET {sets} WHERE {wc}', list(sv.values()) + wp)
                elif op.tag == "Delete":
                    wc, wp = where_clause(row_values(op))
                    db.execute(f'DELETE FROM "{tname}" WHERE {wc}', wp)
            except sqlite3.Error as e:
                errors.append(f"SQL {path.relative_to(DATA)} <{tname}/{op.tag}>: {e}")


def apply_sql(db, path):
    # Statement by statement: executescript() would COMMIT the surrounding transaction.
    buf = ""
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True):
        buf += line
        if not sqlite3.complete_statement(buf):
            continue
        stmt, buf = buf.strip(), ""
        if re.fullmatch(r"(?is)(BEGIN|COMMIT|END)(\s+TRANSACTION)?\s*;", stmt):
            continue
        try:
            db.execute(stmt)
        except sqlite3.Error as e:
            errors.append(f"SQLFILE {path.relative_to(DATA)}: {e}")


def apply_file(db, path, is_text):
    if not path.exists():
        errors.append(f"MISSING {path.relative_to(DATA)}")
        return
    if path.suffix.lower() == ".sql":
        if not is_text:
            apply_sql(db, path)
    elif is_text:
        m = re.search(r"([a-z]{2}_[A-Za-z]{2,4}(?:_[A-Z]{2})?)", path.stem)
        lang = "en_US" if "en_US" in path.parts else (m.group(1) if m else "en_US")
        apply_xml(db, path, text_lang=lang)
    else:
        apply_xml(db, path)


# ---- criteria evaluation -------------------------------------------------------------------
def criterion_true(el):
    tag, text = el.tag, (el.text or "").strip()
    if tag == "GameCoreInUse":
        ok = GAMECORE in [t.strip() for t in text.split(",")]
    elif tag == "RuleSetInUse":
        ok = RULESET in [t.strip() for t in text.split(",")]
    elif tag == "LeaderPlayable":
        ok = "Expansion2_Players" in text
    elif tag == "CivilizationPlayable":
        ok = "Expansion2_Players" in text
    else:  # ConfigurationValueMatches, MapInUse, ... -> scenario/map specific, off by default
        ok = False
    if el.get("inverse") == "1":
        ok = not ok
    return ok


def criteria_map(mod):
    out = {}
    ac = mod.find("ActionCriteria")
    if ac is None:
        return out
    for c in ac.findall("Criteria"):
        results = [criterion_true(ch) for ch in c if isinstance(ch.tag, str)]
        out[c.get("id")] = (any(results) if c.get("any") == "1" else all(results)) if results else True
    return out


def collect_actions():
    actions = []  # (load_order, seq, mod_dir, kind, [files])
    seq = 0
    for modinfo in sorted(DATA.glob("DLC/*/*.modinfo")):
        if "Scenario" in modinfo.stem:
            continue
        mod = read_xml(modinfo)
        crit = criteria_map(mod)
        igs = mod.find("InGameActions")
        if igs is None:
            continue
        for act in igs:
            if act.tag not in ("UpdateDatabase", "UpdateText"):
                continue
            cid = act.get("criteria")
            if cid and not crit.get(cid, False):
                continue
            lo = act.find("Properties/LoadOrder")
            load_order = int(lo.text) if lo is not None and lo.text else 0
            files = [(int(f.get("Priority", 0)), i, f.text.strip()) for i, f in enumerate(act.findall("File"))]
            files.sort(key=lambda t: (-t[0], t[1]))
            actions.append((load_order, seq, modinfo.parent, act.tag, [f for _, _, f in files], act.get("id")))
            seq += 1
    actions.sort(key=lambda a: (a[0], a[1]))
    return actions


def build(data_dir: Path, out_db: Path, label: str = "Civilization VI") -> dict:
    """Build ``out_db`` from ``data_dir`` (the folder holding Base/ and DLC/). Returns a summary."""
    global DATA, OUT, GAME_LABEL, errors
    DATA, OUT, GAME_LABEL, errors = Path(data_dir), Path(out_db), label, []
    if OUT.exists():
        OUT.unlink()
    db = sqlite3.connect(OUT, timeout=30)  # busy timeout: navig convention for every sqlite store
    db.create_function("Make_Hash", 1, make_hash, deterministic=True)
    db.execute("PRAGMA foreign_keys=OFF")
    schema = DATA / "Base/Assets/Gameplay/Data/Schema"
    apply_sql(db, schema / "01_GameplaySchema.sql")
    apply_sql(db, schema / "02_AddTriggers.sql")
    db.execute("""CREATE TABLE IF NOT EXISTS LocalizedText(Language TEXT NOT NULL, Tag TEXT NOT NULL,
                  Text TEXT, Gender TEXT, Plurality TEXT, Source TEXT, PRIMARY KEY(Language, Tag))""")

    # Base gameplay: Kinds/Types first so later tables reference existing rows.
    base = DATA / "Base/Assets/Gameplay/Data"
    for p in sorted(base.glob("*.xml")):
        apply_xml(db, p)
    # Base text: English folder + per-language Vanilla_* files.
    text = DATA / "Base/Assets/Text"
    for p in sorted((text / "en_US").glob("*.xml")) + sorted(text.glob("*.xml")):
        apply_file(db, p, is_text=True)
    db.commit()

    # DLC + expansions in one transaction: foreign keys on so Delete cascades like in-game,
    # deferred so a row may reference one a later file inserts (the engine checks at the end).
    db.isolation_level = None
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN")
    db.execute("PRAGMA defer_foreign_keys=ON")
    loaded = []
    for load_order, _, moddir, kind, files, aid in collect_actions():
        for f in files:
            apply_file(db, moddir / f, is_text=(kind == "UpdateText"))
        loaded.append(f"{moddir.name}:{aid} ({kind}, LoadOrder {load_order}, {len(files)} files)")
    # EFFECT_*/COLLECTION_*/REQUIREMENT_* types are registered by the engine, not by XML.
    native = [("DynamicModifiers", "CollectionType", "KIND_COLLECTION"),
              ("DynamicModifiers", "EffectType", "KIND_EFFECT"),
              ("Requirements", "RequirementType", "KIND_REQUIREMENT")]
    for table, col, kind in native:
        db.execute(f'''INSERT OR IGNORE INTO Types(Type, Kind) SELECT DISTINCT "{col}", ? FROM "{table}"
                       WHERE "{col}" IS NOT NULL AND "{col}" NOT IN (SELECT Type FROM Types)''', (kind,))
    # Same for kinds the engine declares itself (KIND_TRAIT is never declared in XML).
    db.execute("INSERT OR IGNORE INTO Kinds(Kind) SELECT DISTINCT Kind FROM Types WHERE Kind NOT IN (SELECT Kind FROM Kinds)")
    # Rows still dangling at the end would make the game reject the load; drop and log them.
    for _ in range(10):
        bad = db.execute("PRAGMA foreign_key_check").fetchall()
        if not bad:
            break
        for table, rowid, parent, fkid in bad:
            if rowid is None:
                continue
            cols = [r[3] for r in db.execute(f'PRAGMA foreign_key_list("{table}")') if r[0] == fkid]
            vals = db.execute(f'SELECT {",".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}" WHERE rowid=?',
                              (rowid,)).fetchone()
            errors.append(f"FK-DROP {table}.{'/'.join(cols)}={vals} -> {parent}")
            db.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,))
    db.execute("COMMIT")
    db.isolation_level = ""

    db.execute("CREATE TABLE _BuildInfo(Key TEXT, Value TEXT)")
    db.executemany("INSERT INTO _BuildInfo VALUES (?,?)",
                   [("game", GAME_LABEL),
                    ("ruleset", RULESET)] + [("action", a) for a in loaded] + [("error", e) for e in errors])
    db.commit()
    db.execute("VACUUM")
    db.close()
    return {"actions": len(loaded), "errors": len(errors)}
