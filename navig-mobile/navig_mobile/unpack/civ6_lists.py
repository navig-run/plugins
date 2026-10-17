"""Civilization VI: readable lists from the database built by civ6_db.build().

Writes into reports/:
  README.md                 overview with counts and links
  lists/<category>.md|.csv  curated lists with English names/descriptions resolved
  text/                     every localized string (English, and all languages side by side)
  tables/<Table>.csv        raw dump of every gameplay table
"""

import csv
import re
import sqlite3
from pathlib import Path

def export(db_path, out_dir, game_title: str = "Civilization VI") -> dict:
    """Write README.md, lists/, text/ and tables/ for a database made by civ6_db.build()."""
    DB = sqlite3.connect(db_path, timeout=30)  # busy timeout: navig convention for every sqlite store
    DB.row_factory = sqlite3.Row
    OUT = Path(out_dir)
    LISTS, TEXT, TABLES = OUT / "lists", OUT / "text", OUT / "tables"
    for d in (LISTS, TEXT, TABLES):
        d.mkdir(parents=True, exist_ok=True)

    EN = {r["Tag"]: r["Text"] for r in DB.execute("SELECT Tag, Text FROM LocalizedText WHERE Language='en_US'")}


    def icon_word(m):
        """[ICON_Food] -> 'Food ' unless the following words already say it ('[ICON_Food] Food')."""
        word = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", m.group(1).replace("_", " ")).strip()
        word = " ".join(w.capitalize() if w.isupper() else w for w in word.split())
        last = word.split()[-1].lower()
        following = m.string[m.end():m.end() + 25].lower()
        return "" if last in following else word + " "


    def loc(tag, depth=0):
        """English text for a LOC_ tag, with nested {LOC_...} resolved and markup flattened."""
        if not tag:
            return ""
        s = EN.get(tag, tag)
        if s is None:
            return ""
        if depth < 4:
            s = re.sub(r"\{(LOC_[A-Z0-9_]+)\}", lambda m: loc(m.group(1), depth + 1), s)
        s = re.sub(r"\[NEWLINE\]", " / ", s)
        s = re.sub(r"\[ICON_([A-Za-z0-9_]+)\]\s*", icon_word, s)
        s = re.sub(r"\[/?COLOR[^\]]*\]|\[ENDCOLOR\]", "", s)
        s = s.split("|")[0] if "|" in s and not s.startswith("|") else s
        return re.sub(r"\s+", " ", s).strip()


    def name_of(type_id, table, key, col="Name"):
        if not type_id:
            return ""
        r = DB.execute(f'SELECT "{col}" FROM "{table}" WHERE "{key}"=?', (type_id,)).fetchone()
        return loc(r[0]) if r else type_id


    def has_table(t):
        return DB.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


    # Who owns a unique item: TraitType -> civilization or leader name.
    OWNER = {}
    for r in DB.execute("SELECT ct.TraitType, c.Name FROM CivilizationTraits ct JOIN Civilizations c USING(CivilizationType)"):
        OWNER[r[0]] = loc(r[1])
    for r in DB.execute("SELECT lt.TraitType, l.Name FROM LeaderTraits lt JOIN Leaders l USING(LeaderType)"):
        OWNER.setdefault(r[0], loc(r[1]))

    ERA = {r[0]: loc(r[1]) for r in DB.execute("SELECT EraType, Name FROM Eras")}
    TECH_ERA = {r[0]: r[1] for r in DB.execute("SELECT TechnologyType, EraType FROM Technologies")}
    CIVIC_ERA = {r[0]: r[1] for r in DB.execute("SELECT CivicType, EraType FROM Civics")}


    def era_for(tech, civic):
        if not tech and not civic:
            return "From start"
        return ERA.get(TECH_ERA.get(tech) or CIVIC_ERA.get(civic), "")


    def unlock(tech, civic):
        if tech:
            return name_of(tech, "Technologies", "TechnologyType")
        if civic:
            return name_of(civic, "Civics", "CivicType")
        return ""


    def yields(table, key, val):
        rows = DB.execute(f'SELECT YieldType, YieldChange FROM "{table}" WHERE "{key}"=?', (val,)).fetchall()
        return ", ".join(f"{r[1]:+g} {r[0].replace('YIELD_', '').title()}" for r in rows)


    INDEX = []  # (title, file stem, count, blurb)


    def write_list(stem, title, blurb, headers, rows):
        rows = [["" if v is None else v for v in r] for r in rows]
        with open(LISTS / f"{stem}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        md = [f"# {title}", "", blurb, "", f"{len(rows)} entries. CSV: [{stem}.csv]({stem}.csv)", "",
              "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        for r in rows:
            md.append("| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in r) + " |")
        (LISTS / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        INDEX.append((title, stem, len(rows), blurb))


    # ---- Civilizations & leaders -----------------------------------------------------------------
    ITEM_TRAITS = {r[0] for t in ("Units", "Buildings", "Districts", "Improvements")
                   for r in DB.execute(f'SELECT TraitType FROM "{t}" WHERE TraitType IS NOT NULL')}


    def traits_text(rows):
        # Unique-item traits are listed in the Uniques column; keep only real abilities.
        return " // ".join(f"{loc(t['Name'])}: {loc(t['Description'])}" for t in rows
                           if t["Name"] and t["TraitType"] not in ITEM_TRAITS)


    def uniques_for(trait_types):
        out = []
        for t in trait_types:
            for table, key in (("Units", "UnitType"), ("Buildings", "BuildingType"), ("Districts", "DistrictType"),
                               ("Improvements", "ImprovementType")):
                for r in DB.execute(f'SELECT Name FROM "{table}" WHERE TraitType=?', (t,)):
                    out.append(f"{loc(r[0])} ({table[:-1].replace('Buildin', 'Building').lower()})")
        return ", ".join(out)


    rows = []
    for c in DB.execute("""SELECT * FROM Civilizations WHERE StartingCivilizationLevelType='CIVILIZATION_LEVEL_FULL_CIV'
                           ORDER BY Name"""):
        ct = DB.execute("""SELECT t.* FROM CivilizationTraits ct JOIN Traits t USING(TraitType)
                           WHERE ct.CivilizationType=?""", (c["CivilizationType"],)).fetchall()
        leaders = DB.execute("""SELECT l.LeaderType, l.Name FROM CivilizationLeaders cl JOIN Leaders l USING(LeaderType)
                                WHERE cl.CivilizationType=?""", (c["CivilizationType"],)).fetchall()
        for ld in leaders:
            lt = DB.execute("""SELECT t.* FROM LeaderTraits lt JOIN Traits t USING(TraitType)
                               WHERE lt.LeaderType=?""", (ld["LeaderType"],)).fetchall()
            agenda = DB.execute("""SELECT a.Name, a.Description FROM HistoricalAgendas h JOIN Agendas a USING(AgendaType)
                                   WHERE h.LeaderType=?""", (ld["LeaderType"],)).fetchone()
            rows.append([loc(c["Name"]), loc(ld["Name"]), traits_text(ct), traits_text(lt),
                         uniques_for([t["TraitType"] for t in ct] + [t["TraitType"] for t in lt]),
                         f"{loc(agenda[0])}: {loc(agenda[1])}" if agenda else ""])
    write_list("civilizations", "Civilizations & Leaders", "Every playable civ/leader pair with abilities, unique items and AI agenda.",
               ["Civilization", "Leader", "Civilization ability", "Leader ability", "Unique units/buildings/districts/improvements", "Agenda"], rows)

    rows = []
    for c in DB.execute("SELECT * FROM Civilizations WHERE StartingCivilizationLevelType='CIVILIZATION_LEVEL_CITY_STATE' ORDER BY Name"):
        cat = DB.execute("SELECT Value FROM TypeProperties WHERE Type=? AND Name='CityStateCategory'", (c["CivilizationType"],)).fetchone()
        lt = DB.execute("""SELECT t.* FROM CivilizationLeaders cl JOIN LeaderTraits lt USING(LeaderType) JOIN Traits t USING(TraitType)
                           WHERE cl.CivilizationType=?""", (c["CivilizationType"],)).fetchall()
        rows.append([loc(c["Name"]), (cat[0] if cat else "").title(), " // ".join(loc(t["Description"]) for t in lt if t["Description"])])
    write_list("city_states", "City-States", "Every city-state, its type and its suzerain bonus.", ["City-state", "Type", "Suzerain bonus"], rows)

    # ---- Units ------------------------------------------------------------------------------------
    xp2 = has_table("Units_XP2")
    upg = {r[0]: r[1] for r in DB.execute("SELECT Unit, UpgradeUnit FROM UnitUpgrades")}
    rows = []
    for u in DB.execute("SELECT * FROM Units ORDER BY Domain, PromotionClass, Cost"):
        res = u["StrategicResource"]
        extra = DB.execute("SELECT * FROM Units_XP2 WHERE UnitType=?", (u["UnitType"],)).fetchone() if xp2 else None
        res_txt = name_of(res, "Resources", "ResourceType") if res else ""
        if extra is not None and res:
            res_txt += f" ({extra['ResourceCost']} to build, {extra['ResourceMaintenanceAmount']}/turn)"
        rows.append([loc(u["Name"]), u["Domain"].replace("DOMAIN_", "").title(),
                     name_of(u["PromotionClass"], "UnitPromotionClasses", "PromotionClassType") if u["PromotionClass"] else "Civilian/Support",
                     era_for(u["PrereqTech"], u["PrereqCivic"]), unlock(u["PrereqTech"], u["PrereqCivic"]),
                     u["Cost"], u["Maintenance"], u["Combat"] or "", u["RangedCombat"] or "", u["Bombard"] or "",
                     u["Range"] or "", u["AntiAirCombat"] or "", u["BaseMoves"], u["BaseSightRange"], res_txt,
                     name_of(upg.get(u["UnitType"]), "Units", "UnitType") if upg.get(u["UnitType"]) else "",
                     OWNER.get(u["TraitType"], "") if u["TraitType"] else "", loc(u["Description"])])
    write_list("units", "Units", "All units with combat stats. Combat = melee strength, Ranged/Bombard = ranged strength, Moves = movement points.",
               ["Unit", "Domain", "Class", "Era", "Unlocked by", "Cost", "Maintenance", "Combat", "Ranged", "Bombard", "Range",
                "Anti-air", "Moves", "Sight", "Strategic resource", "Upgrades to", "Unique to", "Description"], rows)

    rows = []
    for p in DB.execute("SELECT * FROM UnitPromotions ORDER BY PromotionClass, Level, Column"):
        rows.append([name_of(p["PromotionClass"], "UnitPromotionClasses", "PromotionClassType"), p["Level"], loc(p["Name"]), loc(p["Description"])])
    write_list("unit_promotions", "Unit Promotions", "Promotion tree per unit class (Level 1-5).", ["Class", "Level", "Promotion", "Effect"], rows)

    if has_table("UnitAbilities"):
        rows = [[loc(a["Name"]) or a["UnitAbilityType"], loc(a["Description"])] for a in DB.execute("SELECT * FROM UnitAbilities") if a["Name"] or a["Description"]]
        write_list("unit_abilities", "Unit Abilities", "Special abilities units carry (named ones only).", ["Ability", "Effect"], rows)

    # ---- Buildings, wonders, districts, improvements, projects ----------------------------------------
    for wonder, stem, title in ((0, "buildings", "Buildings"), (1, "wonders", "World Wonders")):
        rows = []
        for b in DB.execute("SELECT * FROM Buildings WHERE IsWonder=? AND IFNULL(InternalOnly,0)=0 ORDER BY Cost", (wonder,)):
            rows.append([loc(b["Name"]), name_of(b["PrereqDistrict"], "Districts", "DistrictType") if b["PrereqDistrict"] else "",
                         era_for(b["PrereqTech"], b["PrereqCivic"]), unlock(b["PrereqTech"], b["PrereqCivic"]), b["Cost"],
                         b["Maintenance"], yields("Building_YieldChanges", "BuildingType", b["BuildingType"]),
                         b["Housing"] or "", b["Entertainment"] or "", b["CitizenSlots"] or "",
                         OWNER.get(b["TraitType"], "") if b["TraitType"] else "", loc(b["Description"])])
        write_list(stem, title, f"All {title.lower()} with cost, base yields and effects.",
                   ["Name", "District", "Era", "Unlocked by", "Cost", "Maintenance", "Yields", "Housing", "Amenities", "Specialist slots", "Unique to", "Effect"], rows)

    rows = []
    for d in DB.execute("SELECT * FROM Districts WHERE IFNULL(InternalOnly,0)=0 ORDER BY Cost"):
        adj = DB.execute("""SELECT a.Description, a.YieldChange, a.TilesRequired FROM District_Adjacencies da
                            JOIN Adjacency_YieldChanges a ON a.ID=da.YieldChangeId WHERE da.DistrictType=?""", (d["DistrictType"],)).fetchall()

        def adj_text(a):
            n = a[2] or 1
            s = re.sub(r"\{1_num\}", f"{a[1]:g}", loc(a[0]), flags=re.I)
            s = re.sub(r"\{1_Num\s*:\s*plural \d+\?([^;]*);\s*other\?([^;]*);\s*\}",
                       lambda m: m.group(1) if n == 1 else m.group(2), s, flags=re.I)
            return s.rstrip(".") + (f" (per {n} adjacent)" if n > 1 else "")

        rows.append([loc(d["Name"]), unlock(d["PrereqTech"], d["PrereqCivic"]), d["Cost"], d["Housing"] or "", d["Entertainment"] or "",
                     "; ".join(dict.fromkeys(adj_text(a) for a in adj if a[0])), OWNER.get(d["TraitType"], "") if d["TraitType"] else "",
                     loc(d["Description"])])
    write_list("districts", "Districts", "All districts with adjacency bonuses.",
               ["District", "Unlocked by", "Base cost", "Housing", "Amenities", "Adjacency bonuses", "Unique to", "Description"], rows)

    rows = []
    for i in DB.execute("SELECT * FROM Improvements ORDER BY Name"):
        if not i["Name"]:
            continue
        rows.append([loc(i["Name"]), unlock(i["PrereqTech"], i["PrereqCivic"]), yields("Improvement_YieldChanges", "ImprovementType", i["ImprovementType"]),
                     i["Housing"] or "", i["Appeal"] or "", "Yes" if i["Buildable"] else "No",
                     OWNER.get(i["TraitType"], "") if i["TraitType"] else "", loc(i["Description"])])
    write_list("improvements", "Tile Improvements", "Builder improvements plus non-buildable ones (goody huts, barbarian camps...).",
               ["Improvement", "Unlocked by", "Base yields", "Housing", "Appeal", "Buildable", "Unique to", "Description"], rows)

    rows = []
    for p in DB.execute("SELECT * FROM Projects ORDER BY Cost"):
        rows.append([loc(p["Name"]), name_of(p["PrereqDistrict"], "Districts", "DistrictType") if p["PrereqDistrict"] else "",
                     unlock(p["PrereqTech"], p["PrereqCivic"]), p["Cost"], "Yes" if p["SpaceRace"] else "", loc(p["Description"])])
    write_list("projects", "City Projects", "Projects cities can run (incl. space race).", ["Project", "District", "Unlocked by", "Cost", "Space race", "Effect"], rows)

    # ---- Tech & civic trees ----------------------------------------------------------------------
    for table, key, stem, title, prereq in (("Technologies", "TechnologyType", "technologies", "Technologies", "TechnologyPrereqs"),
                                             ("Civics", "CivicType", "civics", "Civics", "CivicPrereqs")):
        pcol = "PrereqTech" if table == "Technologies" else "PrereqCivic"
        bcol = "TechnologyType" if table == "Technologies" else "CivicType"
        rows = []
        for t in DB.execute(f'SELECT t.* FROM "{table}" t JOIN Eras e USING(EraType) ORDER BY e.ChronologyIndex, t.Cost'):
            pre = [name_of(r[0], table, key) for r in DB.execute(f'SELECT "{pcol}" FROM "{prereq}" WHERE "{key}"=?', (t[key],))]
            boost = DB.execute(f'SELECT TriggerDescription, Boost FROM Boosts WHERE "{bcol}"=?', (t[key],)).fetchone()
            unlocks = []
            for tb, kc, nm in (("Units", "UnitType", "Name"), ("Buildings", "BuildingType", "Name"), ("Districts", "DistrictType", "Name"),
                               ("Improvements", "ImprovementType", "Name"), ("Policies", "PolicyType", "Name"), ("Governments", "GovernmentType", "Name"),
                               ("Projects", "ProjectType", "Name")):
                cols = [r[1] for r in DB.execute(f'PRAGMA table_info("{tb}")')]
                if pcol in cols:
                    unlocks += [loc(r[0]) for r in DB.execute(f'SELECT "{nm}" FROM "{tb}" WHERE "{pcol}"=?', (t[key],))]
            rows.append([ERA.get(t["EraType"], ""), loc(t["Name"]), t["Cost"], ", ".join(pre),
                         f"{loc(boost[0])} ({boost[1]}%)" if boost else "", ", ".join(unlocks)])
        write_list(stem, title, f"The full {title.lower()} tree in era order, with prerequisites, {'Eureka' if table == 'Technologies' else 'Inspiration'} boosts and unlocks.",
                   ["Era", "Name", "Cost", "Requires", "Boost (Eureka/Inspiration)", "Unlocks"], rows)

    # ---- Government, religion, great people, governors -----------------------------------------------
    rows = [[loc(g["Name"]), g["Tier"] or "", name_of(g["PrereqCivic"], "Civics", "CivicType"),
             " / ".join(f"{loc(r[0])}: {r[1]}" for r in DB.execute("""SELECT s.Name, gs.NumSlots FROM Government_SlotCounts gs
                 JOIN GovernmentSlots s ON s.GovernmentSlotType=gs.GovernmentSlotType WHERE gs.GovernmentType=?""", (g["GovernmentType"],))),
             loc(g["InherentBonusDesc"]), loc(g["AccumulatedBonusDesc"])]
            for g in DB.execute("SELECT * FROM Governments ORDER BY Tier, Name")]
    write_list("governments", "Governments", "Governments, their policy slots and bonuses.",
               ["Government", "Tier", "Unlocked by", "Policy slots", "Inherent bonus", "Legacy bonus"], rows)

    rows = [[loc(p["Name"]), name_of(p["GovernmentSlotType"], "GovernmentSlots", "GovernmentSlotType"),
             unlock(p["PrereqTech"], p["PrereqCivic"]), loc(p["Description"])]
            for p in DB.execute("SELECT * FROM Policies ORDER BY GovernmentSlotType, Name")]
    obsolete = {r[0]: r[1] for r in DB.execute("SELECT PolicyType, ObsoletePolicy FROM ObsoletePolicies")} if has_table("ObsoletePolicies") else {}
    for r, p in zip(rows, DB.execute("SELECT PolicyType FROM Policies ORDER BY GovernmentSlotType, Name")):
        r.append(name_of(obsolete.get(p[0]), "Policies", "PolicyType") if obsolete.get(p[0]) else "")
    write_list("policies", "Policy Cards", "All policy cards by slot type.", ["Policy", "Slot", "Unlocked by", "Effect", "Made obsolete by"], rows)

    rows = [[name_of(b["BeliefClassType"], "BeliefClasses", "BeliefClassType"), loc(b["Name"]), loc(b["Description"])]
            for b in DB.execute("SELECT * FROM Beliefs ORDER BY BeliefClassType, Name")]
    write_list("beliefs", "Pantheons & Religious Beliefs", "Pantheon, founder, follower, worship and enhancer beliefs.", ["Type", "Belief", "Effect"], rows)

    rows = [[loc(r["Name"]), "Yes" if r["RequiresCustomName"] else ""]
            for r in DB.execute("SELECT * FROM Religions WHERE IFNULL(Pantheon,0)=0 ORDER BY Name")]
    write_list("religions", "Religions", "Religions that can be founded.", ["Religion", "Custom-named"], rows)

    rows = []
    for g in DB.execute("""SELECT g.*, c.Name AS ClassName FROM GreatPersonIndividuals g JOIN GreatPersonClasses c USING(GreatPersonClassType)
                           LEFT JOIN Eras e ON e.EraType=g.EraType ORDER BY c.Name, e.ChronologyIndex, g.Name"""):
        eff = DB.execute("""SELECT m.ModifierId FROM GreatPersonIndividualActionModifiers m WHERE m.GreatPersonIndividualType=?""",
                         (g["GreatPersonIndividualType"],)).fetchall()
        text = loc(g["ActionEffectTextOverride"]) or loc(g["BirthEffectTextOverride"])
        if not text and eff:
            text = " / ".join(loc(f"LOC_{e[0]}_DESCRIPTION") for e in eff if f"LOC_{e[0]}_DESCRIPTION" in EN)
        works = DB.execute("SELECT Name FROM GreatWorks WHERE GreatPersonIndividualType=?", (g["GreatPersonIndividualType"],)).fetchall()
        rows.append([loc(g["ClassName"]), loc(g["Name"]), ERA.get(g["EraType"], ""), g["ActionCharges"] or "", text,
                     ", ".join(loc(w[0]) for w in works)])
    write_list("great_people", "Great People", "Every great person by class and era. Ability text where the game defines it; artists/writers/musicians list their great works.",
               ["Class", "Name", "Era", "Charges", "Ability", "Great works"], rows)

    rows = [[loc(g["Name"]), loc(g["Title"]), loc(g["Description"])] for g in DB.execute("SELECT * FROM Governors ORDER BY Name")]
    write_list("governors", "Governors", "The governors.", ["Governor", "Title", "Role"], rows)
    rows = []
    for p in DB.execute("""SELECT gp.*, g.Name AS GName FROM GovernorPromotions gp JOIN GovernorPromotionSets s ON s.GovernorPromotion=gp.GovernorPromotionType
                           JOIN Governors g ON g.GovernorType=s.GovernorType ORDER BY g.Name, gp.Level, gp.Column"""):
        rows.append([loc(p["GName"]), p["Level"], loc(p["Name"]), loc(p["Description"])])
    write_list("governor_promotions", "Governor Promotions", "Each governor's promotion tree (level 0 = base ability).", ["Governor", "Level", "Promotion", "Effect"], rows)

    # ---- Map: resources, terrain, features, natural wonders ------------------------------------------
    rows = [[loc(r["Name"]), r["ResourceClassType"].replace("RESOURCECLASS_", "").title(), yields("Resource_YieldChanges", "ResourceType", r["ResourceType"]),
             unlock(r["PrereqTech"], r["PrereqCivic"]), r["Happiness"] or "", r["Frequency"], r["SeaFrequency"] or ""]
            for r in DB.execute("SELECT * FROM Resources ORDER BY ResourceClassType, Name")]
    write_list("resources", "Resources", "Bonus, luxury, strategic and artifact resources.", ["Resource", "Class", "Yields", "Revealed by", "Amenities", "Land frequency", "Sea frequency"], rows)

    rows = [[loc(t["Name"]), t["MovementCost"], t["DefenseModifier"] or "", t["Appeal"] or "", yields("Terrain_YieldChanges", "TerrainType", t["TerrainType"])]
            for t in DB.execute("SELECT * FROM Terrains ORDER BY Name")]
    write_list("terrains", "Terrains", "Base terrain types.", ["Terrain", "Move cost", "Defense", "Appeal", "Yields"], rows)

    for nw, stem, title in ((0, "features", "Terrain Features"), (1, "natural_wonders", "Natural Wonders")):
        rows = [[loc(f["Name"]), f["MovementChange"] or "", f["DefenseModifier"] or "", f["Appeal"] or "",
                 "Yes" if f["Impassable"] else "", yields("Feature_YieldChanges", "FeatureType", f["FeatureType"]), loc(f["Description"])]
                for f in DB.execute("SELECT * FROM Features WHERE IFNULL(NaturalWonder,0)=? ORDER BY Name", (nw,))]
        write_list(stem, title, f"All {title.lower()}.", ["Name", "Extra move cost", "Defense", "Appeal", "Impassable", "Yields", "Description"], rows)

    # ---- Game setup: eras, difficulties, speeds, maps, victories -------------------------------------
    rows = [[e["ChronologyIndex"], loc(e["Name"]), e["GreatPersonBaseCost"], e["WarmongerPoints"], e["EmbarkedUnitStrength"]]
            for e in DB.execute("SELECT * FROM Eras ORDER BY ChronologyIndex")]
    write_list("eras", "Eras", "Game eras in order.", ["#", "Era", "Great person base cost", "Warmonger points", "Embarked strength"], rows)

    diff_mods = {}
    if has_table("TraitModifiers"):
        for r in DB.execute("""SELECT m.ModifierId, a.Name, a.Value FROM Modifiers m JOIN ModifierArguments a USING(ModifierId)
                               WHERE m.ModifierId LIKE 'HIGH_DIFFICULTY%' OR m.ModifierId LIKE 'LOW_DIFFICULTY%'"""):
            diff_mods.setdefault(r[0], []).append(f"{r[1]}={r[2]}")
    rows = [[i + 1, loc(d["Name"]), d["DifficultyType"]] for i, d in enumerate(DB.execute(
            "SELECT d.* FROM Difficulties d LEFT JOIN Types t ON t.Type=d.DifficultyType ORDER BY t.rowid"))]
    write_list("difficulties", "Difficulty Levels", "Difficulty levels, easiest first. AI bonuses per level scale from the HIGH/LOW_DIFFICULTY modifiers (see tables/Modifiers.csv).",
               ["#", "Level", "Id"], rows)

    rows = [[loc(g["Name"]), g["CostMultiplier"], loc(g["Description"])] for g in DB.execute("SELECT * FROM GameSpeeds ORDER BY CostMultiplier")]
    write_list("game_speeds", "Game Speeds", "Cost multiplier in percent of Standard.", ["Speed", "Cost %", "Description"], rows)

    rows = [[loc(m["Name"]), m["DefaultPlayers"], m["GridWidth"], m["GridHeight"], m["NumNaturalWonders"], loc(m["Description"])]
            for m in DB.execute("SELECT * FROM Maps ORDER BY GridWidth")]
    write_list("map_sizes", "Map Sizes", "Map size presets.", ["Size", "Default players", "Width", "Height", "Natural wonders", "Description"], rows)

    rows = [[loc(v["Name"]), "Yes" if v["EnabledByDefault"] else "No", loc(v["Description"]) or loc(v["Blurb"])] for v in DB.execute("SELECT * FROM Victories")]
    write_list("victories", "Victory Types", "Ways to win.", ["Victory", "On by default", "Description"], rows)

    # ---- Gathering Storm / Rise & Fall systems ---------------------------------------------------
    rows = [[loc(m["Name"]), m["EraScore"], ERA.get(m["MinimumGameEra"], ""), ERA.get(m["MaximumGameEra"], ""), loc(m["Description"])]
            for m in DB.execute("SELECT * FROM Moments ORDER BY EraScore DESC, Name")]
    write_list("historic_moments", "Historic Moments", "Era-score moments.", ["Moment", "Era score", "From era", "Until era", "Trigger"], rows)

    if has_table("CommemorationTypes"):
        rows = [[loc(c["CategoryDescription"]), loc(c["GoldenAgeBonusDescription"]), loc(c["NormalAgeBonusDescription"]), loc(c["DarkAgeBonusDescription"])]
                for c in DB.execute("SELECT * FROM CommemorationTypes")]
        write_list("dedications", "Age Dedications", "Golden/normal/dark age dedications.", ["Dedication", "Golden age", "Normal age", "Dark age"], rows)

    rows = [[loc(r["Name"]), r["Severity"], r["ClimateChangePoints"] or "", loc(r["Description"]), loc(r["EffectString"])]
            for r in DB.execute("SELECT * FROM RandomEvents ORDER BY Name, Severity")]
    write_list("disasters", "Natural Disasters & Random Events", "Storms, floods, volcanoes, droughts... by severity.", ["Event", "Severity", "Climate pts", "Description", "Effect"], rows)

    rows = [[loc(r["Name"]), ERA.get(r["EarliestEra"], ""), loc(r["Effect1Description"]), loc(r["Effect2Description"])]
            for r in DB.execute("SELECT * FROM Resolutions ORDER BY Name")]
    write_list("world_congress", "World Congress Resolutions", "Option A / option B for each resolution.", ["Resolution", "From era", "Option A", "Option B"], rows)

    if has_table("Emergencies_XP2"):
        rows = [[loc(f"LOC_{e['EmergencyType']}_NAME") if f"LOC_{e['EmergencyType']}_NAME" in EN else e["EmergencyType"],
                 "Hostile" if e["Hostile"] else "Non-hostile", loc(f"LOC_{e['EmergencyType']}_DESCRIPTION") if f"LOC_{e['EmergencyType']}_DESCRIPTION" in EN else ""]
                for e in DB.execute("SELECT * FROM Emergencies_XP2")]
        write_list("emergencies", "Emergencies & Competitions", "World Congress emergencies and scored competitions.", ["Emergency", "Kind", "Description"], rows)

    rows = [[loc(a["Name"]), loc(a["Description"])] for a in DB.execute("SELECT * FROM Alliances")] if has_table("Alliances") else []
    if rows:
        write_list("alliances", "Alliance Types", "Alliance types.", ["Alliance", "Description"], rows)

    rows = [[loc(a["Name"]), loc(a["Description"])] for a in DB.execute("SELECT * FROM Agendas ORDER BY Name") if a["Name"]]
    write_list("agendas", "Leader Agendas", "Historical and random AI agendas.", ["Agenda", "Description"], rows)

    rows = [[loc(g["Name"]), g["GreatWorkObjectType"].replace("GREATWORKOBJECT_", "").title(), name_of(g["GreatPersonIndividualType"], "GreatPersonIndividuals", "GreatPersonIndividualType"),
             ERA.get(g["EraType"], ""), g["Tourism"]] for g in DB.execute("SELECT * FROM GreatWorks ORDER BY GreatWorkObjectType, Name")]
    write_list("great_works", "Great Works", "Art, writing, music, relics and artifacts.", ["Work", "Type", "Creator", "Era", "Tourism"], rows)

    if has_table("LoyaltyLevels"):
        rows = [[dict(r)[k] for k in dict(r)] for r in DB.execute("SELECT * FROM LoyaltyLevels")]
        write_list("loyalty_levels", "Loyalty Levels", "City loyalty thresholds.", list(dict(DB.execute("SELECT * FROM LoyaltyLevels").fetchone()).keys()), rows)

    rows = [[r["Name"], r["Value"]] for r in DB.execute("SELECT * FROM GlobalParameters ORDER BY Name")]
    write_list("global_parameters", "Global Game Parameters", "Engine-wide tuning constants (combat, growth, costs...).", ["Parameter", "Value"], rows)

    # ---- Text ------------------------------------------------------------------------------------
    langs = [r[0] for r in DB.execute("SELECT Language FROM LocalizedText GROUP BY 1 HAVING count(*) > 100 ORDER BY 1")]
    with open(TEXT / "text_en_US.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Tag", "Text", "Source file"])
        w.writerows(DB.execute("SELECT Tag, Text, Source FROM LocalizedText WHERE Language='en_US' ORDER BY Tag"))
    pivot = {}
    for tag, lang, text in DB.execute("SELECT Tag, Language, Text FROM LocalizedText"):
        pivot.setdefault(tag, {})[lang] = text
    with open(TEXT / "text_all_languages.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Tag"] + langs)
        for tag in sorted(pivot):
            w.writerow([tag] + [pivot[tag].get(lg, "") for lg in langs])
    # Readable groupings of the English text by prefix family.
    groups = {"civilopedia": "LOC_PEDIA_", "quotes": "_QUOTE", "loading_screens": "LOC_LOADING_", "diplomacy_dialogue": "LOC_DIPLO_",
              "tutorial": "LOC_TUTORIAL_", "advisor": "LOC_ADVISOR_", "historic_moments_text": "LOC_MOMENT_"}
    for stem, needle in groups.items():
        with open(TEXT / f"text_{stem}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Tag", "Text"])
            w.writerows((t, x) for t, x in sorted(EN.items()) if needle in t)

    # ---- Raw tables ------------------------------------------------------------------------------
    tables = [r[0] for r in DB.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    raw_counts = []
    for t in tables:
        if t == "LocalizedText":
            continue
        cur = DB.execute(f'SELECT * FROM "{t}"')
        data = cur.fetchall()
        raw_counts.append((t, len(data)))
        if not data:
            continue
        with open(TABLES / f"{t}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(tuple(r) for r in data)

    # ---- README ------------------------------------------------------------------------------------
    text_counts = DB.execute("SELECT Language, count(*) FROM LocalizedText GROUP BY 1 HAVING count(*) > 100 ORDER BY 1").fetchall()
    md = [f"# {game_title} — extracted game data", "",
          "Ruleset: **Gathering Storm** with all "
          "bundled DLC civs (Australia, Aztec, Indonesia/Khmer, Macedonia/Persia, Nubia, Poland, Vikings). Values are the final "
          "in-game values after every expansion/DLC override was applied.", "",
          "## Lists", "", "| List | Entries | What it contains |", "|---|---|---|"]
    md += [f"| [{t}](lists/{s}.md) | {n} | {b} |" for t, s, n, b in INDEX]
    md += ["", "## Text", "", f"{len(EN)} English strings; {len(text_counts)} languages: " + ", ".join(f"{lang} ({n})" for lang, n in text_counts), "",
           "- [text/text_en_US.csv](text/text_en_US.csv) — every English string (tag, text, source file)",
           "- [text/text_all_languages.csv](text/text_all_languages.csv) — every string side by side in all languages"]
    md += [f"- [text/text_{s}.csv](text/text_{s}.csv)" for s in groups]
    md += ["", "## Raw tables", "", f"All {len(raw_counts)} gameplay tables as CSV in [tables/](tables/) "
           "(also queryable in `../civ6_gameplay.sqlite`). Largest:", "", "| Table | Rows |", "|---|---|"]
    md += [f"| {t} | {n} |" for t, n in sorted(raw_counts, key=lambda x: -x[1])[:40]]
    (OUT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    DB.close()
    return {"lists": len(INDEX), "tables": len(raw_counts), "strings": len(EN)}
