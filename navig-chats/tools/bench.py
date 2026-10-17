"""Benchmark the chat library at realistic sizes.

    py -3.13 plugins/navig-chats/tools/bench.py --sizes 1000 10000 50000 --out .dev/reports/chats-bench.md

Builds a synthetic ChatGPT-shaped export per size (``--messages`` turns per chat, long
assistant answers with code), imports it into a throwaway database, then times searches.
Reports import throughput, database size and p50/p95 search latency. Nothing touches the
real library: every run uses its own temporary directory.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REPO = _ROOT.parents[1]
for p in (_REPO / "registry" / "sdk" / "python", _ROOT):
    sys.path.insert(0, str(p))

from navig_chats.chatgpt_export import import_export  # noqa: E402
from navig_chats.store import ChatStore  # noqa: E402

WORDS = (
    "deploy worker cache index query latency token stream export folder branch search rust python "
    "sqlite postgres kubernetes design decision budget review schema migration render layout prompt "
    "agent memory vector embedding summary meeting roadmap invoice customer feature bug crash log"
).split()
SYLL = "ka lo mi ne su ra to vi pe da lu ko si ma te no ri ba ze fu".split()
QUERIES = ["deploy", "sqlite migration", '"design decision"', "rust -python", "embed*", "kubernetes latency",
           "invoice OR budget", "zebra", "schema review", "stream"]


class _Vocab:
    """Zipf-distributed filler words (like real prose) plus a few topic words per message."""

    def __init__(self, rng: random.Random, size: int = 20000) -> None:
        self.words = list({"".join(rng.choice(SYLL) for _ in range(rng.randint(2, 4))) for _ in range(size * 2)})[:size]
        self.cum = list(__import__("itertools").accumulate(1 / (r + 1) ** 1.07 for r in range(len(self.words))))
        self.rng = rng

    def para(self, n: int) -> str:
        words = self.rng.choices(self.words, cum_weights=self.cum, k=n)
        for _ in range(3):
            words[self.rng.randrange(n)] = self.rng.choice(WORDS)
        return " ".join(words).capitalize() + "."


_VOCAB: _Vocab | None = None


def _para(rng: random.Random, n: int) -> str:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _Vocab(random.Random(1))
    return _VOCAB.para(n)


def build_export(path: Path, n: int, turns: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    t0 = 1_690_000_000.0
    with path.open("w", encoding="utf-8") as fh:
        fh.write("[")
        for c in range(n):
            cid = f"bench-{c:06d}"
            mapping = {"root": {"id": "root", "message": None, "parent": None, "children": []}}
            parent = "root"
            for i in range(turns):
                role = "user" if i % 2 == 0 else "assistant"
                text = _para(rng, 25) if role == "user" else "\n\n".join(_para(rng, 60) for _ in range(4))
                if role == "assistant" and i % 6 == 1:
                    text += "\n\n```python\n" + "\n".join(f"x{j} = {rng.randint(0, 99)}" for j in range(12)) + "\n```"
                nid = f"{cid}-{i}"
                mapping[nid] = {"id": nid, "parent": parent, "children": [], "message": {
                    "id": nid, "author": {"role": role}, "create_time": t0 + c * 60 + i,
                    "content": {"content_type": "text", "parts": [text]}, "metadata": {}}}
                mapping[parent]["children"].append(nid)
                parent = nid
            conv = {"id": cid, "conversation_id": cid, "title": _para(rng, 4)[:-1], "create_time": t0 + c * 60,
                    "update_time": t0 + c * 60 + turns, "mapping": mapping, "current_node": parent}
            if c:
                fh.write(",")
            json.dump(conv, fh)
        fh.write("]")


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def run(size: int, turns: int, rounds: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="chats-bench-") as tmp:
        tmpd = Path(tmp)
        src = tmpd / "conversations.json"
        t = time.perf_counter()
        build_export(src, size, turns)
        gen_s = time.perf_counter() - t
        store = ChatStore(tmpd / "chats.db")
        try:
            t = time.perf_counter()
            res = import_export(store, src, batch=500)
            import_s = time.perf_counter() - t
            store.maintenance()
            lat: list[float] = []
            per_query: dict[str, float] = {}
            for _ in range(rounds):
                for q in QUERIES:
                    t = time.perf_counter()
                    store.search(q, limit=30)
                    ms = (time.perf_counter() - t) * 1000
                    lat.append(ms)
                    per_query[q] = max(per_query.get(q, 0), ms)
            worst_q = _VOCAB.words[0] if _VOCAB else "the"
            t = time.perf_counter()
            worst = store.search(worst_q, limit=30)
            worst_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            store.list(sort="newest", limit=100)
            list_ms = (time.perf_counter() - t) * 1000
            msgs = store.stats()["messages"]
        finally:
            store.close()
        db_mb = sum(f.stat().st_size for f in tmpd.glob("chats.db*")) / 1e6
        return {
            "size": size, "messages": msgs, "src_mb": round(src.stat().st_size / 1e6, 1), "gen_s": round(gen_s, 1),
            "import_s": round(import_s, 1), "conv_per_s": round(res["done"] / import_s), "db_mb": round(db_mb, 1),
            "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(pct(lat, 0.95), 1),
            "max_ms": round(max(lat), 1), "list_ms": round(list_ms, 1),
            "slowest": max(per_query, key=per_query.get),
            "worst_ms": round(worst_ms, 1), "worst_ranking": worst["ranking"],
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    ap.add_argument("--messages", type=int, default=30, help="turns per chat")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    rows = []
    for n in args.sizes:
        r = run(n, args.messages, args.rounds)
        rows.append(r)
        print(json.dumps(r), flush=True)
    lines = [
        "| chats | messages | export MB | import s | chats/s | db MB | search p50 ms | p95 ms | max ms | list ms | slowest query | most-common-word ms (ranking) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        *(f"| {r['size']} | {r['messages']} | {r['src_mb']} | {r['import_s']} | {r['conv_per_s']} | {r['db_mb']} | "
          f"{r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} | {r['list_ms']} | `{r['slowest']}` | {r['worst_ms']} ({r['worst_ranking']}) |" for r in rows),
    ]
    print("\n".join(lines))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("# navig-chats benchmark\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
