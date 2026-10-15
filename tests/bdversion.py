"""The bd on PATH, for the opt-in real-bd tests. bd's behaviour changed between
releases; the tests assert what was measured for each, so a run against a bd
nobody has measured yet fails exactly where it behaves differently."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Tuple


def bd_version() -> Tuple[int, ...]:
    if shutil.which("bd") is None:
        return ()
    out = subprocess.run(["bd", "version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in match.groups()) if match else ()


VERSION = bd_version()
# Measured 2026-10-15 on 1.3.1 against 1.1.2: closing a parent with an open child
# needs --force; --assignee over another actor's in_progress claim needs --force
# (or `bd reclaim`); closing an issue assigned to another actor is refused;
# create --id over an existing id fails instead of replacing; an issue's
# dependencies are listed by target id, not by edge id. cairn's verbs keep 1.1.2's
# rules (README, "Migrating from other bd versions").
STRICT_CLOSE = STRICT_TAKEOVER = CREATE_ID_REFUSED = TARGET_DEP_ORDER = VERSION >= (1, 3)
# Read-side differences seen on 1.3.1 and not analysed: `show` adds a `revision`
# field; `ready` orders issues differently. The shadow comparison reports them;
# they do not touch the export, so a migration is unaffected.
KNOWN_READ_DIFFERENCES = (
    (r"^show \S+: bd and cairn differ in \['revision'\]$", r"^ready order: ")
    if VERSION >= (1, 3) else ())


def unexplained(differences):
    return [d for d in differences
            if not any(re.search(p, d) for p in KNOWN_READ_DIFFERENCES)]
