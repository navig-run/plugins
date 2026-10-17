"""Who is writing, and in what voice — the two halves of a reply that reads as yours.

A model asked to "reply to this email" writes something generically polite and wrong: it does
not know that ``support@cybesis.com`` is a studio and ``devclined@gmail.com`` is a developer's
own account, so it invents a company, a role, and a register. Both halves are needed:

* **Identity** — WHERE the mail landed. Resolved from the account it arrived on and the alias it
  was addressed to (``mailroom/identities.yaml``). Carries the business description, the
  signature, and the language to answer in.
* **Style** — HOW to write. A markdown file that is used verbatim as the system prompt
  (``mailroom/styles/<name>.md``, falling back to the ones shipped with this plugin).

They compose: a style says *how*, an identity says *who and what*, and the identity always wins
on facts. Nothing here calls a model — this only builds the prompt; ``navig.llm.guard`` remains
the single door that decides whether a model may be called at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Styles shipped with the plugin; a space may add or override by filename.
BUILTIN_STYLES_DIR = Path(__file__).resolve().parent / "styles"

IDENTITIES_FILE = "identities.yaml"
STYLES_DIR = "styles"

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")


class VoiceError(RuntimeError):
    """A style or identity that was asked for by name and does not exist."""


# ── styles ──────────────────────────────────────────────────────────────────


def styles_dir(paths: Any | None) -> Path | None:
    return (paths.base / STYLES_DIR) if paths is not None else None


def available_styles(paths: Any | None = None) -> dict[str, Path]:
    """``{name: file}`` — the space's own styles shadow the built-in ones by name."""
    found: dict[str, Path] = {}
    for folder in (BUILTIN_STYLES_DIR, styles_dir(paths)):
        if folder is None or not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            name = f.stem.lower()
            if _SAFE_NAME.match(name):
                found[name] = f
    return found


def load_style(name: str, paths: Any | None = None) -> tuple[str, Path]:
    """``(prompt_text, file)`` for *name*, or raise with the list of what does exist."""
    key = (name or "").strip().lower()
    table = available_styles(paths)
    if key not in table:
        known = ", ".join(sorted(table)) or "none"
        raise VoiceError(f"unknown style {name!r} — available: {known}")
    text = table[key].read_text(encoding="utf-8").strip()
    if not text:
        raise VoiceError(f"style {key!r} is empty ({table[key]})")
    return text, table[key]


# ── identities ──────────────────────────────────────────────────────────────


@dataclass
class Identity:
    """Who the reply is written as."""

    name: str = "default"
    display: str = ""
    role: str = ""
    company: str = ""
    context: str = ""
    signature: str = ""
    language: str = ""
    accounts: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    def brief(self) -> str:
        """The facts a model must not invent, as prompt lines. Empty fields are dropped."""
        bits = [
            ("You are writing as", self.display),
            ("Your role", self.role),
            ("The organisation", self.company),
            ("About it", self.context.strip()),
            ("Answer in", self.language),
            ("Sign off with", self.signature),
        ]
        return "\n".join(f"{label}: {value}" for label, value in bits if str(value).strip())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip().lower() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return []


def read_identities(paths: Any) -> dict[str, Any]:
    """The parsed ``mailroom/identities.yaml``; ``{}`` when the space has none."""
    f = paths.base / IDENTITIES_FILE
    if not f.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — a broken file must not break replying
        raise VoiceError(f"{f} is not valid YAML: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _identity_from(name: str, raw: dict[str, Any]) -> Identity:
    match = raw.get("match") or {}
    return Identity(
        name=name,
        display=str(raw.get("display") or "").strip(),
        role=str(raw.get("role") or "").strip(),
        company=str(raw.get("company") or "").strip(),
        context=str(raw.get("context") or "").strip(),
        signature=str(raw.get("signature") or "").strip(),
        language=str(raw.get("language") or "").strip(),
        accounts=_as_list(match.get("accounts")),
        aliases=_as_list(match.get("aliases")),
        domains=_as_list(match.get("domains")),
    )


def identities(paths: Any) -> dict[str, Identity]:
    data = read_identities(paths)
    raw = data.get("identities") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip().lower(): _identity_from(str(k).strip().lower(), v or {})
        for k, v in raw.items()
        if isinstance(v, dict) or v is None
    }


def resolve_identity(
    paths: Any,
    *,
    account: str = "",
    alias: str = "",
    to: str = "",
    name: str = "",
) -> Identity | None:
    """Which identity a message belongs to.

    Most specific first — an explicit ``name``, then the **alias** the mail was addressed to
    (``support@`` and ``billing@`` can be different voices of the same mailbox), then the
    recipient domain, then the account it arrived on, then the file's ``default``. Alias beats
    account deliberately: one mailbox receives every alias, so the account alone cannot tell
    a studio enquiry from a personal one.
    """
    table = identities(paths)
    if not table:
        return None

    wanted = (name or "").strip().lower()
    if wanted:
        if wanted not in table:
            known = ", ".join(sorted(table)) or "none"
            raise VoiceError(f"unknown identity {wanted!r} — available: {known}")
        return table[wanted]

    alias_key = (alias or "").strip().lower().split("@")[0]
    to_addr = (to or "").strip().lower()
    to_domain = to_addr.split("@")[-1] if "@" in to_addr else ""
    acct = (account or "").strip().lower()

    if alias_key:
        for ident in table.values():
            if alias_key in ident.aliases:
                return ident
    if to_domain:
        for ident in table.values():
            if to_domain in ident.domains:
                return ident
    if acct:
        for ident in table.values():
            if acct in ident.accounts:
                return ident

    fallback = str(read_identities(paths).get("default") or "").strip().lower()
    return table.get(fallback) if fallback in table else None


# ── the prompt ──────────────────────────────────────────────────────────────

#: Rules that hold whatever style is chosen. A persona may shape the tone; it may never
#: invent a commitment, and the facts it is given always outrank the character it plays.
GUARDRAILS = (
    "Hard rules, above the style:\n"
    "- Never invent a fact, a price, a date, a deadline or a commitment. If something is "
    "unknown, say so plainly or ask for it.\n"
    "- Reply in the language of the incoming message unless told otherwise.\n"
    "- Keep the persona in the wording only. Never claim to be a different person or company "
    "than the identity below.\n"
    "- Write only the body of the reply: no subject line, no email headers, no commentary."
)


def build_prompt(
    style_text: str,
    identity: Identity | None,
    *,
    instruction: str = "",
) -> str:
    """The system prompt: style, then the guardrails, then the facts, then the ask."""
    parts = [style_text.strip(), GUARDRAILS]
    if identity is not None:
        brief = identity.brief()
        if brief:
            parts.append("Who you are (these facts outrank the style):\n" + brief)
    if instruction.strip():
        parts.append("What this reply must get across:\n" + instruction.strip())
    return "\n\n---\n\n".join(p for p in parts if p.strip())
