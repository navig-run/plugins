"""The reply quality gate — deterministic checks on model output before it may leave DRAFT.

A small local model fails in recognisable ways: an empty string, one word ("Thanks."), the
prompt echoed back, a "[Your Name]" placeholder, a reply in English to a French letter, the
signature of a *different* identity, a price or a date it invented. None of that needs another
model to catch; every check here is a plain function of the reply, the thread, and the
identity facts, so it is fast, free, and testable.

Severity:

* ``hard`` — the text is unusable. A draft is not saved; an automatic send is impossible.
* ``soft`` — a human should look (an unexplained number, a commitment). A person approving it
  may proceed; an **automatic** send may not (:func:`QualityReport.auto_ok`).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_URL = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
# Numbers that carry meaning in a reply: prices, quantities, dates, times. Bare list
# numbering ("1.") and years in a signature are noise, so very short tokens are skipped.
_NUMBER = re.compile(r"(?<![\w.])\d[\d\s.,:/-]*\d(?![\w])|(?<![\w.])\d{2,}(?![\w])")
_MONEY = re.compile(r"(?:[€$£₽]\s?\d|\d\s?(?:€|eur\b|euros?\b|usd\b|\$|£|₽|руб))", re.IGNORECASE)

_PLACEHOLDERS = re.compile(
    r"\[(?:your|my|recipient|name|company|insert|date|nom|votre|ваше|имя)[^\]]{0,40}\]"
    r"|<(?:your|insert)[^>]{0,40}>|\{\{?[a-z_ ]{2,30}\}?\}",
    re.IGNORECASE,
)
_HEADER_LEAK = re.compile(r"^\s*(?:subject|objet|тема|to|from|de|à)\s*:", re.IGNORECASE | re.MULTILINE)
_META_LEAK = re.compile(
    r"\b(?:as an ai|as a language model|i am an ai|en tant qu'ia|en tant que modèle|"
    r"как языковая модель|как ии|here is (?:a|the|your) (?:reply|response|draft)|"
    r"voici (?:une|la|votre) (?:réponse|proposition)|вот (?:ответ|вариант))\b",
    re.IGNORECASE,
)

#: Promises a model must not make on its own (EN / FR / RU). Matching is deliberately broad:
#: a false positive costs a human glance, a miss costs a commitment nobody agreed to.
_COMMITMENTS = re.compile(
    r"\b(?:we will|i will|we'll|i'll|we guarantee|i guarantee|guaranteed|we promise|"
    r"by (?:monday|tuesday|wednesday|thursday|friday|tomorrow|end of (?:day|week))|"
    r"within \d+ (?:hours?|days?|weeks?)|refund|discount|free of charge|"
    r"nous allons|je vais|nous garantissons|garanti|nous vous rembourserons|remboursement|"
    r"d'ici (?:lundi|mardi|mercredi|jeudi|vendredi|demain)|sous \d+ (?:heures?|jours?)|"
    r"мы гарантируем|гарантирую|мы вернём|возврат|до (?:понедельника|завтра)|в течение \d+)",
    re.IGNORECASE,
)

_STOP = {
    "en": {"the", "and", "you", "for", "with", "this", "that", "your", "are", "have", "will",
           "thanks", "thank", "please", "would", "could", "about", "our", "we", "is", "it", "to"},
    "fr": {"le", "la", "les", "et", "vous", "pour", "avec", "ce", "cette", "votre", "nous",
           "est", "sont", "merci", "bonjour", "une", "des", "du", "dans", "pas", "je", "que", "qui"},
    "ru": {"и", "в", "не", "на", "что", "я", "вы", "с", "по", "это", "как", "для", "мы",
           "спасибо", "здравствуйте", "ваш", "вам", "но", "так", "уже", "если", "или"},
}
_LANG_NAMES = {
    "en": ("en", "english", "anglais", "английский"),
    "fr": ("fr", "french", "français", "francais", "французский"),
    "ru": ("ru", "russian", "russe", "русский"),
}


def detect_language(text: str) -> str:
    """``en`` / ``fr`` / ``ru`` when the text says so clearly, else ``""`` (unknown).

    Cyrillic share decides Russian; stopword hits decide between English and French. A short
    or mixed text answers ``""`` — the gate never fails a reply on a guess.
    """
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < 4:
        return ""
    cyr = sum(1 for w in words if re.search(r"[а-яё]", w))
    if cyr / len(words) >= 0.5:
        return "ru"
    if cyr / len(words) > 0.15:
        return ""
    hits = {lang: sum(1 for w in words if w in stops) for lang, stops in _STOP.items() if lang != "ru"}
    best = max(hits, key=hits.get)
    other = min(hits, key=hits.get)
    if hits[best] >= 2 and hits[best] >= 2 * max(hits[other], 1):
        return best
    return ""


def language_code(name: str) -> str:
    n = (name or "").strip().lower()
    for code, names in _LANG_NAMES.items():
        if n in names:
            return code
    return ""


@dataclass
class Issue:
    code: str
    severity: str  # "hard" | "soft"
    message: str


@dataclass
class QualityReport:
    issues: list[Issue] = field(default_factory=list)
    language: str = ""
    expected_language: str = ""

    @property
    def ok(self) -> bool:
        """No hard failure — fit to save as a draft or show a person for approval."""
        return not any(i.severity == "hard" for i in self.issues)

    @property
    def auto_ok(self) -> bool:
        """No issue at all — the only state in which a policy may send without a person."""
        return not self.issues

    def codes(self) -> list[str]:
        return [i.code for i in self.issues]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "auto_ok": self.auto_ok,
            "language": self.language,
            "expected_language": self.expected_language,
            "issues": [asdict(i) for i in self.issues],
        }

    def summary(self) -> str:
        return "; ".join(f"{i.code}: {i.message}" for i in self.issues) or "ok"


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s.,:/-]", "", n) for n in _NUMBER.findall(text or "") if len(re.sub(r"\D", "", n)) >= 2}


def _urls(text: str) -> set[str]:
    return {u.rstrip(".,;").lower() for u in _URL.findall(text or "")}


def check(
    text: str,
    *,
    thread: str = "",
    identity: Any = None,
    other_identities: list[Any] | None = None,
    expected_language: str = "",
) -> QualityReport:
    """Every check, in one report. *identity* / *other_identities* are ``voice.Identity``s."""
    body = (text or "").strip()
    report = QualityReport()

    if not body:
        report.issues.append(Issue("empty", "hard", "the reply is empty"))
        return report
    words = _WORD.findall(body)
    if len(words) <= 2:
        report.issues.append(Issue("too_short", "hard", f"the reply is only {len(words)} word(s)"))

    if _PLACEHOLDERS.search(body):
        report.issues.append(Issue("placeholder", "hard", "contains an unfilled placeholder"))
    if _HEADER_LEAK.search(body.splitlines()[0] if body.splitlines() else ""):
        report.issues.append(Issue("header_leak", "hard", "starts with an email header line"))
    if _META_LEAK.search(body):
        report.issues.append(Issue("meta_leak", "hard", "talks about itself as a model or a draft"))

    # Language: the identity's language wins; else the thread's. Only a CONFIDENT mismatch fails.
    want = expected_language or language_code(getattr(identity, "language", "") or "") or detect_language(thread)
    got = detect_language(body)
    report.language, report.expected_language = got, want
    if want and got and want != got:
        report.issues.append(Issue("language", "hard", f"written in {got}, expected {want}"))

    # Identity consistency: another identity's company or signature must not appear.
    if identity is not None:
        own = {s.lower() for s in (getattr(identity, "company", ""), getattr(identity, "signature", "")) if s}
        for other in other_identities or []:
            if getattr(other, "name", None) == getattr(identity, "name", None):
                continue
            for marker in (getattr(other, "company", ""), getattr(other, "signature", "")):
                m = (marker or "").strip()
                if len(m) >= 4 and m.lower() not in own and m.lower() in body.lower():
                    report.issues.append(
                        Issue("foreign_identity", "hard", f"mentions another identity ({other.name})")
                    )
                    break

    # Grounding: every meaningful number / URL must come from the thread or the identity.
    known = " ".join([thread or ""] + [str(getattr(identity, f, "") or "") for f in
                                       ("context", "signature", "company", "display")])
    stray_numbers = _numbers(body) - _numbers(known)
    if stray_numbers:
        report.issues.append(
            Issue("ungrounded_number", "soft", "numbers not found in the thread: " + ", ".join(sorted(stray_numbers)[:5]))
        )
    stray_urls = _urls(body) - _urls(known)
    if stray_urls:
        report.issues.append(Issue("ungrounded_url", "soft", "links not found in the thread"))
    if _MONEY.search(body) and not _MONEY.search(known):
        report.issues.append(Issue("money", "soft", "mentions an amount of money"))
    if _COMMITMENTS.search(body):
        report.issues.append(Issue("commitment", "soft", "makes a promise or a deadline"))
    return report
