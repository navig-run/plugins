"""Filter-rule matching for incoming email. A rule matches when ALL of its
specified conditions hold (case-insensitive).

Two shapes are accepted and normalised to one:

* the daemon's flat dict — ``{from, subject_contains, subject_exact, body_words,
  channels, enabled}`` (what the Deck writes into ``~/.navig/email/config.json``);
* the mailroom's ``rules.yaml`` entry — ``{id, name, match: {…}, actions: […]}``.

Conditions: ``to``, ``from``, ``from_any``, ``subject_contains``, ``subject_exact``,
``subject_any``, ``body_words``, ``in`` (folder / label), ``has_attachment``.
Actions (``actions`` list): ``label:<Name>``, ``notify:telegram``, ``star``,
``archive``, ``mark_read``, ``run:<command>``. No model is consulted anywhere.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from .messages import FOLDER_LABEL, address, addresses

CONDITION_KEYS = (
    "to", "from", "from_any", "subject_contains", "subject_exact", "subject_any",
    "body_words", "in", "has_attachment",
    # Set by the mailroom edge Worker (X-Cybesis-Tag / X-Cybesis-Alias): the Gmail side
    # labels by the edge's own triage rather than re-deriving it from the subject.
    "tag", "tag_any", "alias", "alias_any",
)
ACTION_KINDS = ("label", "notify", "star", "archive", "mark_read", "run")


def normalize(rule: dict[str, Any]) -> dict[str, Any]:
    """Flatten a ``match:`` block into the rule and coerce list-ish fields."""
    out = dict(rule or {})
    match = out.pop("match", None)
    if isinstance(match, dict):
        for k, v in match.items():
            out.setdefault(k, v)
    for key in ("from_any", "subject_any", "body_words", "tag_any", "alias_any"):
        v = out.get(key)
        if isinstance(v, str):
            out[key] = [v]
    acts = out.get("actions")
    if isinstance(acts, str):
        out["actions"] = [acts]
    elif acts is None:
        out["actions"] = []
    return out


def needs_body(rules: list[dict[str, Any]]) -> bool:
    """True if any enabled rule inspects the body (so we fetch it)."""
    return any(r.get("enabled", True) and (r.get("body_words")) for r in rules)


def _lower(v: Any) -> str:
    return str(v or "").lower()


def match(msg: dict[str, Any], rule: dict[str, Any]) -> bool:
    rule = normalize(rule)
    if not rule.get("enabled", True):
        return False
    frm = _lower(msg.get("from"))
    to_all = " ".join(addresses(msg.get("to", "")) + addresses(msg.get("cc", ""))) or _lower(msg.get("to"))
    subject = _lower(msg.get("subject"))
    body = _lower(msg.get("body") or msg.get("snippet"))
    labels = {str(x).upper() for x in (msg.get("labels") or [])}

    conds: list[bool] = []
    if rule.get("from"):
        conds.append(_lower(rule["from"]) in frm)
    if rule.get("from_any"):
        conds.append(any(_lower(x) in frm for x in rule["from_any"] if str(x).strip()))
    if rule.get("to"):
        conds.append(_lower(rule["to"]) in to_all)
    if rule.get("subject_contains"):
        conds.append(_lower(rule["subject_contains"]) in subject)
    if rule.get("subject_exact"):
        conds.append(subject.strip() == _lower(rule["subject_exact"]).strip())
    if rule.get("subject_any"):
        conds.append(any(_lower(x) in subject for x in rule["subject_any"] if str(x).strip()))
    words = rule.get("body_words") or []
    if words:
        hay = subject + " " + body
        conds.append(any(_lower(w) in hay for w in words if str(w).strip()))
    if rule.get("in"):
        want = str(rule["in"]).strip()
        label_id = FOLDER_LABEL.get(want.lower(), want.upper())
        conds.append(label_id in labels)
    if "has_attachment" in rule and rule["has_attachment"] is not None:
        conds.append(bool(msg.get("has_attachment")) == bool(rule["has_attachment"]))
    tags = {str(t).strip().lower() for t in (msg.get("tags") or [])}
    alias = _lower(msg.get("alias"))
    if rule.get("tag"):
        conds.append(_lower(rule["tag"]) in tags)
    if rule.get("tag_any"):
        conds.append(any(_lower(t) in tags for t in rule["tag_any"] if str(t).strip()))
    if rule.get("alias"):
        conds.append(_lower(rule["alias"]) == alias)
    if rule.get("alias_any"):
        conds.append(any(_lower(a) == alias for a in rule["alias_any"] if str(a).strip()))

    # A rule with no conditions never matches (avoids notifying on everything).
    return bool(conds) and all(conds)


def first_match(msg: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rule in rules:
        if match(msg, rule):
            return rule
    return None


def all_matches(msg: dict[str, Any], rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rule for rule in rules if match(msg, rule)]


# ── Actions ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Action:
    kind: str           # label | notify | star | archive | mark_read | run
    arg: str = ""       # label name · notify target · command line

    def __str__(self) -> str:
        return f"{self.kind}:{self.arg}" if self.arg else self.kind


def parse_action(raw: str) -> Action:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty action")
    kind, _, arg = text.partition(":")
    kind = kind.strip().lower()
    arg = arg.strip()
    if kind not in ACTION_KINDS:
        raise ValueError(f"unknown action {kind!r} (expected one of {', '.join(ACTION_KINDS)})")
    if kind in ("label", "run") and not arg:
        raise ValueError(f"action {kind!r} needs an argument, e.g. {kind}:…")
    if kind == "notify" and not arg:
        arg = "telegram"
    if kind == "notify" and arg != "telegram":
        raise ValueError(f"notify target {arg!r} not supported (telegram)")
    if kind == "run" and not arg.split()[0].lower().startswith("navig"):
        # The mailroom runs navig verbs, not arbitrary programs: a rules.yaml is data.
        raise ValueError("run: actions must invoke a `navig …` command")
    return Action(kind, arg)


def actions_for(rule: dict[str, Any]) -> list[Action]:
    rule = normalize(rule)
    acts = [parse_action(a) for a in (rule.get("actions") or [])]
    # Legacy daemon rules: `channels` containing telegram means "notify".
    if not acts and any(str(c).lower() == "telegram" for c in (rule.get("channels") or [])):
        acts.append(Action("notify", "telegram"))
    return acts


def render_run(command: str, msg: dict[str, Any]) -> list[str]:
    """``run:`` command → argv, placeholders filled per-token (never through a shell)."""
    values = {
        "id": msg.get("id", ""),
        "thread_id": msg.get("thread_id", ""),
        "from": address(msg.get("from", "")),
        "subject": msg.get("subject", ""),
    }
    argv: list[str] = []
    for tok in shlex.split(command, posix=True):
        for key, val in values.items():
            tok = tok.replace("{" + key + "}", str(val))
        argv.append(tok)
    return argv


def validate(rules: list[dict[str, Any]]) -> list[str]:
    """Human-readable problems in a rule list; empty when it is sound."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(rules):
        rule = normalize(raw)
        rid = str(rule.get("id") or rule.get("name") or f"#{i + 1}")
        if rid in seen_ids:
            problems.append(f"{rid}: duplicate id")
        seen_ids.add(rid)
        if not any(rule.get(k) not in (None, "", []) for k in CONDITION_KEYS):
            problems.append(f"{rid}: no condition — would never match")
        unknown = [k for k in rule if k not in CONDITION_KEYS and k not in
                   ("id", "name", "enabled", "actions", "channels", "description")]
        if unknown:
            problems.append(f"{rid}: unknown key(s) {', '.join(unknown)}")
        try:
            if not actions_for(rule):
                problems.append(f"{rid}: no actions")
        except ValueError as exc:
            problems.append(f"{rid}: {exc}")
    return problems
