"""cairn — an agent-facing persistence store (work tracking + keyed memories).

Two kinds of store: a read-only SHADOW of a beads (bd) store, which cairn
rebuilds from bd's export and diffs against bd (bd stays its only writer), and a
WRITABLE store made by `cairn init`, which cairn writes itself.
"""

__version__ = "0.3.0"

# The CLI name. bd's verbs and flags are kept, so porting a hook is a matter of
# swapping this name for `bd` in the hook script.
PROG = "cairn"
