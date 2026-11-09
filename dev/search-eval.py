#!/usr/bin/env python3
"""Measure `cairn search` on a real store, with the store's own links as ground truth.

  issues    query = an issue's title and description; relevant = the earlier issues it
            depends on (blocks, parent, discovered-from, related); results created after
            the query issue are dropped, as they did not exist when it was written.
  memories  query = the text of an issue that names a memory key, with every key removed;
            relevant = the keys it names.

Baselines: newest-first (what `cairn list` shows) for issues; the hot tier (what prime
injects in full) and `memories KEYWORD` substring matching for memories. Prints R@5,
R@10 and MRR for each, and for several column weightings.

    python3 dev/search-eval.py /path/to/checkout     # reads its .cairn/ exports

Written for workspace-d3ct (2026-11-09). The ground truth was written by the same people
who wrote the text, which favours lexical matching; read the numbers as relative.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairn import search as searchmod
from cairn.importer import build_store
from cairn.store import db_path, open_db


def metrics(results):
    n = len(results)
    r5 = sum(len(set(r[:5]) & g) / len(g) for r, g in results) / n
    r10 = sum(len(set(r[:10]) & g) / len(g) for r, g in results) / n
    mrr = sum(next((1 / (i + 1) for i, x in enumerate(r) if x in g), 0) for r, g in results) / n
    return f"R@5={r5:.2f}  R@10={r10:.2f}  MRR={mrr:.2f}"


def main(checkout: Path) -> None:
    store = checkout / ".cairn"
    if not (store / "issues.jsonl").exists():
        store = checkout / ".beads"
    issues = [json.loads(line) for line in (store / "issues.jsonl").read_text().splitlines()]
    mems = [json.loads(line) for line in (store / "memories.jsonl").read_text().splitlines()]
    byid = {i["id"]: i for i in issues}
    with tempfile.TemporaryDirectory() as tmp:
        export = Path(tmp) / "export.jsonl"
        export.write_text("".join(json.dumps(r) + "\n" for r in issues + mems))
        build_store(db_path(Path(tmp) / ".cairn"), export)
        conn = open_db(db_path(Path(tmp) / ".cairn"))

        def text(i):
            return f"{i['title']}\n{i.get('description') or ''}"

        cases = []
        for i in issues:
            g = {d["depends_on_id"] for d in i.get("dependencies") or []
                 if d["depends_on_id"] in byid and byid[d["depends_on_id"]]["created_at"] < i["created_at"]}
            if g:
                cases.append((i, g))
        print(f"issues: {len(cases)} queries")
        newest = [(sorted([p["id"] for p in issues if p["created_at"] < i["created_at"]],
                          key=lambda x: byid[x]["created_at"], reverse=True), g) for i, g in cases]
        print(f"  {'newest first':28} {metrics(newest)}")
        for label, w in (("weights title3 body1 notes.5", {"title": 3.0, "body": 1.0, "notes": 0.5}),
                         ("weights 1 1 1", {"title": 1.0, "body": 1.0, "notes": 1.0}),
                         ("weights 1 1 0 (no notes)", {"title": 1.0, "body": 1.0, "notes": 0.0}),
                         ("weights 3 1 0", {"title": 3.0, "body": 1.0, "notes": 0.0})):
            searchmod.WEIGHTS.update(w)
            res = []
            for i, g in cases:
                rows = searchmod.search(conn, text(i), kinds=("issue",), limit=0)
                res.append(([r["id"] for r in rows if r["id"] != i["id"]
                             and byid[r["id"]]["created_at"] < i["created_at"]], g))
            print(f"  {label:28} {metrics(res)}")

        keys = [m["key"] for m in mems]
        hot = [r["key"] for r in conn.execute("SELECT key FROM memories WHERE hot = 1")]
        mcases = []
        for i in issues:
            g = {k for k in keys if k in json.dumps(i)}
            if g:
                q = text(i)
                for k in sorted(keys, key=len, reverse=True):
                    q = q.replace(k, " ")
                mcases.append((q, g))
        print(f"memories: {len(mcases)} queries over {len(keys)} memories")
        if hot:
            print(f"  {'hot tier (static)':28} {metrics([(hot, g) for _, g in mcases])}")
        sub = []
        for q, g in mcases:
            score = {}
            for w in searchmod.query_terms(q):
                for m in mems:
                    if w in m["key"].lower() or w in m["value"].lower():
                        score[m["key"]] = score.get(m["key"], 0) + 1
            sub.append((sorted(score, key=lambda k: -score[k]), g))
        print(f"  {'substring, every word':28} {metrics(sub)}")
        res = [([r["id"] for r in searchmod.search(conn, q, kinds=("memory",), limit=0)], g)
               for q, g in mcases]
        print(f"  {'cairn search --memories':28} {metrics(res)}")
        conn.close()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
