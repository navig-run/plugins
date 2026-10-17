"""``navig paperwork reply`` — draft the answer to a filed letter, in the space's own style.

The only place this plugin talks to a language model, and it goes through
``navig.llm.guard``: opt-in, provider printed, cloud refused without ``--allow-cloud``.
The letter's facts (émetteur, référence, échéance, action) come from the deterministic
``courrier`` pass over the document text, the tone from the space's prompt
(``docs/prompts/courrier-redaction.md``), the intent from the operator's ``--say``.

Output is a **draft file** under ``out/courriers/`` — the space's convention for
correspondence — never anything sent. Reading the document again (rather than the
ledger) is deliberate: the ledger keeps only a hash of the dossier reference, and a
reply needs the real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from navig_cabinet._core import NEWER_CORE, llm_guard

from . import courrier as C
from .extract import extract_text
from .names import slug


def _guard():
    g = llm_guard()
    if g is None:
        raise RuntimeError(f"drafting replies needs {NEWER_CORE}")
    return g


DEFAULT_STYLE_REL = "docs/prompts/courrier-redaction.md"
OUT_DIR_REL = "out/courriers"
TEXT_CAP = 6000


@dataclass
class ReplyDraft:
    path: Path
    provider: str
    emetteur: str
    objet: str
    reference: str
    echeance: str
    action: str


def space_ai(space_root: Path) -> dict:
    """``mailroom.ai`` from the space config — the shared model pin (see navig_email.space)."""
    from .profiles import read_space_config

    block = (read_space_config(space_root).get("mailroom") or {}).get("ai")
    return block if isinstance(block, dict) else {}


def load_style(space_root: Path, explicit: Path | None) -> str:
    path = explicit or (space_root / DEFAULT_STYLE_REL)
    if not path.exists():
        raise FileNotFoundError(f"style prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def resolve_document(space_root: Path, ref: str) -> Path:
    """A path (absolute, or relative to the space root) to a filed letter."""
    p = Path(ref).expanduser()
    if not p.is_absolute():
        p = space_root / p
    if not p.is_file():
        raise FileNotFoundError(f"document not found: {p}")
    try:
        p.resolve().relative_to(space_root.resolve())
    except ValueError as exc:
        raise ValueError(f"document is outside the space: {p}") from exc
    return p


def facts_for(doc: Path) -> tuple[str, C.Courrier]:
    ex = extract_text(doc, ocr=True, cloud=False, max_pages=4)
    text = ex.text or ""
    from .classify import _parse_date

    doc_date = _parse_date(text, doc.name)
    return text, C.read_courrier(text, doc.name, doc_date=doc_date)


def build_messages(
    *, style: str, letter_text: str, facts: C.Courrier, instruction: str, today: date
) -> list[dict[str, str]]:
    who = facts.emetteur_label or facts.emetteur or "l'organisme expéditeur"
    context = (
        f"Date du jour : {today.isoformat()}.\n"
        f"Destinataire (organisme) : {who}.\n"
        f"Référence de dossier citée dans la lettre reçue : {facts.reference or 'non trouvée — ne pas inventer'}.\n"
        f"Échéance mentionnée : {facts.echeance or 'aucune'}.\n"
        f"Action demandée par la lettre : {facts.action}.\n"
        f"Objet de la lettre reçue : {facts.objet or 'non précisé'}.\n\n"
        f"Ce que je veux répondre : {instruction.strip()}\n\n"
        "Texte de la lettre reçue (OCR, peut contenir des coquilles) :\n"
        "-----\n" + letter_text[:TEXT_CAP] + "\n-----\n\n"
        "Rédige la lettre de réponse complète, prête à imprimer, en français formel, avec en-tête, "
        "objet, corps, pièces jointes et signature. Ne cite que des références présentes dans la "
        "lettre reçue ; n'invente ni montant ni numéro. Réponds uniquement par la lettre."
    )
    return [{"role": "system", "content": style}, {"role": "user", "content": context}]


def draft_path(space_root: Path, facts: C.Courrier, doc: Path, today: date) -> Path:
    who = (
        slug(facts.emetteur or facts.emetteur_label or "organisme", max_len=30)
        or "organisme"
    )
    objet = slug(facts.objet or doc.stem, max_len=50) or "reponse"
    return space_root / OUT_DIR_REL / f"{who}-{objet}-{today.isoformat()}.fr.md"


def draft_reply(
    space_root: Path,
    document: str,
    *,
    instruction: str,
    style_path: Path | None = None,
    model: str | None = None,
    allow_cloud: bool = False,
    dry_run: bool = False,
    today: date | None = None,
) -> ReplyDraft:
    """Write the draft (or, with *dry_run*, only resolve the provider and the facts)."""
    today = today or date.today()
    doc = resolve_document(space_root, document)
    style = load_style(space_root, style_path)
    ai = space_ai(space_root)
    model = model or str(ai.get("model") or "").strip() or None
    allow_cloud = allow_cloud or bool(ai.get("allow_cloud"))
    resolved = _guard().resolve(mode="chat", model=model)
    _guard().ensure_allowed(
        resolved, allow_cloud=allow_cloud
    )  # refuse BEFORE reading the letter

    letter_text, facts = facts_for(doc)
    out = draft_path(space_root, facts, doc, today)
    if dry_run:
        return ReplyDraft(
            out,
            str(resolved),
            facts.emetteur_label,
            facts.objet,
            facts.reference,
            facts.echeance,
            facts.action,
        )

    text, resolved = _guard().generate(
        build_messages(
            style=style,
            letter_text=letter_text,
            facts=facts,
            instruction=instruction,
            today=today,
        ),
        mode="chat",
        model=model,
        allow_cloud=allow_cloud,
        temperature=0.3,
        max_tokens=1400,
    )
    if not text.strip():
        raise RuntimeError(f"empty draft from {resolved}")
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- brouillon généré le {today.isoformat()} par navig paperwork reply · modèle {resolved} · "
        f"source {doc.relative_to(space_root).as_posix()} · NON ENVOYÉ — relire avant impression -->\n\n"
    )
    out.write_text(header + text.strip() + "\n", encoding="utf-8")
    return ReplyDraft(
        out,
        str(resolved),
        facts.emetteur_label,
        facts.objet,
        facts.reference,
        facts.echeance,
        facts.action,
    )
