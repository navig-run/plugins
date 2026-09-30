"""The personal profile: a person's administrative mail, filed and dated.

Fixtures are synthetic French letters — the shapes are real (CAF, EDF, préfecture),
the identifiers are not.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from navig_cabinet.paperwork import courrier, mailroom, profiles
from navig_cabinet.paperwork.classify import classify, decision_for
from navig_cabinet.paperwork.naming import destination
from navig_cabinet.paperwork.plan import PlanRow
from navig_cabinet.paperwork.space import PaperworkPaths

CAF = """CAISSE D ALLOCATIONS FAMILIALES DE L HERAULT
Montpellier, le 12 septembre 2026
N de dossier : 1234567A
Objet : Aide personnalisee au logement - pieces manquantes
Monsieur,
Afin de poursuivre l etude de votre dossier APL, merci de nous retourner avant le
30 septembre 2026 la quittance de loyer.
"""

EDF = """EDF - Votre facture d electricite
Date de facture : 05/09/2026
Montant a payer : 84,20 EUR
Prelevement le 20/09/2026
N de contrat : 45AB-778
"""

PREFECTURE = """PREFECTURE DE L HERAULT
Montpellier, le 3 septembre 2026
Objet : Convocation - titre de sejour
Vous etes invite a vous presenter sous 15 jours muni des pieces justificatives.
"""

URSSAF = """URSSAF Languedoc-Roussillon
Cotisations sociales des travailleurs independants
SIRET 753 204 742 00047
"""

NIR_ONLY = "Assurance Maladie\nN : 1 86 02 99 123 193 76\nvotre attestation de droits"


# ── Classification ─────────────────────────────────────────────────────────


class TestPersonalClassification:
    def test_a_caf_letter_files_under_logement(self):
        pc = classify(CAF, "IMG_2026.jpg", profile="personal")
        assert pc.doc_class == "courrier-logement"
        assert decision_for(pc) == "migrate"
        assert pc.emetteur == "caf"
        assert pc.echeance == "2026-09-30"
        assert pc.action_requise == "fournir"
        assert pc.reference == "1234567A"
        assert pc.doc_date == "2026-09-12"
        assert pc.objet.startswith("Aide personnalisee au logement")

    def test_an_edf_bill_files_under_energie_telecom_with_prelevement_as_deadline(self):
        pc = classify(EDF, "facture.pdf", profile="personal")
        assert pc.doc_class == "courrier-energie-telecom"
        assert decision_for(pc) == "migrate"
        assert pc.echeance == "2026-09-20"
        assert pc.action_requise == "payer"
        assert pc.reference == "45AB-778"

    def test_a_relative_deadline_is_anchored_on_the_letter_date(self):
        pc = classify(PREFECTURE, "scan0001.pdf", profile="personal")
        assert pc.doc_class == "courrier-sejour"
        assert pc.echeance == "2026-09-18"
        assert "sous 15 jours" in pc.echeance_source

    def test_company_paperwork_in_the_personal_inbox_is_handed_off(self):
        pc = classify(URSSAF, "scan.pdf", profile="personal")
        assert pc.doc_class == "business-handoff"
        assert pc.veto_subclass == "business"
        assert decision_for(pc) == "handoff"

    def test_another_persons_document_is_never_filed_under_either_profile(self):
        text = "Ordonnance pour Elvira Siadova"
        assert classify(text, "x.pdf", profile="personal").veto_subclass == "thirdparty"
        assert classify(text, "x.pdf").veto_subclass == "thirdparty"

    def test_business_profile_is_unchanged_by_the_personal_one(self):
        pc = classify(CAF, "IMG_2026.jpg")
        assert pc.doc_class == "personal-handoff" and pc.veto_subclass == "housing"

    def test_unreadable_document_is_unknown_not_filed(self):
        pc = classify("", "IMG_0001.jpg", profile="personal")
        assert pc.doc_class == "unknown"
        assert decision_for(pc) == "review"

    def test_text_only_family_without_sender_lands_in_review(self):
        pc = classify("votre quittance de loyer du mois", "doc.pdf", profile="personal")
        assert pc.doc_class == "courrier-logement"
        assert decision_for(pc) == "review"


# ── Naming ─────────────────────────────────────────────────────────────────


class TestPersonalNaming:
    def test_a_camera_name_is_replaced_by_the_objet(self):
        row = PlanRow(
            doc_class="courrier-logement",
            doc_date="2026-09-12",
            emetteur="caf",
            objet="Aide personnalisee au logement",
            src=r"H:\IMG_2026.jpg",
            sha256="ab" * 32,
        )
        assert (
            destination(row)
            == "personal/logement/2026/2026-09-12-caf-aide-personnalisee-au-logement.jpg"
        )

    def test_a_meaningful_name_is_kept(self):
        row = PlanRow(
            doc_class="courrier-sante",
            doc_date="2026-01-02",
            emetteur="cpam",
            objet="whatever",
            src=r"H:\attestation-droits-2026.pdf",
            sha256="ab" * 32,
        )
        assert (
            destination(row)
            == "personal/sante/2026/2026-01-02-cpam-attestation-droits-2026.pdf"
        )

    def test_no_sender_no_date(self):
        row = PlanRow(
            doc_class="courrier-banque", src=r"H:\releve.pdf", sha256="ab" * 32
        )
        assert destination(row) == "personal/banque/undated/releve.pdf"

    def test_business_handoff_takes_no_destination(self):
        row = PlanRow(doc_class="business-handoff", src=r"H:\urssaf.pdf")
        assert destination(row) == ""


# ── Courrier field finders ─────────────────────────────────────────────────


class TestCourrierFields:
    def test_relative_deadline_without_letter_date_is_reported_not_guessed(self):
        iso, source = courrier.find_echeance("merci de repondre sous 15 jours", "")
        assert iso == "" and source.startswith("relative:")

    def test_deadline_before_the_letter_is_ignored(self):
        iso, _ = courrier.find_echeance("avant le 01/01/2020", "2026-09-12")
        assert iso == ""

    def test_word_numbers_and_months(self):
        iso, _ = courrier.find_echeance("dans un delai de deux mois", "2026-01-01")
        assert iso == "2026-03-02"
        iso, _ = courrier.find_echeance("sous quinzaine", "2026-01-01")
        assert iso == "2026-01-16"

    def test_reference_skips_a_nir_shaped_number(self):
        assert courrier.find_reference(NIR_ONLY) == ""
        assert courrier.find_reference("Reference : ABC-2026-77 merci") == "ABC-2026-77"

    def test_action_families(self):
        assert courrier.find_action("montant a payer 12 EUR") == "payer"
        assert (
            courrier.find_action("merci de nous transmettre le justificatif")
            == "fournir"
        )
        assert (
            courrier.find_action("vous devez actualiser votre situation") == "declarer"
        )
        assert courrier.find_action("convocation le 3 mars") == "repondre"
        assert courrier.find_action("pour information") == "info"

    def test_objet_and_generic_stem(self):
        assert (
            courrier.find_objet("Objet : Renouvellement de droits\nMonsieur,")
            == "Renouvellement de droits"
        )
        assert courrier.is_generic_stem("IMG_4021")
        assert courrier.is_generic_stem("Scan 2026-09-12 10.11.12")
        assert not courrier.is_generic_stem("attestation-caf-2026")

    def test_organism_label_and_reference_hash(self):
        assert courrier.organism_label("caf") == "CAF"
        assert courrier.organism_label("") == ""
        assert courrier.reference_hash("X") == courrier.reference_hash("X")
        assert courrier.reference_hash("") == ""

    def test_channel_from_routes_keywords_then_bucket_fallback(self):
        channels = [
            ("#logement", ["loyer", "apl"]),
            ("#echeances", ["echeance"]),
            ("#courrier", ["lettre"]),
        ]
        assert (
            courrier.find_channel("votre apl et votre loyer", "x", "sante", channels)
            == "#logement"
        )
        assert courrier.find_channel("rien", "x", "sante", channels) == "#sante"
        assert (
            courrier.find_channel("rien", "x", "banque", channels, has_echeance=True)
            == "#echeances"
        )
        assert courrier.find_channel("rien", "x", "banque", []) == ""


# ── Profiles ───────────────────────────────────────────────────────────────


class TestProfiles:
    def test_profile_from_space_config(self, tmp_path):
        (tmp_path / ".navig").mkdir()
        (tmp_path / ".navig" / "config.yaml").write_text(
            "paperwork:\n  profile: personal\nrenewal_alert_days: 45\n",
            encoding="utf-8",
        )
        assert profiles.resolve_profile(tmp_path) == "personal"
        assert profiles.resolve_profile(tmp_path, "business") == "business"
        assert profiles.renewal_alert_days(tmp_path) == 45

    def test_default_and_invalid(self, tmp_path):
        assert profiles.resolve_profile(tmp_path) == "business"
        assert profiles.renewal_alert_days(tmp_path) == 30
        with pytest.raises(ValueError):
            profiles.resolve_profile(tmp_path, "nope")


# ── Mailroom: ledger, échéances, radar ─────────────────────────────────────


def _space(tmp_path) -> PaperworkPaths:
    root = tmp_path / "paperwork-space"
    (root / ".navig").mkdir(parents=True)
    return PaperworkPaths(root)


def _receipt(paths: PaperworkPaths, row_ids: list[str]) -> Path:
    paths.receipts_dir.mkdir(parents=True, exist_ok=True)
    r = paths.receipt("t1")
    with r.open("w", encoding="utf-8") as f:
        for rid in row_ids:
            f.write(json.dumps({"row_id": rid, "action": "migrated"}) + "\n")
    return r


class TestMailroom:
    def test_record_filed_is_idempotent_and_hashes_the_reference(self, tmp_path):
        paths = _space(tmp_path)
        row = PlanRow(
            row_id="r1",
            decision="migrate",
            doc_class="courrier-logement",
            dest_rel="personal/logement/2026/x.jpg",
            src="x.jpg",
            sha256="c" * 64,
            emetteur="caf",
            echeance="2026-09-30",
            reference="1234567A",
            action_requise="fournir",
            original_name="x.jpg",
            objet="APL",
        )
        receipt = _receipt(paths, ["r1"])
        filed, n_ledger, n_due = mailroom.record_filed([row], paths, receipt)
        assert [r.row_id for r in filed] == ["r1"] and n_ledger == 1 and n_due == 1

        mp = mailroom.mailroom_paths(paths)
        entry = mailroom.read_jsonl(mp.courrier_jsonl)[0]
        assert entry["emetteur_label"] == "CAF"
        assert entry["reference_hash"] and "1234567A" not in json.dumps(entry)

        # Same document again: nothing new, one row.
        filed, n_ledger, n_due = mailroom.record_filed([row], paths, receipt)
        assert n_ledger == 0 and n_due == 0
        assert len(mailroom.read_jsonl(mp.courrier_jsonl)) == 1
        assert len(mailroom.read_jsonl(mp.echeances_jsonl)) == 1

    def test_only_rows_the_receipt_says_were_migrated_are_recorded(self, tmp_path):
        paths = _space(tmp_path)
        rows = [
            PlanRow(
                row_id="a", doc_class="courrier-sante", sha256="a" * 64, src="a.pdf"
            ),
            PlanRow(
                row_id="b", doc_class="courrier-sante", sha256="b" * 64, src="b.pdf"
            ),
            PlanRow(
                row_id="c", doc_class="invoice-received", sha256="c" * 64, src="c.pdf"
            ),
        ]
        filed, n, _ = mailroom.record_filed(rows, paths, _receipt(paths, ["a", "c"]))
        assert [r.row_id for r in filed] == ["a"] and n == 1
        assert mailroom.record_filed(rows, paths, None) == ([], 0, 0)

    def test_upsert_keeps_hand_edited_status(self, tmp_path):
        p = tmp_path / "e.jsonl"
        mailroom.upsert_jsonl(
            p, [{"id": "1", "due": "2026-01-01", "status": "open"}], key="id"
        )
        rows = mailroom.read_jsonl(p)
        rows[0]["status"] = "done"
        mailroom.write_jsonl(p, rows)
        mailroom.upsert_jsonl(p, [{"id": "1", "due": "2026-01-02"}], key="id")
        assert mailroom.read_jsonl(p) == [
            {"id": "1", "due": "2026-01-02", "status": "done"}
        ]

    def test_radar_statuses_ordering_and_recurrent(self, tmp_path):
        paths = _space(tmp_path)
        mp = mailroom.mailroom_paths(paths)
        today = date(2026, 9, 19)
        mailroom.write_jsonl(
            mp.echeances_jsonl,
            [
                {
                    "id": "late",
                    "due": "2026-09-10",
                    "organisme": "CAF",
                    "objet": "APL",
                    "status": "open",
                },
                {
                    "id": "soon",
                    "due": "2026-09-24",
                    "organisme": "EDF",
                    "objet": "facture",
                    "status": "open",
                },
                {
                    "id": "far",
                    "due": "2026-12-01",
                    "organisme": "X",
                    "objet": "later",
                    "status": "open",
                },
                {
                    "id": "done",
                    "due": "2026-09-20",
                    "organisme": "Y",
                    "objet": "closed",
                    "status": "done",
                },
            ],
        )
        mp.base.mkdir(parents=True, exist_ok=True)
        mp.recurrent_yaml.write_text(
            "recurrent:\n  - id: cni\n    objet: Carte nationale d'identite\n    organisme: ANTS\n    due: 2026-10-10\n",
            encoding="utf-8",
        )
        items = mailroom.radar(paths, horizon_days=30, today=today)
        assert [i.id for i in items] == ["late", "soon", "cni"]
        assert items[0].status == mailroom.EN_RETARD and items[0].days == -9
        assert items[1].status == mailroom.URGENT
        assert items[2].status == mailroom.A_VENIR and items[2].recurrent

        report = mailroom.write_echeancier(paths, items, horizon_days=30, today=today)
        body = report.read_text(encoding="utf-8")
        assert "Top 3 urgences" in body and "EN RETARD" in body and "ANTS" in body

        text = mailroom.telegram_radar_text(items, horizon_days=30)
        assert "🔴" in text and "CAF" in text
        assert mailroom.telegram_radar_text([], horizon_days=30).startswith("🗓")

    def test_telegram_filed_text_has_no_reference(self):
        row = PlanRow(
            doc_class="courrier-logement",
            dest_rel="personal/logement/2026/x.jpg",
            emetteur="caf",
            echeance="2026-09-30",
            reference="SECRET-REF-1",
            action_requise="fournir",
            original_name="x.jpg",
            objet="APL <pieces>",
        )
        text = mailroom.telegram_filed_text([row])
        assert "SECRET-REF-1" not in text
        assert "CAF" in text and "2026-09-30" in text and "&lt;pieces&gt;" in text
        assert mailroom.telegram_filed_text([]) == ""


class TestReplyDraft:
    def _space(self, tmp_path):
        root = tmp_path / "space"
        (root / ".navig").mkdir(parents=True)
        (root / "docs" / "prompts").mkdir(parents=True)
        (root / "docs" / "prompts" / "courrier-redaction.md").write_text("STYLE: formal French.", encoding="utf-8")
        (root / "personal" / "logement" / "2026").mkdir(parents=True)
        letter = root / "personal" / "logement" / "2026" / "2026-09-12-caf-apl.txt"
        letter.write_text(CAF, encoding="utf-8")
        return root, letter

    def test_cloud_refused_before_reading_the_letter(self, tmp_path, monkeypatch):
        from navig.llm import guard
        from navig_cabinet.paperwork import reply as R

        root, letter = self._space(tmp_path)
        monkeypatch.setattr(guard, "resolve", lambda **kw: guard.Resolved("openai", "gpt"))
        with pytest.raises(guard.CloudRefused):
            R.draft_reply(root, str(letter), instruction="joindre la quittance")
        assert not (root / "out").exists()

    def test_draft_is_written_with_facts_and_never_sent(self, tmp_path, monkeypatch):
        from navig.llm import guard
        from navig_cabinet.paperwork import reply as R

        root, letter = self._space(tmp_path)
        seen = {}

        def fake_generate(messages, **kw):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return "Madame, Monsieur,\nVeuillez trouver ci-joint…", guard.Resolved("ollama", "llama3")

        monkeypatch.setattr(guard, "resolve", lambda **kw: guard.Resolved("ollama", "llama3"))
        monkeypatch.setattr(guard, "generate", fake_generate)
        d = R.draft_reply(root, "personal/logement/2026/2026-09-12-caf-apl.txt",
                          instruction="joindre la quittance de juillet", today=date(2026, 9, 20))
        assert d.emetteur == "CAF" and d.reference == "1234567A" and d.echeance == "2026-09-30"
        assert d.path == root / "out" / "courriers" / "caf-aide-personnalisee-au-logement-pieces-manquantes-2026-09-20.fr.md"
        body = d.path.read_text(encoding="utf-8")
        assert "NON ENVOYÉ" in body and "Veuillez trouver" in body
        assert seen["system"] == "STYLE: formal French." and "1234567A" in seen["user"] and "quittance de juillet" in seen["user"]

    def test_document_outside_the_space_is_refused(self, tmp_path):
        from navig_cabinet.paperwork import reply as R

        root, _ = self._space(tmp_path)
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            R.resolve_document(root, str(outside))
