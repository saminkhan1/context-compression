"""Schema preview for query-based context ("don't inline the data, describe it").

For large structured files, or for questions that need counting, filtering or
aggregation, the best context is not any encoding of the rows. It is a short,
deterministic description: shape, counts, fields with types, optional-field
coverage and two sample records. The agent then answers with code against the
file on disk.

Measured on the TOON retrieval benchmark (bench/FINDINGS.md): a single model
turn that writes a script from this preview answered 96-98% of questions,
versus 64-66% for TOON in context, at a fraction of the billed tokens.

This is deliberately *lossy* and *labelled*. Adapters must surface it as a
preview with the real path, never as a silent substitute for the file.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import ctab

_TYPE_NAMES = {
    bool: "bool", int: "int", float: "number", str: "string",
    type(None): "null", list: "array", dict: "object",
}


def _fields(rows: list[dict]) -> str:
    types: dict[str, set[str]] = {}  # insertion order is first-seen field order
    present = Counter()
    subs: dict[str, dict[str, None]] = {}
    for row in rows:
        for key, val in row.items():
            types.setdefault(key, set()).add(_TYPE_NAMES[type(val)])
            present[key] += 1
            if isinstance(val, dict):
                subs.setdefault(key, {}).update(dict.fromkeys(val))
    parts = []
    for key, kinds in types.items():
        label = f"{key}{{{','.join(subs[key])}}}" if key in subs else key
        label += ":" + "|".join(sorted(kinds))
        if present[key] < len(rows):
            label += f" (optional, in {present[key]}/{len(rows)})"
        parts.append(label)
    return ", ".join(parts)


def schema_preview(value: Any, max_inline_chars: int = 1500) -> str:
    """Deterministic summary of a parsed JSON value. Never depends on the question."""
    lines = []
    items = value.items() if isinstance(value, dict) else [("", value)]
    for key, val in items:
        ref = f'data["{key}"]' if key != "" else "data"
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            lines.append(
                f"{ref}: list of {len(val)} objects; top-level fields in order: {_fields(val)}"
            )
            lines.append("first 2 records:\n" + ctab.encode({key or "rows": val[:2]}))
        elif (
            isinstance(val, dict)
            and len(val) > 3
            and all(isinstance(v, dict) for v in val.values())
        ):
            lines.append(
                f"{ref}: object with {len(val)} keyed entries (key -> record); "
                f"record fields in order: {_fields(list(val.values()))}"
            )
            first_two = dict(list(val.items())[:2])
            lines.append("first 2 entries:\n" + ctab.encode({key or "entries": first_two}))
        else:
            text = json.dumps(val, ensure_ascii=False)
            summary = f" = {text}" if len(text) < max_inline_chars else f" ({len(text)} chars)"
            lines.append(f"{ref}: {_TYPE_NAMES[type(val)]}{summary}")
    return "\n".join(lines)


def labelled_preview(value: Any, source_path: str, source_desc: str) -> str:
    return (
        f"[Preview of {source_path} ({source_desc}). This is NOT the full data. To answer "
        f"questions about the records (counts, filters, sums, lookups), load {source_path} "
        "with code and compute the answer instead of reading or guessing.]\n"
        + schema_preview(value)
    )
