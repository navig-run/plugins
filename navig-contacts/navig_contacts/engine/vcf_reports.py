"""
Reports for the vCard import.

Every judgement the importer makes gets written down somewhere a human can
argue with it: what was merged and on which key, which numbers were thrown
away and why, which regions had to be guessed, which merges look like they
might have fused two different people, and everything that was dropped.

Reports go to ``exports/`` with the run's date in the filename, so successive
imports never silently overwrite each other's evidence.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .identity import looks_like_handle, name_key

WIDTH = 76


def _stamp() -> str:
    """The LOCAL calendar day, because this names the file a human goes looking for.

    A UTC day is a different day for several hours each night: run the import at 21:00
    in UTC+3 and every report is stamped tomorrow, so "today's export" is not in the
    listing. ``_generated`` below is a different question -- an instant, correctly UTC,
    and it says so in the string.
    """
    return date.today().strftime("%Y-%m-%d")


def _generated() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _csv(header: list[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Suspicious merges
# ---------------------------------------------------------------------------

def _real_name_groups(cluster) -> list[set[str]]:
    """
    Group a cluster's human-looking aliases by shared tokens.

    'Adnot Florian' and 'Florian Adnot' share both tokens and form one group.
    'Fred' and 'Marie Dupont' share nothing and form two — which is the shape a
    shared landline or a mis-keyed number leaves behind.
    """
    groups: list[set[str]] = []
    for name, _key, _src in cluster.aliases():
        if looks_like_handle(name):
            continue
        tokens = {t for t in name_key(name).split() if len(t) > 2}
        if len(tokens) < 2:
            continue  # a lone first name proves nothing either way
        merged = tokens
        rest: list[set[str]] = []
        for existing in groups:
            if existing & merged:
                merged = merged | existing
            else:
                rest.append(existing)
        rest.append(merged)
        groups = rest
    return groups


def suspicious_clusters(clusters, reviewed: Optional[set] = None
                        ) -> list[tuple[object, list[set[str]]]]:
    """
    Clusters holding two or more unrelated human names — worth a human look.

    Anything already judged is left out: a report that reprints the same
    verdicts on every import is one nobody reads, and a genuinely new
    suspicious merge would arrive buried in them.
    """
    reviewed = reviewed or set()
    out = []
    for cluster in clusters:
        if cluster.alias() in reviewed:
            continue
        groups = _real_name_groups(cluster)
        if len(groups) >= 2:
            out.append((cluster, groups))
    out.sort(key=lambda item: (-len(item[1]), -len(item[0].cards)))
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_verified(clusters) -> str:
    """Readable roster of the phone-verified contacts — the actual deliverable."""
    rows = [c for c in clusters if c.tier() == "verified"]
    rows.sort(key=lambda c: (c.display_name() or "").lower())

    out: list[str] = []
    out.append("=" * WIDTH)
    out.append("VERIFIED CONTACTS — imported from the vCard archive")
    out.append("=" * WIDTH)
    out.append(f"Generated : {_generated()}")
    out.append(f"Contacts  : {len(rows)}")
    out.append(f"Cards     : {sum(len(c.cards) for c in rows)} merged into the above")
    out.append("")
    out.append("Every contact below has at least one phone number that")
    out.append("libphonenumber confirms is valid. That is what 'verified' means")
    out.append("here — not that anyone has checked the person still answers it.")
    out.append("=" * WIDTH)
    out.append("")

    for i, c in enumerate(rows, 1):
        aliases = [a[0] for a in c.aliases()]
        primary = c.display_name() or "—"
        others = [a for a in aliases if a != primary]
        meta = " · ".join(x for x in (c.city() or "", c.country() or "") if x)
        out.append(f"{i:>4}. {primary}" + (f"   ({meta})" if meta else ""))
        for phone, raw in c.phones():
            suffix = "" if raw.replace(" ", "") == phone else f"   [as written: {raw}]"
            out.append(f"      phone  : {phone}{suffix}")
        for email, _raw in c.emails():
            out.append(f"      email  : {email}")
        for kind, norm, _raw in c.handles():
            out.append(f"      {kind:<7}: {norm}")
        if c.org():
            out.append(f"      org    : {c.org()}"
                       + (f" — {c.title()}" if c.title() else ""))
        if c.birthday():
            out.append(f"      born   : {c.birthday()}")
        if others:
            out.append(f"      also   : {', '.join(others)}")
        out.append(f"      source : {len(c.cards)} card(s) in "
                   f"{', '.join(c.source_files())}")
        out.append("")

    out.append("=" * WIDTH)
    out.append("PHONE LIST")
    out.append("=" * WIDTH)
    for c in rows:
        for phone, _raw in c.phones():
            out.append(f"{phone}\t{c.display_name()}")
    out.append("")
    return "\n".join(out)


def render_merges(clusters) -> str:
    """Every cluster built from more than one card, and the key that joined it."""
    merged = [c for c in clusters if len(c.cards) > 1]
    merged.sort(key=lambda c: -len(c.cards))

    out: list[str] = []
    out.append("# Merges — one person, many cards\n")
    out.append(f"Generated: {_generated()}\n")
    out.append(
        f"{len(merged)} people were assembled from more than one card. "
        f"Merging is on hard identifiers only — phone, email, skype, telegram, "
        f"facebook, vk. Names never merge anything: on this archive, name "
        f"matching fuses unrelated people.\n"
    )
    out.append("| Cards | Contact | Merged on | Also known as | Files |")
    out.append("|------:|---------|-----------|---------------|-------|")
    for c in merged:
        keys: list[str] = []
        keys += [p for p, _ in c.phones()]
        keys += [e for e, _ in c.emails()]
        keys += [f"{k}:{v}" for k, v, _ in c.handles()]
        primary = c.display_name() or "—"
        others = [a[0] for a in c.aliases() if a[0] != primary]
        out.append(
            f"| {len(c.cards)} | {_md(primary)} | {_md(', '.join(keys[:4]))} "
            f"| {_md(', '.join(others))} | {_md(', '.join(c.source_files()))} |"
        )
    out.append("")
    return "\n".join(out)


def render_suspicious(clusters, reviewed: Optional[set] = None) -> str:
    """The merges most likely to be wrong — a shared line, or a mis-typed number."""
    reviewed = reviewed or set()
    flagged = suspicious_clusters(clusters, reviewed)

    out: list[str] = []
    out.append("# Review — merges that may have fused two different people\n")
    out.append(f"Generated: {_generated()}\n")
    out.append(
        "Each entry below is one contact whose card set carries two or more "
        "human names with no words in common. Usually that is a nickname or a "
        "maiden name. Sometimes it is a landline shared by a couple, an office "
        "number on several people's cards, or a number typed onto the wrong "
        "contact years ago.\n"
    )
    out.append(
        "Undo any of them with:\n\n"
        "```\npython cli.py merge --list --uid <uid>\n"
        "python cli.py merge --undo <merge_id>\n```\n"
    )
    if not flagged:
        out.append("Nothing flagged.\n")
        return "\n".join(out)

    out.append(f"**{len(flagged)} to review.**\n")
    for cluster, groups in flagged:
        out.append(f"### {cluster.display_name() or '—'}  ·  `{cluster.alias()}`")
        out.append("")
        out.append(f"- shared identifiers: "
                   f"{', '.join(p for p, _ in cluster.phones()) or '—'}")
        out.append(f"- distinct name groups: {len(groups)}")
        for name, _key, source in cluster.aliases():
            out.append(f"  - {name}  *(from {source})*")
        out.append(f"- {len(cluster.cards)} cards from "
                   f"{', '.join(cluster.source_files())}")
        out.append("")
    return "\n".join(out)


def render_rejected(rejected) -> str:
    """Every TEL value that did not survive validation, with the reason."""
    seen: dict[str, object] = {}
    counts: dict[str, int] = {}
    for r in rejected:
        counts[r.raw] = counts.get(r.raw, 0) + 1
        seen.setdefault(r.raw, r)
    rows = [
        [r.raw, counts[raw], r.reason, r.name, r.source_file, r.card_index]
        for raw, r in sorted(seen.items(), key=lambda kv: kv[1].reason)
    ]
    return _csv(
        ["raw_value", "occurrences", "reason", "on_contact", "source_file",
         "card_index"],
        rows,
    )


def render_assumed(assumed) -> str:
    """Numbers that only validated because a region was inferred."""
    seen: dict[str, object] = {}
    for a in assumed:
        seen.setdefault(a.raw, a)
    rows = [
        [a.raw, a.e164, a.reason, a.name, a.source_file]
        for _raw, a in sorted(seen.items(), key=lambda kv: kv[1].reason)
    ]
    return _csv(
        ["raw_value", "interpreted_as", "assumption", "on_contact", "source_file"],
        rows,
    )


def render_interests(split) -> str:
    """The non-people pulled out of the 2013 follow list."""
    out: list[str] = []
    out.append("# Interests from the vCard archive\n")
    out.append(f"Generated: {_generated()}\n")
    out.append(
        "The archive's bottom tier is a 2013 Google+ follow list: a name and a "
        "dead profile URL, no phone, no email, no handle. None of it is in the "
        "contacts database. What follows is what that list says you were "
        "interested in, split from the organisations and from the people.\n"
    )

    def table(title: str, entries, note: str) -> None:
        out.append(f"## {title} ({len(entries)})\n")
        out.append(note + "\n")
        if not entries:
            out.append("_None._\n")
            return
        out.append("| Name | Cards | Why it was classified this way |")
        out.append("|------|------:|--------------------------------|")
        for e in entries:
            out.append(f"| {_md(e.name)} | {e.cards} | {_md(e.reason)} |")
        out.append("")

    table("Interests", split.interests,
          "Topics, products, channels and media the operator followed.")
    table("Organisations and services", split.organisations,
          "Companies and public services — a bank, an employment office, a "
          "phone operator. Kept apart from interests because they are places "
          "you call, not things you follow.")
    table("Unclear — needs a human", split.unclear,
          "A single capitalised word with no surname. `Mark` and `Scott` are "
          "probably people; `Chronopost` and `Repondeur` probably are not. "
          "The data cannot settle it.")

    out.append("## Dropped\n")
    out.append(
        f"- **{len(split.people)}** named people with no phone, no email and no "
        f"handle — nothing to reach them by.\n"
        f"- **{len(split.handles)}** bare lowercase account handles.\n"
        f"- **{split.empty_cards}** cards carrying no name at all.\n\n"
        "All of them are listed in the dropped CSV beside this file, and the "
        "source `.vcf` files are untouched, so any of this can be re-imported "
        "later.\n"
    )
    return "\n".join(out)


def render_dropped(split) -> str:
    """Everything the import discarded, so nothing disappears unrecorded."""
    rows = []
    for bucket, label in (
        (split.people, "person-no-contact-method"),
        (split.handles, "bare-handle"),
        (split.unclear, "unclear"),
        (split.interests, "interest"),
        (split.organisations, "organisation"),
    ):
        for e in bucket:
            rows.append([
                e.name, label, e.reason, e.cards,
                "; ".join(sorted(e.source_files)), "; ".join(e.urls),
            ])
    rows.append([
        "(cards with no name)", "empty", "card carries no name at all",
        split.empty_cards, "", "",
    ])
    return _csv(
        ["name", "bucket", "reason", "cards", "source_files", "urls"], rows
    )


def _md(text: Optional[str]) -> str:
    """Escape a value for a Markdown table cell."""
    return (text or "—").replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_all(exports_dir: Path, clusters, rejected, assumed, split,
              reviewed: Optional[set] = None) -> dict[str, Path]:
    """Write every report and return {label: path}."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    prefix = f"vcf-import-{stamp}"

    files = {
        "verified":  (exports_dir / f"{prefix}-verified.txt",
                      render_verified(clusters)),
        "merges":    (exports_dir / f"{prefix}-merges.md",
                      render_merges(clusters)),
        "review":    (exports_dir / f"{prefix}-review-suspicious.md",
                      render_suspicious([c for c in clusters
                                         if c.tier() != "archive"], reviewed)),
        "rejected":  (exports_dir / f"{prefix}-rejected-phones.csv",
                      render_rejected(rejected)),
        "assumed":   (exports_dir / f"{prefix}-assumed-regions.csv",
                      render_assumed(assumed)),
        "interests": (exports_dir / f"vcf-interests-{stamp}.md",
                      render_interests(split)),
        "dropped":   (exports_dir / f"{prefix}-dropped.csv",
                      render_dropped(split)),
    }

    written: dict[str, Path] = {}
    for label, (path, content) in files.items():
        path.write_text(content, encoding="utf-8")
        written[label] = path
    return written
