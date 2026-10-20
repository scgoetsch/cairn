"""Admission control for `remember`: is this body new, an update of another
key, or in need of links?

The heuristic is bd-memgraph's `evolve` (token overlap over key and body,
divided by the smaller token set with a floor of 6), so a store moved from bd
keeps the thresholds its memories were written against. It is folded into
`remember` here (operator, 2026-10-20, workspace-7m8.20): under bd it was a
separate step an agent had to remember to run.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping, Set, Tuple

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")
STOP = set(
    """the and for with not are was were this that from into over under when only all
    its can will just use used using has have had but you they there then than does
    did each per via more most some what which where how why been being also may must
    should would could very much many other after before between during""".split()
)
NEAR_DUPLICATE = 0.5
RELATED = 0.18
# Below this many distinct words a draft's score is mostly the floor of 6 doing the work: three
# shared words make 0.5. Measured 2026-10-20: this store's memories run 53-360 words, and a
# rehearsal's 5-word test memory was refused against an unrelated key. A shorter near-duplicate
# is stored with the advice instead (operator, 2026-10-20).
MIN_WORDS_TO_REFUSE = 20


def tokens(text: str) -> Set[str]:
    return set(TOKEN_RE.findall(text.lower())) - STOP


def score(draft: str, memories: Mapping[str, str], *, exclude: Iterable[str] = ()
          ) -> List[Tuple[float, str]]:
    """(score, key) for every stored memory but `exclude`, best first."""
    dt = tokens(draft)
    skip = set(exclude)
    scored = []
    for key, body in memories.items():
        if key in skip:
            continue
        mt = tokens(body) | tokens(key.replace("-", " "))
        scored.append((len(dt & mt) / max(6, min(len(dt), len(mt))), key))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return scored
