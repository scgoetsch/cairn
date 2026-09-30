"""cairn — an agent-facing persistence store (work tracking + keyed memories).

Current stage: a read-only SHADOW of a beads (bd) store. bd stays the only
writer; cairn imports bd's export, answers the same read verbs, and diffs its
answers against bd's.
"""

__version__ = "0.2.1"

# The CLI name. bd's verbs and flags are kept, so porting a hook is a matter of
# swapping this name for `bd` in the hook script.
PROG = "cairn"
