"""The `data` of a field_change event: how a column change is written, and how it is read back.

A notes append (`cairn note`, `update --append-notes`) writes only the text it added,
`{"field": "notes", "appended": <tail>}`, because the full shape repeated the whole notes field
twice in every event, so the tracked events file grew with the square of the notes on a long
issue (workspace-7m8.5.8). Every other change, a notes change that is not an append included,
keeps `{"field", "old_value", "new_value"}`. Rows written before 0.4.2 are all the full shape;
both shapes import and re-export unchanged, since import treats `data` as opaque.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

APPEND_FIELDS = frozenset({"notes"})


def field_change_data(field: str, old: Any, new: Any, *, anchored: bool = True) -> Dict[str, Any]:
    """The event data for `field` going from `old` to `new` (the caller has checked they differ).
    The rule is the prefix test, not the command: any write whose new text extends the old is an
    append. `anchored` says an earlier event of this field already holds the text before `old`
    can be rebuilt from: false for the first change of the field in the history (notes set by
    `create`, or carried over from bd), which then keeps the full shape so the chain has a start."""
    if (anchored and field in APPEND_FIELDS and isinstance(old, str) and old
            and isinstance(new, str) and len(new) > len(old) and new.startswith(old)):
        return {"field": field, "appended": new[len(old):]}
    return {"field": field, "old_value": old, "new_value": new}


def apply_field_change(current: Optional[str], data: Dict[str, Any]) -> Any:
    """The field's value after the event, given its value before. A full-shape row names the
    new value itself; an append row needs the value before it, and a reader that starts in the
    middle of a history without one cannot rebuild the text (it still has the tail)."""
    if "appended" in data:
        if current is None:
            raise ValueError(f"an append to {data.get('field')} needs the value before it")
        return current + data["appended"]
    return data.get("new_value")
