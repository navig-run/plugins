"""Filename repair, one damage class per test.

Every case here is a real filename from the archive this plugin was built for.
"""

from __future__ import annotations

from navig_cabinet.paperwork.names import normalized_stem, repair, slug


def test_plus_encoded_spaces_become_spaces():
    assert repair("DECLARATION+DE+DOMICILE+Ukraine+pieces") == "DECLARATION DE DOMICILE Ukraine pieces"


def test_fullwidth_colon_becomes_hyphen():
    assert repair("Cybesis Studios (7_10_2025 12：06：31 AM)") == (
        "Cybesis Studios (7_10_2025 12-06-31 AM)"
    )


def test_cp1251_mojibake_declaration_is_repaired():
    # UTF-8 'é' read back as cp1251 renders as 'й'.
    assert repair("dйclaration de CA_T3_2022") == "déclaration de CA_T3_2022"


def test_combining_mark_damage_generales_is_repaired():
    assert "GENERALES" in repair("CONDITIONS GEěNEěRALES D UTILISATION")


def test_unclosed_paren_fragment_is_stripped():
    assert repair("Attestation_de_tiers_payant_MIZERNY_SERGUEI (1") == (
        "Attestation_de_tiers_payant_MIZERNY_SERGUEI"
    )


def test_repair_is_idempotent():
    for name in ("dйclaration de CA", "DECLARATION+DE+DOMICILE", "foo (1", "plain name"):
        once = repair(name)
        assert repair(once) == once


def test_a_clean_french_name_is_not_damaged():
    """Anti-vacuity: the guards must leave correct names completely alone."""
    clean = "Avis_d’échéance_de_cotisation_01_04_2025"
    assert repair(clean) == clean


def test_genuine_cyrillic_is_not_treated_as_mojibake():
    """The regression that motivated the two-part guard.

    A birth certificate's Cyrillic name round-trips through cp1251 into Latin-1
    accent soup, which *raises* the French-accent count — so an accent-count guard
    alone silently destroys it.
    """
    name = "СВИДЕТЕЛЬСТВО"
    assert repair(name) == name


def test_slug_is_ascii_kebab_lowercase():
    assert slug("Avis d'échéance 01/04/2025") == "avis-d-echeance-01-04-2025"


def test_slug_respects_max_length_and_never_ends_in_a_dash():
    out = slug("a" * 200, max_len=20)
    assert len(out) <= 20 and not out.endswith("-")


def test_duplicate_marker_collapses_for_grouping():
    assert normalized_stem("INV-000022-25 (1)") == normalized_stem("INV-000022-25")


def test_compressed_variant_shares_a_grouping_key():
    assert normalized_stem("20230326220945_compressed") == normalized_stem("20230326220945")


def test_promesse_dembauche_variants_are_never_grouped():
    """Two genuinely different documents that must not be merged."""
    a = normalized_stem("Promesse d’embauche Sergei")
    b = normalized_stem("Promesse d’embauche SergeiM")
    assert a != b


# ── Cyrillic is transliterated, not discarded ────────────────────────────────


def test_a_cyrillic_name_becomes_readable_latin_not_an_empty_string():
    """Dropping non-ASCII made every Russian filename slug to "".

    Callers then fall back to a content hash, so a donor sheet holding names, phone
    numbers and tax ids filed itself as `031dda10.xlsx` — unidentifiable without
    opening it. For an operator whose documents are largely Russian that is the
    common case, not an edge case.
    """
    assert slug("Донаты") == "donaty"
    assert slug("Накладная") == "nakladnaya"


def test_multi_letter_cyrillic_sequences_are_not_truncated():
    """`щ` must be consumed before `ш`, or it romanises as the wrong sound."""
    assert slug("Расшифровка") == "rasshifrovka"
    assert slug("Щука") == "shchuka"


def test_a_cyrillic_phrase_keeps_its_word_boundaries():
    assert slug("Бьюти Сфера Инстаграм") == "byuti-sfera-instagram"
    assert slug("СВИДЕТЕЛЬСТВО О РОЖДЕНИИ") == "svidetelstvo-o-rozhdenii"


def test_latin_names_are_completely_unaffected():
    """Anti-regression: transliteration must not touch what already worked."""
    assert slug("INV-000022-25") == "inv-000022-25"
    assert slug("Avis d'échéance 01/04/2025") == "avis-d-echeance-01-04-2025"
    assert slug("") == ""


def test_repair_still_leaves_genuine_cyrillic_alone():
    """Transliteration belongs in slug(), not in repair() — the original name is kept."""
    assert repair("СВИДЕТЕЛЬСТВО") == "СВИДЕТЕЛЬСТВО"
