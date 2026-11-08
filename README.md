# cairn

An agent-facing store for work tracking and keyed memories: a small SQLite
database, the verbs and flags of [beads](https://github.com/gastownhall/beads)
(`bd`), and nothing but the Python standard library.

**Status: a read-only shadow of a beads store, plus a write engine for stores
of its own.** A store is one of two kinds:

- a **shadow** (`cairn shadow sync`): rebuilt from `bd export --all`, answering
  the same read questions and diffed against bd's answers. bd is its only
  writer; every write verb is refused there. Deleting `.cairn/` undoes
  everything.
- a **writable store** (`cairn init`): cairn's own. Write verbs work only here.
  A shadow can never become writable: only `init` sets the meta keys the write
  path checks.

The write engine is proven against bd offline — a scripted drill and a replay of
the bd commands agents actually typed, run against a throwaway bd store and a
throwaway cairn store side by side (see Tests). No live store is written by both.

## Install

```bash
ln -s "$PWD/bin/cairn" ~/.local/bin/cairn     # nothing to pip install
```

Needs Python 3.9+ and, for shadow mode, `bd` on PATH.

## Use

```bash
cairn shadow sync        # rebuild .cairn/cairn.db (next to .beads/) from bd
cairn shadow diff        # compare cairn's answers with bd's
cairn shadow run -q      # sync + diff + log; prints nothing when they agree
cairn shadow status      # progress against the exit criteria

cairn ready [--json] [-n N] [-p P1] [-t bug] [--parent ID] [--include-deferred]
cairn list  [--status=in_progress] [--all] [--json] [-n N]
cairn blocked | show ID... | stats | where | check
cairn memories [keyword] | recall KEY
cairn memories --rank [keyword] [-n N] [--half-life D] [--recency-weight W] [--json]
cairn related KEY [--depth N] [--json]                      # a memory's links, out and in
cairn history ID [--limit N] [--kind KIND] [--json]          # events of one issue, oldest first
cairn history --memory KEY [--limit N] [--kind KIND] [--json]  # its events, each with the body it replaced
cairn prime [--budget BYTES]         # default 10000: what Claude Code shows of a hook in full
cairn export [--include-memories | --all | --allocator | --events] [-o FILE]
cairn import FILE [--interactions F] [--ledger F] [--hot F]   # build from files, no bd
cairn seed-from-dolt BEADS_DIR [--dolt PATH] [-o FILE]        # allocator seed, see below
cairn history-from-dolt BEADS_DIR --export F -o FILE [--interactions F] [--manifest F]
cairn migrate                                                 # writable store -> this schema
```

Writable stores (`--actor`, else `$BEADS_ACTOR`, else git user.name; a claim
requires `--actor` or `$BEADS_ACTOR`):

```bash
cairn init --prefix proj [--from EXPORT [--seed F] [--interactions F] [--events F] [--ledger F] [--hot F]]
cairn create "Title" [-d TEXT|--body-file F] [-p P] [-t TYPE] [--parent ID] [-l a,b] [--silent]
cairn update ID... [--claim] [--status S] [--assignee A] [--title T] [-d T] [--notes T]
                   [--append-notes T] [--design T] [--acceptance T] [-p P] [-t T]
                   [--add-label L] [--remove-label L] [--defer DATE]
cairn close ID... [--reason R] [--force] [--suggest-next]  |  cairn reopen ID...
cairn note ID TEXT  |  cairn comment ID TEXT  |  cairn comments [add] ID [TEXT]
cairn dep add A B [--type blocks|parent-child|related|discovered-from]  |  cairn dep rm A B
cairn defer ID --until WHEN  |  cairn undefer ID  |  cairn delete ID --force
cairn remember --key KEY BODY  |  cairn forget KEY
cairn ready --claim [ready's filters]                 # claim the first issue ready lists
```

Every write is one SQLite transaction (`BEGIN IMMEDIATE`, WAL, 5 s busy
timeout) that also appends its history row to `events`; a change cannot commit
without its event. `update --claim` is a compare-and-swap on the assignee: it
succeeds when the issue is not closed and unassigned or already the actor's,
and otherwise exits 1 naming the holder. Taking over is `update --assignee`
(logged) followed by `--claim`.

Ids: a root id is the prefix, a dash and base-36 characters, as wide as the
widest root id in the store (at least three), drawn at random until unused;
the width grows by one only when a width runs out. A child is `<parent>.N`.
No id and no child number is ever issued twice: every issued id stays in
`used_ids` after the issue is deleted, and `id_counters` keeps each parent's
next number.

Memory links are a projection of the bodies: `remember` and `forget` re-derive
`memory_edges` in the same transaction.

### Moving a bd store over

```bash
cairn seed-from-dolt .beads --dolt PATH/TO/dolt -o seed.json   # read-only, on a copy
bd export --all -o bd.jsonl
cairn history-from-dolt .beads --dolt PATH/TO/dolt --interactions .beads/interactions.jsonl \
           --export bd.jsonl -o history.jsonl --manifest manifest.json   # read-only, on a copy
cairn init -p proj --from bd.jsonl --seed seed.json --events history.jsonl \
           --ledger .beads/memgraph-ledger.json
cairn migrate                     # later: bring a writable store to a newer schema
```

The order, for a repo whose store is bd (beads-agent-pipeline 0.2.0 and later install on cairn
only and refuse a live bd store that has not been through this). Stop at the first failure:

1. Freeze every writer: no other session, no `bd dolt push`, no other clone writing.
2. `bd export --all -o bd.jsonl`.
3. `cairn seed-from-dolt .beads --dolt PATH/TO/dolt -o seed.json`. A seed not read from Dolt (a
   hand-made one) lacks the ids bd deleted, and cairn could issue them again.
4. `cairn history-from-dolt ... --export bd.jsonl -o history.jsonl --manifest manifest.json`
   (`--interactions .beads/interactions.jsonl` when the file exists). If it fails, stop: an
   `init` without `--events` is a content migration that silently leaves the history behind.
5. `cairn init -p PREFIX --from bd.jsonl --seed seed.json --events history.jsonl` (plus `--ledger`
   and `--hot` when those files exist). It must print that the re-export is byte-identical, or
   equal in content with a layout warning; anything else is a failed migration.
6. `cairn check`, then write the four tracked files into `.cairn/` (`cairn export --all`, split
   into `issues.jsonl` and `memories.jsonl`; `cairn export --allocator -o .cairn/allocator.json`;
   `cairn export --events -o .cairn/events.jsonl`; the pipeline's `tools/refresh-beads-exports.sh`
   does all of it) and commit them with `.cairn/config.json` and `.cairn/.gitignore`.
7. Only now replace bd's hooks (git hooks, harness hooks) with ones that call cairn; the
   pipeline's installer does this.
8. Keep `.beads/` until a person has read the manifest; archive it, then remove it. Every other
   clone needs cairn 0.7.0 or later and a `cairn sync` after it pulls.

`init --from` builds a writable store from a bd export, all or nothing. It
refuses an export it cannot represent faithfully (a row type it does not know,
an issue under another prefix) and never replaces an existing store. The id
allocator is seeded so that nothing bd ever issued is issued again: `used_ids`
gets every imported id plus every id in the seed, and each parent's next child
number is one past both bd's counter and the highest child number seen.

No bd export carries the ids bd deleted, and bd's child counters live only in
its database, so the seed comes from Dolt: `seed-from-dolt` copies the Dolt
repository to a temporary directory and reads, from the copy, every id in the
history of `issues` and bd's `child_counters`. bd embeds Dolt and ships no CLI;
any dolt release that reads the store works (measured with 2.4.2 on bd 1.1.2).

bd keeps its history only in Dolt: every earlier body of a memory, every earlier
title, description and note, who changed what. No export carries it.
`history-from-dolt` reads it, from a copy, through Dolt's `dolt_diff_<table>`
tables, so only changes leave Dolt, and writes an events file for `init
--events`: issue creations, column changes (`field_change`; a notes append as
`appended`), memory versions (`memory_set` with the replaced body as
`old_value`) and deletions, labels, dependencies and comments. Each event is
dated by its Dolt commit, carries `data.dolt_commit`, has the id
`dolt-<hash>` (two runs give the same file), and takes its actor from the row
bd wrote to its `events` table in that commit, else (since 0.7.0) from bd's
auto-commit message (`... (auto-commit) by <actor>`): bd 1.3 keeps that table out
of every commit. A commit whose events table names two actors, or whose message
names more than one, gets none. `--interactions` merges bd's
field-change log in by time and keeps a change both sources hold once, from
the log (same issue, field and values, within 5 seconds; a notes append in Dolt
matches the log's full-shape row of it). `--export` (required) is the export
taken with this Dolt store, and the history must end at it: every issue and
memory on both sides, every memory body and every issue column the reader
carries (a field the export omits counts as empty), or the read fails and
nothing is written. That catches a stale copy, a log whose diffs do not add up
to the present, and a bd that keeps memories somewhere other than
`config.kv.memory.*` (bd 1.1.2's place). It cannot see a gap whose two ends are
right: `dev/verify-dolt-history.py` checks the intermediate memory bodies and
issue texts against `dolt_history_*`. A `.beads` holding both an embedded and a
server-mode Dolt store is refused until the repository is named. `-o` refuses an existing file: on
a store that is already cairn, the history goes into its events file by a
splice, never written over it.

The manifest says what was read, carried and dropped, and what stays in Dolt
whatever the reader does: the commit graph (hashes and dates ride along, parent
links and messages do not), the order of changes inside one commit, the Dolt
committer (never used as the actor), bd's `events` rows themselves (each row's
fate is counted by type: its commit carried, its commit carrying nothing, or
removed by bd), changes between two commits, the text of comments on deleted
issues, issue columns and tables cairn has no place for (named), and the
source repository it read.
A history with merge commits is refused. `dev/verify-dolt-history.py` checks a
written file against the Dolt copy: every memory body and every issue text
value Dolt ever held must be recoverable from the file plus the export.

### Migrating from other bd versions

A migration does not depend on cairn knowing your bd version. `init --from`
proves each one: before anything is written it exports the new store and
compares that with the bd export it read, row by row and field by field
(dependency and label order aside). Any difference in content (a field cairn
cannot hold, a row it would drop) refuses the migration and names the
differences. A difference only in layout is reported, and the tracked export is
then rewritten once.

The layout is learned from the source, not assumed: the key order (fields a
newer bd adds included) and the order an issue's dependencies are listed in,
which bd changed between versions. `seed-from-dolt` records the bd version
(`local_metadata.bd_version`), reads embedded (`.beads/embeddeddolt/`) and
server-mode (`.beads/dolt/`) stores, and derives the child counters from the id
history if a bd has no `child_counters` table (it says so). `init` records the
version; `--bd-version` names it when there is no seed.

| bd | Migration | Export layout | Measured | History (`history-from-dolt`) |
|---|---|---|---|---|
| 1.1.2 | proven on a 244-issue store | byte-identical | 2026-10-15 | every kind read; actors from bd's events table, else the commit message (2026-11-08) |
| 1.3.1 | proven on the same data plus a deleted child | byte-identical | 2026-10-15 | every kind read; actors from commit messages only (2026-11-08) |
| 1.3.4 | real-bd tests pass with the 1.3.1 rules | not measured | 2026-11-08 | every kind read; actors from commit messages only |

The history column is `dev/measure-bd-version.sh BD`: a small store grown through that bd
(issues, a child, description, notes twice, priority, status, a label, a dependency, a comment, a
close, a deleted issue, a memory set twice, a memory forgotten), read back, every expectation
checked and `dev/verify-dolt-history.py` run. Memories are `config` rows `kv.memory.<key>` in all
three. bd 1.3 lists its `events` table (and `bd_events_journal`, `wisps`, ...) in `dolt_ignore`,
so those rows are in no commit and no event's actor can come from them; the reader takes the
actor from bd's auto-commit message (`... (auto-commit) by <actor>`) where the events table names
none, and the manifest says how many (`read.actors_from_commit_messages`) and warns when the events
table is not versioned. A commit whose message names no actor (1.3's `close`, `delete`) leaves its
events without one: 5 of 22 events had an actor on 1.3.x, 19 of 22 on 1.1.2.

After the migration cairn's verbs follow bd 1.1.2's rules. bd 1.3.1 tightened
some of them: closing a parent with an open child, closing an issue assigned to
someone else, and `--assignee` over another actor's live claim are refused
without `--force` (cairn allows them), and `create --id` over an existing id
fails (cairn refuses it too). On reads, 1.3.1's `show` adds a `revision` field
and `ready` orders differently (not analysed). The real-bd tests encode these
per version (`tests/bdversion.py`).

To check a bd version nobody has measured, run the real-bd tests with that bd
first on PATH:

```bash
CAIRN_TEST_WITH_BD=1 python3 -m unittest tests.test_drill tests.test_bd_integration
```

A failure there is a behaviour your bd has and 1.1.2 did not. It does not make
the migration unsafe (the self-check guards that); it tells you where cairn will
behave differently from the bd you are leaving. Record it in `tests/bdversion.py`
and in the table above.

### What git must carry, and worktrees

The database is git-ignored. What rebuilds it is four tracked files, which live in the store
directory itself (`.cairn/issues.jsonl`, `memories.jsonl`, `allocator.json`, `events.jsonl`; the
`.cairn/.gitignore` cairn writes un-ignores them) since 0.7.0. Before, they lived in `.beads/`,
and cairn still reads them there while `.cairn/` holds none, so a clone that has not pulled the
move keeps working and a pull that moves them moves the clone with it. The two JSONL exports are
bd's format; two more files hold what that format has no rows for: `cairn export --allocator -o .cairn/allocator.json` writes every id ever
issued and each parent's last child number, in the seed format; `cairn export --events -o
.cairn/events.jsonl` writes the history, one JSON line per event, oldest first, every key present
(`id`, `kind`, `issue_id`, `memory_key`, `actor`, `created_at`, `data` as an object). `data` is
opaque to import and export: `memory_set` and `memory_deleted` rows written under 0.4.0 have no
`old_value` (see Memory links), and a file that mixes both shapes imports and re-exports byte for
byte (`EventsExportTest`). A column change is `field_change` with `field`, `old_value` and
`new_value`, except a notes append: since 0.4.2 a notes change whose new text extends the old
(`note`, `update --append-notes`, or an `update --notes` that happens to) is `{"field": "notes",
"appended": <the added text>}`, because the full shape repeated the whole notes field twice in
every event and the file grew with the square of the notes on a long issue. The text after an
append is the text before it plus `appended` (`cairn/eventdata.py`). The first notes change of an
issue keeps the full shape, since notes set by `create` or carried over from bd are in no event:
replaying an issue's notes events from the start rebuilds the text, and a reader that starts in
the middle of a run of appends has only the tails. Any other notes change (a replacement, a clear)
keeps the full shape. Rows written before 0.4.2 are all the full shape and import as they are. Two
exports of
one store are equal and a new event lands at the end, so git sees an append. A fresh checkout
rebuilds with `cairn init -p P --from <issues + memories> --seed .cairn/allocator.json --events
.cairn/events.jsonl` without reissuing an id and with its history (`FreshCheckoutTest`,
`EventsExportTest`); `init` checks that every event came back with its content (per id; the order
is the file's by construction and pinned by the test) and refuses a row with a key it does not know. A git worktree checks out `.cairn/config.json` but no database; an untracked
`.cairn/redirect` holding the absolute path of the main `.cairn/` points every verb there (one hop).

### More than one clone: `cairn sync`

Worktrees on one machine share one database through the redirect. Another clone (another
machine, CI, a re-clone) has its own, and git carries only the four files. `cairn sync` brings a
clone's database up to them. It compares the store's event history with the tracked `events.jsonl`:
a later export of one store is a string prefix of the new one, so the two are **equal**, the
store is **ahead** (writes not exported yet: the export refresh writes them), the store is
**behind** (a pull brought another clone's work: `sync` rebuilds the database from the four files,
which hold everything it has, and keeps the old one as `cairn.db.before-sync-<time>`), or they
**diverged** (both clones wrote: refused, and `sync` says where the histories part). There is no
merge; one side's writes are redone by hand. A fresh clone has no database at all and `sync`
builds it, with the prefix `init` recorded in `.cairn/config.json` (or, failing that, the one
prefix of the issue ids). Unmerged export files in git are never rebuilt from.

While the store is behind or diverged every write is refused (exit 2) with the command to run,
so nobody claims work another clone already closed; reads still work and warn on stderr. `sync
--status` says which state it is (exit 1 behind, 2 diverged). The check that runs on every verb
compares one line, where the two histories meet; `sync` compares them whole before it changes
anything, under the store's write lock, and a write that was waiting for that lock refuses
instead of landing in the replaced file. What the four files have no field for, a rebuild takes
from the memory events (since 0.7.0): memory tombstones from `memory_deleted`, so `check` names
the deletion time of a forgotten key after a `sync`, and each memory's `created_at` (the
`memory_set` that started its current life) and `updated_at` (its last `memory_set` that changed
the body, as the writer keeps it). A ledger
given to `init --ledger` only fills keys the events never mention. A rebuild prints its warnings
after the `Synced` line (`--json`: `warnings`).

`migrate` applies the numbered steps in `store.MIGRATIONS`. It holds the write
lock, writes `cairn.db.v<N>.bak` first, runs every step in one transaction,
checks foreign keys before committing, and removes the backup again if a step
fails (the store is then unchanged). A shadow is never migrated; `shadow sync`
rebuilds it. A store of an older schema version is refused by every verb with
a pointer to `cairn migrate`.

Write semantics measured on bd 1.1.2 and matched (pinned in `tests/test_drill.py`
and `tests/test_writer.py`): `close` leaves `assignee` and `started_at`;
`reopen` clears `closed_at` and `close_reason`; closing a closed issue changes
nothing; `close` refuses an issue with an open `blocks` dependency (exit 1)
unless `--force`; a closed or deferred issue is not claimable (exit 1);
`--status in_progress` is not a claim (no compare-and-swap, the assignee
stays); `ready --claim` skips a ready issue assigned to someone else; `note`
appends a line to `notes`; `dep rm A B` removes every edge from A to B whatever
its type; `delete` removes the edges into the issue too. A WHEN for `defer`
and `--defer` is an ISO date or timestamp, `+Nh`, `+Nd`, `+Nw` or `tomorrow`,
checked before anything is written.

`delete` records the whole issue (row, labels, comments, edges) in its event,
and each surviving issue that lost an edge gets its own `dependency_removed`
event. A shadow is never replaced over a writable store: `shadow sync`,
`shadow run` and `import` refuse a target that `cairn init` made.

`--json` works before or after the verb, as with bd. `ready` and `list` stop at
bd's default limits (100 and 50); when a `--json` listing is cut short, cairn
says so on stderr — the JSON on stdout keeps bd's shape.

Exit codes: **0** ok; **1** not found, differences, or `check` errors; **2**
refused, bad arguments, or could not run. An internal error is reported as 2,
never as 1, so a crash cannot be read as "differences found". **141**, with
nothing on stderr, when the reader of stdout went away (`cairn list | head`), as
bd and any process killed by SIGPIPE.

The store is found by walking up from the working directory to a `.cairn/`
directory (not stopping at a nested git repository), or from `CAIRN_DIR`. bd is
always run from the directory that holds `.beads/`.

| Environment | Meaning |
|---|---|
| `CAIRN_DIR` | store directory, overriding the upward search |
| `CAIRN_ISSUE_PREFIXES` | extra issue-id prefixes, comma-separated (see Memory links) |
| `CAIRN_DEBUG` | print a traceback for an internal error |

Git ignores everything in `.cairn/` except `config.json`, the one file there
that is not derived, and the four tracked files when they live there (since 0.7.0):

```json
{"issue_prefixes": ["ws"]}
```

## What a shadow run checks

| Question | bd read path | cairn |
|---|---|---|
| ready set and its order | `bd ready --limit 0 --json` | `Snapshot.ready` |
| in-progress set | `bd list --status=in_progress --json` | `Snapshot.select` |
| blocked set and each `blocked_by` | `bd blocked --json` | `Snapshot.blocked` |
| every field of every issue | `bd list --all --json` | `Snapshot.issue_object` |
| `show` output, three issues per run, rotating | `bd show ID --json` | `issue_object(shape="show")` |
| memory keys and body hashes | `bd memories --json` | store rows |
| round trip: every exported row comes back out unchanged | `bd export --all` | `cairn export --all` |

The import comes from `bd export`, the comparisons from bd's query commands, so
an import bug and a semantics bug both show up.

A run has three outcomes, logged to `.cairn/shadow-log.jsonl`:

- **clean** — they agree and nothing was left out of the import;
- **not clean** — they disagree (`DIFF` lines) or rows could not be imported
  (`LOSS` lines); exit 1;
- **could not run** — bd missing, a timeout, unreadable input; exit 2. This is
  evidence of nothing: it neither counts towards the soak nor resets it.

Another session can write to bd between the export and the queries, so a run
that finds differences repeats once and reports only what is still different.
All bd calls of a run share one deadline (15 s by default, `--deadline`), so a
hung bd ends the run with exit 2 instead of being killed silently by whatever
supervises it. On SIGTERM cairn removes its temporary export and database before
exiting; leftovers from a run that was killed outright are cleared by the next.

Oddities in the source — a dependency on an issue that no longer exists, a
misspelt link type, an optional input file that is absent — are `WARN` lines. They are shown unless `--quiet` and do
not make a run unclean.

Exit criteria for the shadow stage: ten consecutive clean runs spanning fourteen
days.

Not compared by a shadow run:

- filter combinations (`-p`, `-t`, `--parent`, `-a`, `-u`, labels) — checked by
  hand against bd on a live store and covered by unit tests;
- what bd has no query for: memory timestamps and deletion records (from the
  bd-memgraph ledger), the hot tier, parsed memory links, imported history.

## bd semantics, as measured

Measured on a throwaway bd 1.1.2 store and pinned by `tests/test_semantics.py`:

- **blocked**: a non-closed issue with a `blocks` dependency on any issue that
  is not closed — an in-progress or deferred blocker still blocks. A blocked
  parent blocks its children, transitively. `blocked_by` lists the direct
  blockers, or the blocked parent when there are none.
- **ready**: status exactly `open`, not blocked, own `defer_until` not in the
  future, and no *direct* parent with a future `defer_until` (a grandchild of a
  deferred issue is ready). The parent's status is not consulted: a closed
  parent that still carries a future `defer_until` hides its children.
- **order**: priority ascending, `created_at` descending to the second, id ascending.
- `related` and `discovered-from` dependencies never block.
- **cycles**: `dep add` refuses a `blocks` edge that would close a cycle of
  `blocks` edges, at any length, exit 1, `adding dependency would create a
  cycle` (1.1.2 and 1.3.0). On 1.1.2 the walk ignores status (a closed issue on
  the path still counts) and every other type: a `blocks` edge against a
  `parent-child` edge, and `related` or `discovered-from` both ways, are
  accepted (not measured on 1.3.x). cairn refuses
  the same edges and names the path; pinned in `tests/test_writer.py` and
  `tests/test_drill.py`. `cairn check` reports, as an error, any `blocks` cycle
  a store already holds (one written before cairn refused them, or imported).
- `dependency_count` / `dependent_count` count `blocks` edges only in `list`,
  `ready` and `export`, but edges of every type in `show` (so an epic's children
  are dependents there). cairn reproduces both.
- `blocked --json` and the issues nested under `dependencies` in `show --json`
  omit `dependencies`, `parent` and the three counts.
- An issue with status `deferred` never returns to `ready`, even after its date
  passes. cairn mirrors that for parity and lists such issues separately
  (`Snapshot.due_deferred`, shown by `cairn prime`).

Not measured (bd refuses to close an issue with an open blocker): whether a
closed issue can count as blocked. cairn assumes it cannot.

How bd allocates ids, measured the same way and pinned by `IdAllocationTest` in
`tests/test_bd_integration.py`. cairn's allocator (writable stores) is designed
around these and refuses the last one:

- A root id is the prefix, a dash and three characters of `[0-9a-z]` in a small
  store; it is not a function of the title.
- Children are numbered `<parent>.1`, `.2`, … to any depth, and a number is
  never issued twice: deleting a child does not free its number. Deleted ids
  are absent from the export, so the next number cannot be derived from one.
- `bd create --id` accepts any id under the store's prefix, but not together
  with `--parent`: a child-shaped id given alone gets no parent link.
- `bd create --id` with an id that is taken does not fail. It replaces the
  issue — exit 0, the description gone, a closed issue open again.

## Differences from bd that are deliberate

Writes (each pinned in `tests/test_writer.py` or `tests/test_drill.py`):

- `update --claim` needs an actor; bd falls back to git user.name, which makes
  every session the same claimant.
- Stored status `blocked` cannot be written; bd accepts it. Blocked-ness is
  computed from dependencies (an import still accepts the stored value).
- A second parent is refused; bd accepts it.
- `create --id` of an id that exists or ever existed is refused; bd silently
  replaces the issue (description gone, priority reset, a closed issue open).
- `update`, `close` and the other verbs need an id; bd acts on the issue it
  last touched when none is given.
- `remember` needs `--key`; bd generates one.
- `remember` runs admission control (bd-memgraph's `evolve`, folded in): a body
  of at least 20 distinct words that is a near-duplicate of another key (token
  overlap at least 0.5) is refused, exit 1, naming that key and the update recipe;
  `--force` stores it anyway, and a key the body `supersedes::` is not a candidate.
  A shorter near-duplicate is stored, with that key named on stderr (over so few
  words three shared ones reach 0.5). A related body (0.18 or more)
  that links to no memory is stored with a note on stderr naming keys to link.
  Rewriting the same key is never refused. bd stores every body.
- `delete` without `--force` is refused (exit 2); bd prints a preview and exits
  0. `delete` does not rewrite mentions of the id in other issues' text; bd does.
- A usage error (no title) exits 2, where bd exits 1.
- bd verbs `claim`, `supersede`, `duplicate` and `edit` are not implemented and
  exit 2 (use `update --claim` or `ready --claim` to claim).
- `--json` on `dep`, `delete`, `remember` and `forget` prints a small status
  object of cairn's own.

Reads:

- `export` is byte-identical to the bd export a store was migrated from: rows by
  priority, then newest first, then id; keys in the order learned from the
  source (bd's Issue struct order when there is no source); an issue's
  dependencies in the source's order (bd 1.1.2: UUIDv5 edge id,
  `internal/storage/depid`; bd 1.3.1: target id); `<`, `>`, `&`, U+2028 and
  U+2029 escaped as Go's encoding/json does. Measured on the workspace store
  (244 issues, 49 memories) from bd 1.1.2 and 1.3.1, and pinned against real bd
  by `RoundTripTest`. Consecutive exports of an unchanged store are equal.
- `show --json` adds `dependents` and `comments`; bd's prints neither. Every key
  bd prints is present with the same value.
- `blocked_by` is sorted; bd's order is unspecified.
- History (`events`) is append-only, enforced by triggers.
- Read verbs open the database read-only.
- A malformed source line is an error naming file and line; a failed import
  leaves the previous database in place.
- Timestamps are compared as instants, so an offset, a space separator or a
  date-only value orders correctly.

## Memory links

Links inside memory bodies use the bd-memgraph notation: `[[key]]` (related),
and `supersedes::`, `depends-on::`, `contradicts::`, `justified-by::`,
`refines::` before the brackets. A target of one or two characters is the escape
for writing about the notation and is not a link.

A target is an issue reference when it is an issue id in the store; otherwise a
memory when it is a memory key; otherwise an issue reference when it has the
shape of an issue id under the store's prefix or one listed under
`issue_prefixes` in `.cairn/config.json` (or in `CAIRN_ISSUE_PREFIXES`). `cairn check` reports dangling and self links as errors,
explains a link to a deleted key, and flags an issue with more than one parent.

Under cairn these replace bd-memgraph: `remember` does its `evolve` (see the
deliberate differences below), `cairn check` its hard link check, and the
`created_at` / `updated_at` columns and `memory_tombstones` its ledger. Since
0.7.0 `cairn memories --rank` is its `rank`: PageRank over the memory-to-memory
links (damping 0.85), times recency (0.5 ** (age / half-life), age from the
newest YYYY-MM-DD in the key or body or from `updated_at`, whichever is later;
an undated memory counts 0.3), times 0.1 for a key some memory `supersedes`;
same defaults (half-life 90 days, recency weight 0.5). It differs from
bd-memgraph in what dates a memory: `updated_at` (the last change of the body,
from the events after a rebuild) where bd-memgraph read its ledger's
`last_changed`, the day it happened to observe a change; in listing every memory
unless `-n N` (bd-memgraph: the top 20); and in breaking a tie of score and
recency by key A to Z (bd-memgraph: Z to A). A keyword keeps the whole graph's
scores and shows only the matches. `cairn related KEY` is its `related`, and
`--depth N` walks the links N hops, which is what its `lineage` printed; issue
links are listed, not walked, and a link to a missing memory is shown. Not
carried: `graph` (a Mermaid flowchart of the links, nothing reads it) and
`stats` (a link census for curation; `cairn check` and `cairn stats` count the
store); a memory's versions are `cairn history --memory`.

A memory's earlier versions are in its events: `memory_set` and
`memory_deleted` carry the body they replaced as `old_value` (since 0.4.1;
before that only its hash, so a version written under 0.4.0 is gone unless a
commit of `memories.jsonl` caught it). `old_value` is null for a new key and
for a rewrite with the same body; the create signal is `old_hash` null, not
`old_value` null. The live body is in `memories.jsonl`; every replaced one is
in `events.jsonl`, both tracked in git, so a fresh checkout gets every version
back. That includes `forget`: the body a `forget` removes from `memories.jsonl`
lands in the events file at the next export, so forgetting a memory does not
take its text out of the checkout (it never took it out of git history). bd
kept earlier versions only in Dolt's history, where no bd verb reached them.

`cairn history --memory KEY` prints them: each `memory_set` with the body it replaced
(`old_value`), and a `memory_deleted` with the last one, even after `forget` (when `recall` exits
1). `cairn history ID` does the same for an issue, a deleted one included (its `deleted` event
holds the issue as it was). Order is recording order, never time order; `--limit N` keeps the
newest N, still oldest first; `--kind` is an exact match. `--json` is a list of the objects `export
--events` writes. Each event's `data` is printed as stored: a `memory_set` shows the body it
replaced, not the new one (`recall` shows the live body; a new key's `old_value` is `null`, and a
row written under 0.4.0 has hashes only), and a notes append shows only the added tail
(`appended`). The text shows each event's `data` keys sorted, strings and numbers unquoted, `null`
as `null`, lists and objects as one sorted JSON line, and the lines of a multi-line value indented
under its key. An unknown issue or key exits 1; one that exists with no events (an issue imported
from bd without history) prints `No events for ID` and exits 0. A read verb: it answers on a shadow
and on a store that is behind or diverged, after the warning.

## Known limits

- A shadow is read-only and uses SQLite's default journal mode (it is
  replaced whole on every sync); writable stores use WAL.
- `bd import` of `cairn export --all` reproduces the export byte for byte
  (measured, `RoundTripTest`), but bd's child counter then restarts from the children it
  sees and hands out numbers cairn had deleted. A rollback into bd must burn
  those numbers first. Events and `used_ids` do not travel in that export; `export --events`
  and `export --allocator` carry them in files of their own (see "What git must carry").
- One issue prefix per store. `init --from` refuses an export holding issues under another prefix,
  and a seed naming one, before it writes anything (`issue_prefixes` in `.cairn/config.json` is
  for links in memory bodies, not for issues).
- `seed-from-dolt` copies the Dolt directory as files. A copy taken while bd writes can be torn;
  it is checked only for holding every current id, so take it in a quiet window (the workspace's
  `tools/cairn-move.sh` reads it twice and requires both reads to agree).
- `create --id` with a child-shaped id makes no parent link, as in bd.
- The stored status `blocked` is accepted because bd accepts it, while whether
  an issue *is* blocked is computed from dependencies.
- History imported from `interactions.jsonl` alone (`init --interactions`) is
  bd's audit log: issue field changes only, no memory events. `history-from-dolt`
  carries the rest, memory versions included (above).
- `cairn prime` prints its own layout, not bd's.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Real SQLite stores in temp directories; bd is replaced by a stub whose answers
are written by hand and which rejects any query whose flags differ from the
expected ones, so the shadow diff is tested for failing as well as passing.

```bash
CAIRN_TEST_WITH_BD=1 python3 -m unittest tests.test_bd_integration
```

builds the measured edge cases on a throwaway store with the real `bd` and
requires a clean comparison, including `bd show` for every issue; a second
store re-measures id allocation. Run it after upgrading bd.

```bash
CAIRN_TEST_WITH_BD=1 python3 -m unittest tests.test_drill
```

runs the same writes against a throwaway bd store and a throwaway writable
cairn store and compares them field by field (claim, refused second claimant,
takeover, a blocker hiding and returning an issue, notes, comments, labels,
close/reopen, defer, memories), asserts the deliberate differences, and imports
cairn's export into a fresh bd (`RoundTripTest`).

```bash
python3 tests/replay_corpus.py CORPUS.json [--prefix P]
```

replays bd write commands harvested from agent transcripts (a JSON list of
`{"argv": [...], "ts": ...}`; transcripts are private, so no corpus ships)
against both stores and reports every exit code and field that differs.

```bash
git config core.hooksPath .githooks
```

enables the pre-commit gate (`dev/ruff-gate.py`, needs `ruff` on PATH; it passes
and says so without it). It refuses a commit that adds a broad `except`, an
undefined name, a `requests` call without a timeout, SQL built from strings,
`shell=True`, `eval`/`exec`, `pickle` or `yaml.load` on a line the commit adds,
and leaves findings already in a file alone. It also refuses a new `# noqa` for one of
those rules that no longer suppresses anything. A false positive gets
`# noqa: <code> — <why>` on the line ruff reports (the first line of a split
string). Column and table names cannot be bound as SQL parameters, so the code
that interpolates them checks them against a fixed set (`writer._set`,
`queries.COUNTED_TABLES`). Development only; cairn itself stays stdlib.

## Licence

MIT — see `LICENSE`.
