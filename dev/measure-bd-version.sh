#!/usr/bin/env bash
# measure-bd-version.sh — does `cairn history-from-dolt` read the history a given bd writes?
#
#   dev/measure-bd-version.sh BD [--dolt DOLT] [--keep DIR]
#
# BD is the bd binary to measure. In a scratch directory with its own HOME (nothing global is
# touched), it grows a small store through that bd with every kind of change the reader carries:
# three issues (one a child), description / notes (twice) / priority / status changes, a label,
# a dependency, a comment, a close, a deleted issue, a memory set twice and a memory set then
# forgotten. Then `bd export --all`, `cairn history-from-dolt` against that export, and
# dev/verify-dolt-history.py. Prints one JSON object: the bd version, where the memories were
# (config kv.memory.* or elsewhere), the event counts by kind, and each expectation that held or
# failed. Exit 0 when every expectation held, 1 when one failed, 2 when a step could not run.
set -u
BD=${1:?usage: measure-bd-version.sh BD [--dolt DOLT] [--keep DIR]}; shift
DOLT=dolt KEEP=
while [ $# -gt 0 ]; do case $1 in --dolt) DOLT=$2; shift 2 ;; --keep) KEEP=$2; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;; esac; done
HERE=$(cd "$(dirname "$0")/.." && pwd -P)
BD=$(readlink -f "$BD") || exit 2
W=${KEEP:-$(mktemp -d)}; mkdir -p "$W/home" "$W/repo"
[ -z "$KEEP" ] && trap 'rm -rf "$W"' EXIT
export HOME=$W/home DOLT_ROOT_PATH=$W/home BEADS_ACTOR=measure TZ=UTC GIT_CONFIG_NOSYSTEM=1
unset BEADS_DIR BD_ACTOR
die() { echo "measure-bd-version: $*" >&2; exit 2; }
cd "$W/repo" || die "no scratch dir"
git init -q -b main && git config user.name measure && git config user.email measure@example.invalid \
    || die "git init"
run() { "$BD" "$@" >>"$W/bd.log" 2>&1 || die "bd $* failed (see $W/bd.log, kept with --keep)"; }
make() { "$BD" create --silent "$@" 2>>"$W/bd.log" || die "bd create $* failed"; }
"$BD" init -p m --non-interactive >"$W/init.log" 2>&1 || die "bd init failed: $(tail -n 3 "$W/init.log")"
a=$(make --title "alpha" -p 2)
b=$(make --title "beta" -p 2)
c=$(make --title "gamma" -p 3 --parent "$a")
run update "$a" --description "first description"
run update "$a" --notes "note one"
run update "$a" --notes "note one
note two"
run update "$a" --priority 1
run update "$a" --status in_progress
run label add "$a" urgent
run dep add "$b" "$a"
run comments add "$a" "a comment"
run close "$b" --reason "done" --force
run delete "$c" --force
run remember --key mem-kept "body one"
run remember --key mem-kept "body two"
run remember --key mem-gone "short lived"
run forget mem-gone
"$BD" export --all -o "$W/export.jsonl" >>"$W/bd.log" 2>&1 || die "bd export --all failed"
version=$("$BD" version 2>/dev/null | head -n 1)
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from cairn.cli import main; sys.exit(main(sys.argv[2:]))' \
    "$HERE" history-from-dolt "$W/repo/.beads" --dolt "$DOLT" \
    --export "$W/export.jsonl" -o "$W/hist.jsonl" --manifest "$W/man.json" >"$W/hist.out" 2>&1
hist_rc=$?
verify=$(python3 -I "$HERE/dev/verify-dolt-history.py" "$W/repo/.beads" "$W/hist.jsonl" \
    "$W/export.jsonl" --dolt "$DOLT" 2>&1); verify_rc=$?
python3 -I - "$W" "$version" "$hist_rc" "$verify_rc" "$verify" "$a" "$b" "$c" "$DOLT" <<'PY'
import json, os, subprocess, sys
from collections import Counter
w, version, hist_rc, verify_rc, verify, a, b, c, dolt = sys.argv[1:10]
repo = os.path.join(w, "repo", ".beads")
out = {"bd": version, "history_from_dolt_exit": int(hist_rc),
       "history_from_dolt_said": open(os.path.join(w, "hist.out")).read().strip().splitlines()[-3:],
       "verify_exit": int(verify_rc), "verify": verify.strip().splitlines()}
stores = [d for d in ("embeddeddolt", "dolt") if os.path.isdir(os.path.join(repo, d))]
out["dolt_dirs"] = stores
# Where the memories live in this version: ask Dolt directly (the scratch repo is ours).
for d in stores:
    for root, dirs, _ in os.walk(os.path.join(repo, d)):
        if ".dolt" in dirs:
            env = dict(os.environ)
            q = lambda sql: subprocess.run([dolt, "sql", "-q", sql, "-r", "json"], cwd=root,
                                           env=env, capture_output=True, text=True)
            tables = json.loads(q("show tables").stdout or '{"rows": []}')["rows"]
            names = sorted(next(iter(t.values())) for t in tables)
            out["tables"] = names
            r = q("select count(*) n from config where `key` like 'kv.memory.%'")
            out["memories_in_config_kv"] = (json.loads(r.stdout)["rows"][0]["n"]
                                            if r.returncode == 0 and r.stdout else None)
            out["memory_like_tables"] = [n for n in names if "memor" in n]
            break
events = []
if os.path.isfile(os.path.join(w, "hist.jsonl")):
    events = [json.loads(l) for l in open(os.path.join(w, "hist.jsonl")) if l.strip()]
kinds = Counter(e["kind"] for e in events)
out["events"] = dict(sorted(kinds.items()))
def has(pred):
    return any(pred(e) for e in events)
field = lambda e, f: e["kind"] == "field_change" and e["issue_id"] == a and e["data"].get("field") == f
created = {e["issue_id"]: e["data"].get("title") for e in events if e["kind"] == "created"}
manifest = json.load(open(os.path.join(w, "man.json"))) if os.path.isfile(os.path.join(w, "man.json")) else {}
actors = [e["actor"] for e in events if e["actor"]]
out["actors"] = {"with": len(actors), "of": len(events),
                 "from_commit_messages": manifest.get("read", {}).get("actors_from_commit_messages")}
notes = [e["data"] for e in events if field(e, "notes")]
checks = {
    "three issues created, by title": created == {a: "alpha", b: "beta", c: "gamma"},
    "description set": has(lambda e: field(e, "description") and e["data"].get("new_value") == "first description"),
    "notes: first the full text, then only the appended tail": len(notes) == 2
        and notes[0].get("new_value") == "note one" and notes[1].get("appended") == "\nnote two",
    "priority change 2 -> 1": has(lambda e: field(e, "priority") and e["data"].get("old_value") == 2
                                  and e["data"].get("new_value") == 1),
    "status open -> in_progress": has(lambda e: field(e, "status") and e["data"].get("old_value") == "open"
                                      and e["data"].get("new_value") == "in_progress"),
    "label urgent added": has(lambda e: e["kind"] == "label_added" and e["issue_id"] == a
                              and e["data"].get("label") == "urgent"),
    "dependency beta -> alpha": has(lambda e: e["kind"] == "dependency_added" and e["issue_id"] == b
                                    and e["data"].get("depends_on_id") == a),
    "comment added": has(lambda e: e["kind"] == "comment_added" and e["issue_id"] == a),
    "close of beta, with its reason": has(lambda e: e["issue_id"] == b and e["kind"] == "field_change"
                         and e["data"].get("field") == "status" and e["data"].get("new_value") == "closed")
        and has(lambda e: e["issue_id"] == b and e["kind"] == "field_change"
                and e["data"].get("field") == "close_reason" and e["data"].get("new_value") == "done"),
    "deleted child, with its last state": has(lambda e: e["kind"] == "deleted" and e["issue_id"] == c
                                              and (e["data"].get("issue") or {}).get("title") == "gamma"),
    "memory set twice, old body kept": has(lambda e: e["kind"] == "memory_set" and e["memory_key"] == "mem-kept"
                                           and e["data"].get("old_value") == "body one"),
    "forgotten memory, last body kept": has(lambda e: e["kind"] == "memory_deleted" and e["memory_key"] == "mem-gone"
                                            and e["data"].get("old_value") == "short lived"),
    "every actor named is the one that wrote (measure)": set(actors) <= {"measure"},
    "some event has its actor": len(actors) > 0,
    "verifier passes": int(verify_rc) == 0,
}
out["checks"] = {k: ("ok" if v else "FAILED") for k, v in checks.items()}
print(json.dumps(out, indent=1))
sys.exit(0 if all(checks.values()) and int(hist_rc) == 0 else 1)
PY
