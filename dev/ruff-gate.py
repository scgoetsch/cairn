#!/usr/bin/env python3
"""Pre-commit gate: refuse NEW correctness/security findings in staged Python, not old ones.

A finding counts as new when the staged file has more findings of that rule than its HEAD
version had, and one of them sits on a line this commit adds. That is the "introduced" test
of arXiv 2603.28592 (AI-authored commits: smells get fixed, correctness and security debt
accumulates), matched by rule rather than line number: moving or editing the line of an old
finding passes, a new one or a second copy fails. The backlog does not block.

The rules are the paper's classes plus plain execution sinks; style is not here:
broad except, undefined name, requests without a timeout, SQL built from strings,
shell=True, eval/exec, pickle, yaml.load.

Usage (from .githooks/pre-commit): dev/ruff-gate.py [PATHSPEC...]   (default: every staged *.py)
Reads the index (GIT_INDEX_FILE is honoured, so `git commit -- paths` works). Skip once:
RUFF_GATE_SKIP=1 git commit ...   No ruff on PATH: says so and passes (not checked).

The same file is in the workspace (tools/ruff-gate.py) and in cairn (dev/ruff-gate.py);
edit both. workspace-xkwd.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

RULES = "BLE001,F821,S102,S113,S301,S307,S506,S602,S604,S605,S608"


def git(*args, check=True):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def staged_python(pathspecs):
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--", *pathspecs)
    return [p for p in out.split("\0") if p.endswith(".py") or is_python_script(p)]


def is_python_script(path):
    if "." in os.path.basename(path):
        return False
    head = subprocess.run(["git", "show", f":{path}"], capture_output=True).stdout[:64]
    return head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0]


def findings(path, text):
    proc = subprocess.run(
        ["ruff", "check", "--isolated", "--select", RULES, "--output-format", "json",
         "--stdin-filename", path, "-"],
        input=text, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed on {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "[]")


def added_lines(path):
    out = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--", path)
    lines = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", out, re.M):
        start, count = int(m[1]), 1 if m[2] is None else int(m[2])
        lines.update(range(start, start + count))
    return lines


def main(argv):
    if os.environ.get("RUFF_GATE_SKIP") == "1":
        print("ruff-gate: skipped (RUFF_GATE_SKIP=1)", file=sys.stderr)
        return 0
    paths = staged_python(argv)
    if not paths:
        return 0
    if shutil.which("ruff") is None:
        print(f"ruff-gate: ruff is not installed; {len(paths)} staged Python file(s) NOT checked "
              "(pipx install ruff)", file=sys.stderr)
        return 0
    fresh = []
    for path in paths:
        now = findings(path, git("show", f":{path}"))
        if not now:
            continue
        before = git("show", f"HEAD:{path}", check=False)
        old = Counter(f["code"] for f in findings(path, before)) if before else Counter()
        excess = Counter(f["code"] for f in now)
        excess.subtract(old)
        added = added_lines(path) if before else None
        for f in sorted(now, key=lambda f: f["location"]["row"]):
            row = f["location"]["row"]
            if excess[f["code"]] > 0 and (added is None or row in added):
                excess[f["code"]] -= 1
                fresh.append(f"  {path}:{row}:{f['location']['column']}  {f['code']}  {f['message']}")
    if fresh:
        print(f"ruff-gate: {len(fresh)} new correctness/security finding(s) on lines this commit adds:",
              file=sys.stderr)
        print("\n".join(fresh), file=sys.stderr)
        print("Fix them, or mark a false positive with `# noqa: <code>` and the reason. "
              "Skip once: RUFF_GATE_SKIP=1 git commit ...", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
