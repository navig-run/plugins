"""The periodic digest — a template over the stats, with prose only on request.

The default digest is exactly the counts, the rule hits and the top senders, laid out
for a phone screen. ``--llm`` adds a short prose brief over the period's inbox
subjects, through ``llm_guard`` (opt-in, provider printed, cloud refused unless
allowed). The digest is never the trigger for anything; it is a report.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import llm_guard, messages, stats as ST

SUBJECT_CAP = 40


def render_markdown(
    st: dict[str, Any], *, account: str = "", prose: str = "", provider: str = ""
) -> str:
    p = st["period"]
    c = st["counts"]
    lines = [
        f"# Digest mail — {p['label']}",
        "",
        f"Compte `{account or '?'}` · généré le {date.today().isoformat()}",
        "",
        f"- Reçus **{c.get('received', 0)}** (réception {c.get('inbox', 0)}, non lus {c.get('unread', 0)})",
        f"- Envoyés **{c.get('sent', 0)}** · spam {c.get('spam', 0)} · pièces jointes {c.get('attachments', 0)}",
    ]
    if st.get("rules") or st.get("labels"):
        hits = {**st.get("rules", {}), **st.get("labels", {})}
        lines.append("- Règles : " + " · ".join(f"{k} {v}" for k, v in hits.items()))
    if st.get("top_senders"):
        lines += ["", "## Expéditeurs", ""]
        lines += [f"- {addr} — {n}" for addr, n in st["top_senders"][:8]]
    if prose:
        lines += ["", f"## Résumé ({provider})", "", prose.strip()]
    lines.append("")
    return "\n".join(lines)


def telegram_text(
    st: dict[str, Any], *, prose: str = "", title: str = "Digest mail"
) -> str:
    from navig_email._compat import escape_html as esc

    text = ST.telegram_text(st, title=title)
    if prose:
        text += "\n\n" + esc(prose.strip()[:1500])
    return text


async def prose_summary(
    connector,
    period: ST.Period,
    *,
    model: str | None = None,
    allow_cloud: bool = False,
    focus: str = "",
) -> tuple[str, str]:
    """``(prose, provider)`` over the period's inbox subjects — opt-in only."""
    ids = [
        e["id"]
        for e in await connector.iter_message_ids(
            f"in:inbox {period.query()}", limit=SUBJECT_CAP
        )
    ]
    raw = await connector.get_many(ids, format="metadata", headers=["From", "Subject"])
    lines = []
    for m in raw:
        s = messages.shape(m)
        lines.append(
            f"- {messages.display_name(s['from'])[:40]}: {s['subject'][:100]} — {s['snippet'][:120]}"
        )
    if not lines:
        return "", ""
    system = (
        "Tu es un assistant de tri de courrier. À partir de la liste, écris un résumé de 3 à 6 "
        "puces en français : ce qui est IMPORTANT (réponse ou échéance attendue) et ce qui est "
        "notable. Regroupe les sujets similaires. Commence chaque ligne par '- '. Pas de préambule."
    )
    if focus:
        system += f" Priorité : {focus}."
    text, resolved = llm_guard.generate(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)[:6000]},
        ],
        mode="summarize",
        model=model,
        allow_cloud=allow_cloud,
        max_tokens=400,
    )
    return text, str(resolved)
