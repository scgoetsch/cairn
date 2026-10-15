"""Replay bd write commands that agents actually typed against a throwaway bd
store and a throwaway writable cairn store, then compare.

    python3 tests/replay_corpus.py CORPUS.json [--prefix workspace] [--limit N]

CORPUS.json is a list of {"argv": ["bd", "update", ...], "ts": ...}, harvested
from session transcripts (not shipped: transcripts are private). Commands are
replayed in timestamp order. Every issue id a command names is created first,
with the same id, in both stores, so the replay exercises the flag surface and
the semantics without the original store.

Skipped (counted): shell expansions ($VAR, $(...)), --help, ids under another
prefix. Reported per command: exit codes that differ. At the end: the two
stores' issues and memories compared by id (and created issues by title).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests.test_drill import FIELDS, STAMPS, Side  # noqa: E402

VERBS_WITH_IDS = {"update", "close", "reopen", "note", "comment", "comments", "defer",
                  "undefer", "dep", "delete"}


def ids_in(argv: List[str], prefix: str) -> List[str]:
    pat = re.compile(rf"^{re.escape(prefix)}-[a-z0-9]+(?:\.[0-9]+)*$")
    found = [t for t in argv if pat.match(t)]
    found += [t.split("=", 1)[1] for t in argv
              if t.startswith("--parent=") and pat.match(t.split("=", 1)[1])]
    return found


def skip_reason(argv: List[str], prefix: str) -> Optional[str]:
    text = " ".join(argv)
    if any("$" in t or "`" in t for t in argv):
        return "shell expansion or placeholder"
    if "--help" in argv or "-h" in argv:
        return "help"
    if "remember" in argv:
        return None
    other = [t for t in argv[2:] if re.match(r"^[A-Za-z]+-[a-z0-9]{3,}(\.\d+)*$", t)
             and not t.startswith(prefix + "-") and not t.startswith("-")]
    verb = next((t for t in argv[1:] if not t.startswith("-")), "")
    if verb in VERBS_WITH_IDS and other and not ids_in(argv, prefix):
        return "id under another prefix"
    return None


def state_by_id(side: Side) -> Dict[str, Any]:
    _, out, _ = side.run("export", "--all", actor=None, check=True)
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    issues = {}
    for r in rows:
        if r.get("_type", "issue") != "issue":
            continue
        obj = {f: r.get(f) for f in FIELDS}
        obj["labels"] = sorted(r.get("labels") or [])
        for f in STAMPS:
            obj[f] = bool(r.get(f))
        obj["title"] = r["title"]
        obj["deps"] = sorted((d["depends_on_id"], d["type"]) for d in r.get("dependencies") or [])
        obj["comments"] = [c["text"] for c in r.get("comments") or []]
        issues[r["id"]] = obj
    mems = {r["key"]: r["value"] for r in rows if r.get("_type") == "memory"}
    return {"issues": issues, "memories": mems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--prefix", default="workspace")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=40, help="differences to print")
    ap.add_argument("--trace", action="store_true", help="print every create with --parent/--id")
    args = ap.parse_args()
    corpus = sorted(json.loads(Path(args.corpus).read_text()), key=lambda c: c.get("ts", ""))
    if args.limit:
        corpus = corpus[:args.limit]

    skipped: Counter = Counter()
    todo: List[List[str]] = []
    for item in corpus:
        reason = skip_reason(item["argv"], args.prefix)
        if reason:
            skipped[reason] += 1
        else:
            todo.append(item["argv"])

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "bd").mkdir()
        (base / "cairn").mkdir()
        bd = Side(base / "bd", "bd", prefix=args.prefix)
        cairn = Side(base / "cairn", "cairn", prefix=args.prefix)
        seeded: set = set()
        same = 0
        differs: List[Tuple[str, int, int, str, str]] = []
        verbs: Counter = Counter()
        for argv in todo:
            for issue_id in ids_in(argv, args.prefix):
                if issue_id not in seeded:
                    for side in (bd, cairn):
                        side.run("create", "--silent", "--id", issue_id, "--title", issue_id,
                                 actor="seed")
                    seeded.add(issue_id)
            rest = argv[1:]
            verb = next((t for t in rest if not t.startswith("-")), "")
            verbs[verb] += 1
            results = [side.run(*rest, actor="agent") for side in (bd, cairn)]
            if args.trace and verb == "create" and any(t.startswith("--parent") for t in rest):
                print(f"TRACE create {' '.join(rest)[:90]} -> bd {results[0][1].strip()[:40]!r} "
                      f"cairn {results[1][1].strip()[:40]!r}")
            if args.trace and verb == "create" and any(t.startswith("--id") for t in rest):
                print(f"TRACE create-id {' '.join(rest)[:90]} -> bd {results[0][0]} cairn {results[1][0]}")
            if verb == "create":     # an id a replayed create made must not be seeded over:
                for code, out, _ in results:   # bd's create --id would REPLACE it
                    if code == 0:
                        seeded.update(ids_in(re.split(r"[\s:—]+", out), args.prefix))
            if results[0][0] == results[1][0]:
                same += 1
            else:
                differs.append((" ".join(argv)[:160], results[0][0], results[1][0],
                                (results[0][2] or results[0][1]).strip()[:160],
                                (results[1][2] or results[1][1]).strip()[:160]))
        sb, sc = state_by_id(bd), state_by_id(cairn)

    print(f"corpus {len(corpus)}; replayed {len(todo)}; skipped {dict(skipped)}")
    print(f"verbs replayed: {dict(verbs.most_common())}")
    print(f"same exit code: {same}; different: {len(differs)}")
    for cmd, cb, cc, eb, ec in differs[:args.show]:
        print(f"\n  {cmd}\n    bd={cb}: {eb}\n    cairn={cc}: {ec}")
    ib, ic = sb["issues"], sc["issues"]
    by_title = lambda d: {v["title"]: v for k, v in d.items() if k not in ic or k not in ib}  # noqa: E731
    field_diffs = []
    for k in sorted(set(ib) & set(ic)):
        for f in ib[k]:
            if ib[k][f] != ic[k][f]:
                field_diffs.append(f"{k}.{f}: bd={ib[k][f]!r:.80} cairn={ic[k][f]!r:.80}")
    only = (sorted(set(by_title(ib)) ^ set(by_title(ic))))
    print(f"\nissues: bd {len(ib)}, cairn {len(ic)}; field differences on shared ids: "
          f"{len(field_diffs)}; created-issue titles on one side only: {len(only)}")
    for line in field_diffs[:args.show]:
        print("  " + line)
    mem_diff = sorted(k for k in set(sb["memories"]) | set(sc["memories"])
                      if sb["memories"].get(k) != sc["memories"].get(k))
    print(f"memories: bd {len(sb['memories'])}, cairn {len(sc['memories'])}; differing keys: "
          f"{len(mem_diff)} {mem_diff[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
