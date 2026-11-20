"""ctab v2: lossless "tables where possible, compact JSON elsewhere" encoding.

Design goals, in priority order:
  1. Lossless: decode(encode(x)) == x for any JSON value (callers still verify).
  2. Familiar: compact JSON plus CSV-like tables with a header row.
  3. Reader-friendly: explicit record counts, grouped nested headers, and
     redundant list-length columns ("#items") that the decoder verifies and drops.
     These cost a few tokens but remove counting work the model is bad at.
  4. Few tokens: object keys appear once per table; nested objects are flattened
     into grouped columns instead of being indented.

Grammar
  section := "@" name "[" N "]" NL header NL row{N}            list of records
           | "@" name "{" N "}" NL header NL (key ": " row){N}   map of records
           | "@" name NL json                                   anything else
  header  := col ("," col)*   col := name | name "{" header "}" | "#" name
  cell    := ""   key absent  | bare text -> string | JSON value

Benchmark evidence and the v1 -> v2 history live in bench/FINDINGS.md.
"""

from __future__ import annotations

import json
import re
from typing import Any

_RESERVED = {"true", "false", "null", ""}
_BAD_KEY_CHARS = set(',{}.#\n\r":')
_DEC = json.JSONDecoder()
_HEAD = re.compile(r"^@(.*?)(?:\[(\d+)\]|\{(\d+)\})?$")

PRIMER = (
    "Data is in sections. '@name[N]' starts a table of N records: a header row, then one "
    "comma-separated row per record in original order. A header like customer{id,name} means "
    "nested fields customer.id and customer.name. A '#items' column gives the length of the "
    "list in the 'items' column. Cells are plain text or JSON values; an empty cell means the "
    "field is absent. '@name{N}' is a map of N entries: each row is 'key: values'. "
    "'@name' alone is followed by a JSON value."
)


def _cj(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- encoding

def _flatten(obj: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    flat: dict[tuple[str, ...], Any] = {}
    for key, val in obj.items():
        path = prefix + (key,)
        if isinstance(val, dict) and val:
            flat.update(_flatten(val, path))
        else:
            flat[path] = val
    return flat


def _key_ok(k: str) -> bool:
    return bool(k) and not (set(k) & _BAD_KEY_CHARS) and not k.startswith("@") and k == k.strip()


def _columns(flats: list[dict]) -> list[tuple[str, ...]] | None:
    cols = list(dict.fromkeys(p for f in flats for p in f))
    if not all(_key_ok(part) for c in cols for part in c):
        return None
    colset = set(cols)
    if any(c[:i] in colset for c in cols for i in range(1, len(c))):
        return None  # a path is a leaf in one row and a group in another
    # Grouped header rendering needs each group's columns to be contiguous.
    spans: dict[tuple[str, ...], list[int]] = {}
    for idx, c in enumerate(cols):
        for i in range(1, len(c)):
            spans.setdefault(c[:i], []).append(idx)
    if any(ix[-1] - ix[0] + 1 != len(ix) for ix in spans.values()):
        return None
    return cols


def _header(cols: list[tuple[str, ...]], counted: set[tuple[str, ...]]) -> str:
    def render(items: list[tuple[str, ...]], depth: int) -> str:
        out, i = [], 0
        while i < len(items):
            c = items[i]
            if len(c) == depth + 1:
                if c in counted:
                    out.append("#" + c[depth])
                out.append(c[depth])
                i += 1
            else:
                j = i
                while j < len(items) and len(items[j]) > depth + 1 and items[j][depth] == c[depth]:
                    j += 1
                out.append(f"{c[depth]}{{{render(items[i:j], depth + 1)}}}")
                i = j
        return ",".join(out)
    return render(cols, 0)


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
            and not any(ch in value for ch in ",\n\r")
        ):
            return value
    return _cj(value)


def _row(flat: dict, cols: list[tuple[str, ...]], counted: set) -> str:
    cells = []
    for c in cols:
        if c in counted:
            v = flat.get(c)
            cells.append(str(len(v)) if isinstance(v, list) else "")
        cells.append(_cell(flat.get(c), c in flat))
    return ",".join(cells)


def _table_parts(rows: list[Any]):
    if not rows or not all(isinstance(r, dict) for r in rows):
        return None
    flats = [_flatten(r) for r in rows]
    cols = _columns(flats)
    if not cols:
        return None
    if sum(len(f) for f in flats) < 0.6 * len(rows) * len(cols):
        return None  # mostly holes: very heterogeneous records
    counted = {c for c in cols if any(isinstance(f.get(c), list) and f.get(c) for f in flats)}
    return _header(cols, counted), [_row(f, cols, counted) for f in flats]


def _key_cell(k: str) -> str:
    if k and ": " not in k and not k.startswith(('"', "@")) and "\n" not in k and k == k.strip() and not k.endswith(":"):
        return k
    return _cj(k)


def _section(name: str, val: Any) -> str:
    if isinstance(val, list) and len(val) >= 2:
        parts = _table_parts(val)
        if parts:
            header, rows = parts
            return f"@{name}[{len(val)}]\n{header}\n" + "\n".join(rows)
    if isinstance(val, dict) and len(val) >= 2 and all(isinstance(v, dict) and v for v in val.values()):
        parts = _table_parts(list(val.values()))
        if parts:
            header, rows = parts
            body = [f"{_key_cell(k)}: {r}" for k, r in zip(val, rows)]
            return f"@{name}{{{len(val)}}}\n{header}\n" + "\n".join(body)
    return f"@{name}\n{_cj(val)}"


def encode(value: Any) -> str:
    if isinstance(value, dict) and value and all(k and not any(ch in k for ch in "\n[{") for k in value):
        return "\n".join(_section(k, v) for k, v in value.items())
    return _section("", value)


# ---------------------------------------------------------------- decoding

def _parse_header(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Physical cell layout: ("count" | "value", path) per cell."""
    pos = 0
    layout: list[tuple[str, tuple[str, ...]]] = []

    def parse(prefix: tuple[str, ...]) -> None:
        nonlocal pos
        while True:
            name = re.match(r"[^,{}]*", text[pos:]).group(0)
            pos += len(name)
            if pos < len(text) and text[pos] == "{":
                pos += 1
                parse(prefix + (name,))
                pos += 1  # closing }
            elif name.startswith("#"):
                layout.append(("count", prefix + (name[1:],)))
            else:
                layout.append(("value", prefix + (name,)))
            if pos < len(text) and text[pos] == ",":
                pos += 1
                continue
            return

    parse(())
    return layout


def _split_row(line: str, start: int = 0) -> list[str]:
    cells, i, n = [], start, len(line)
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


def _parse_cell(text: str) -> tuple[bool, Any]:
    if text == "":
        return False, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        return True, text


def _decode_row(cells: list[str], layout: list) -> dict[str, Any]:
    out: dict[str, Any] = {}
    counts: dict[tuple[str, ...], int] = {}
    for (kind, path), cell in zip(layout, cells):
        if kind == "count":
            if cell:
                counts[path] = int(cell)
            continue
        present, v = _parse_cell(cell)
        if not present:
            continue
        if path in counts and (not isinstance(v, list) or len(v) != counts[path]):
            raise ValueError(f"length column mismatch for {'.'.join(path)}")
        cur = out
        for p in path[:-1]:
            cur = cur.setdefault(p, {})
        cur[path[-1]] = v
    return out


def _complete(head: str, body: list[str]) -> bool:
    m = _HEAD.match(head)
    if m and (m.group(2) or m.group(3)) is not None:
        return len(body) >= int(m.group(2) or m.group(3)) + 1
    if not body:
        return False
    return _parses_as_json("\n".join(body))


def decode(text: str) -> Any:
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("@") and (not sections or _complete(*sections[-1])):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    result: dict[str, Any] = {}
    for head, body in sections:
        m = _HEAD.match(head)
        name, n, keyed = m.group(1), m.group(2), m.group(3)
        if n is not None or keyed is not None:
            layout = _parse_header(body[0])
            if keyed is not None:
                val: Any = {}
                for line in body[1:]:
                    if line.startswith('"'):
                        key, end = _DEC.raw_decode(line, 0)
                    else:
                        end = line.index(": ")
                        key = line[:end]
                    val[key] = _decode_row(_split_row(line, end + 2), layout)
            else:
                val = [_decode_row(_split_row(line), layout) for line in body[1:]]
        else:
            val = json.loads("\n".join(body))
        if name == "" and len(sections) == 1:
            return val
        result[name] = val
    return result
