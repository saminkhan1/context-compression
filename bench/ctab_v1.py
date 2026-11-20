"""ctab: lossless "tables where possible, compact JSON elsewhere" encoding.

Design goals, in priority order:
  1. Lossless: decode(encode(x)) == x for any JSON value (verified by caller).
  2. Familiar: every piece is either compact JSON or a CSV-like table with a
     header row, so no custom grammar beyond "@name[N]" section markers.
  3. Few tokens: repeated object keys are emitted once per table; nested
     uniform objects are flattened into dotted columns instead of indented.

Grammar
  document   := section+ | json
  section    := "@" path "[" N "]" NEWLINE header NEWLINE row{N}     (table)
              | "@" path NEWLINE json                                 (other)
  header     := col ("," col)*            col = dotted key path
  cell       := ""            -> key absent
              | bare          -> string (when unambiguous)
              | json          -> any JSON value (numbers, true/false/null,
                                 quoted strings, arrays, objects)

A top-level object whose values include arrays of objects becomes one section
per key; other top-level shapes fall back to a single section.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

_NUMLIKE = re.compile(r"^-?\d")
_RESERVED = {"true", "false", "null", ""}


def _cj(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_obj_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2 and all(isinstance(x, dict) for x in value)


def _flatten(obj: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, val in obj.items():
        path = f"{prefix}{key}"
        if isinstance(val, dict) and val and "." not in key and all("." not in k for k in val):
            flat.update(_flatten(val, path + "."))
        else:
            flat[path] = val
    return flat


def _columns(rows: list[dict[str, Any]]) -> list[str] | None:
    if any("." in k for r in rows for k in r):
        return None
    flats = [_flatten(r) for r in rows]
    cols = list(dict.fromkeys(k for f in flats for k in f))
    # A dotted column and its prefix must not both exist (ambiguous).
    colset = set(cols)
    for c in cols:
        parts = c.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[:i]) in colset:
                return None
    if any("," in c or "\n" in c or c.startswith(("@", "*")) or c == "" for c in cols):
        return None
    return cols


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _cell(value: Any, present: bool) -> str:
    if not present:
        return ""
    if isinstance(value, str):
        if (
            value not in _RESERVED
            and not _parses_as_json(value)
            and value == value.strip()
            and not value.startswith(("[", "{", '"', "@"))
            and not any(ch in value for ch in ',\n\r')
        ):
            return value
        return _cj(value)
    if isinstance(value, dict) and not value:
        return "{}"
    return _cj(value)


def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    lines = [",".join(cols)]
    for row in rows:
        flat = _flatten(row)
        lines.append(",".join(_cell(flat.get(c), c in flat) for c in cols))
    return "\n".join(lines)


def _table_worth_it(rows: list[dict[str, Any]], cols: list[str]) -> bool:
    # Avoid tables that are mostly holes (very heterogeneous records).
    filled = sum(len(_flatten(r)) for r in rows)
    return filled >= 0.6 * len(rows) * len(cols)


def encode(value: Any) -> str:
    if isinstance(value, dict) and value and all(k and not any(ch in k for ch in "\n[{") for k in value):
        parts = []
        for key, val in value.items():
            parts.append(_section(key, val))
        return "\n".join(parts)
    return _section("", value)


def _section(name: str, val: Any) -> str:
    if _is_obj_list(val):
        cols = _columns(val)
        if cols and _table_worth_it(val, cols):
            return f"@{name}[{len(val)}]\n{_table(val, cols)}"
    if _is_keyed(val):
        rows = list(val.values())
        cols = _columns(rows)
        if cols and _table_worth_it(rows, cols):
            lines = ["*," + _table(rows, cols)]
            body = lines[0].split("\n")
            body = [body[0]] + [_cell(k, True) + "," + line for k, line in zip(val, body[1:])]
            return f"@{name}{{{len(val)}}}\n" + "\n".join(body)
    return f"@{name}\n{_cj(val)}"


def _is_keyed(val: Any) -> bool:
    return (
        isinstance(val, dict)
        and len(val) >= 2
        and all(isinstance(v, dict) and v for v in val.values())
        and all(k != "" for k in val)
    )


# ---------------------------------------------------------------- decoding

def _parse_cell(text: str) -> tuple[bool, Any]:
    if text == "":
        return False, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        return True, text


_DEC = json.JSONDecoder()


def _split_row(line: str) -> list[str]:
    cells, i, n = [], 0, len(line)
    while True:
        if i < n and line[i] in '[{"':
            _, end = _DEC.raw_decode(line, i)
        else:
            end = line.find(",", i)
            end = n if end < 0 else end
        cells.append(line[i:end])
        if end >= n:
            return cells
        i = end + 1


def _unflatten(flat: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, val in flat:
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = val
    return out


def decode(text: str) -> Any:
    lines = text.split("\n")
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("@") and (not sections or _section_complete(sections[-1])):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    result: dict[str, Any] = {}
    for head, body in sections:
        m = re.match(r"^@(.*?)(?:\[(\d+)\]|\{(\d+)\})?$", head)
        name, n, keyed = m.group(1), m.group(2), m.group(3)
        if n is not None or keyed is not None:
            cols = body[0].split(",")
            rows = []
            keys = []
            for cells in (_split_row(line) for line in body[1:]):
                if keyed is not None:
                    keys.append(_parse_cell(cells[0])[1] if cells[0] else "")
                    keys[-1] = cells[0] if not isinstance(keys[-1], str) else keys[-1]
                    cells, use_cols = cells[1:], cols[1:]
                else:
                    use_cols = cols
                pairs = []
                for c, cell in zip(use_cols, cells):
                    present, v = _parse_cell(cell)
                    if present:
                        pairs.append((c, v))
                rows.append(_unflatten(pairs))
            val: Any = dict(zip(keys, rows)) if keyed is not None else rows
        else:
            val = json.loads("\n".join(body))
        if name == "" and len(sections) == 1:
            return val
        result[name] = val
    return result


def _section_complete(section: tuple[str, list[str]]) -> bool:
    head, body = section
    m = re.match(r"^@(.*?)(?:\[(\d+)\]|\{(\d+)\})?$", head)
    if m and (m.group(2) or m.group(3)) is not None:
        return len(body) >= int(m.group(2) or m.group(3)) + 1
    if not body:
        return False
    try:
        json.loads("\n".join(body))
        return True
    except json.JSONDecodeError:
        return False


PRIMER = (
    "Data is split into sections. '@name[N]' starts a table of N records: a header row of "
    "column names (dots mean nesting, e.g. customer.name), then one comma-separated row per "
    "record. Cells are plain text or JSON values; an empty cell means the field is absent. "
    "'@name{N}' is a map of N entries laid out the same way, where the first cell of each row "
    "(header '*') is the entry's key. '@name' alone is followed by a JSON value."
)
