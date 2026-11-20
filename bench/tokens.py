"""Token-efficiency comparison on the TOON benchmark's token datasets (no LLM calls).

Usage: python tokens.py out/seed-12345 [out/seed-101 ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import ctab  # noqa: E402
import hook  # noqa: E402

import os
ENC = tiktoken.get_encoding(os.environ.get("ENC", "o200k_base"))


def ntok(s: str) -> int:
    return len(ENC.encode(s, disallowed_special=()))


def repo_best(data) -> str:
    """Best repo candidate (advanced tier) applied to the dataset's inner list, else compact JSON."""
    inner = None
    if isinstance(data, dict) and len(data) == 1:
        (k, v), = data.items()
        if isinstance(v, list):
            inner = v
    value = inner if inner is not None else data
    src = hook.SourceData(path=Path("x.json"), kind="json", value=value, raw_text=hook.compact_json(value))
    best = None
    for c in hook.validated_candidates(src, hook.CANDIDATE_TIERS["advanced"]):
        t = ntok(c.instructions + "\n" + c.text if c.instructions else c.text)
        if best is None or t < best[0]:
            best = (t, c.name)
    return best


def main() -> None:
    fmts = ["json-pretty", "json-compact", "yaml", "xml", "toon", "csv"]
    for d in sys.argv[1:]:
        rows = json.load(open(Path(d) / "token-datasets.json"))
        print(f"\n== {d}")
        hdr = f"{'dataset':14}" + "".join(f"{f:>13}" for f in fmts) + f"{'repo-best':>18}{'ctab':>9}"
        print(hdr)
        totals: dict[str, int] = {}
        for r in rows:
            enc = r["encodings"]
            c = ctab.encode(r["data"])
            assert ctab.decode(c) == r["data"], f"ctab roundtrip failed on {r['name']}"
            line = f"{r['name']:14}"
            for f in fmts:
                t = ntok(enc[f]) if f in enc else None
                line += f"{t if t is not None else '-':>13}"
                if t is not None:
                    totals[f] = totals.get(f, 0) + t
            rb = repo_best(r["data"])
            line += f"{rb[0]:>9} {rb[1][:8]:>8}"
            ct = ntok(c)
            line += f"{ct:>9}"
            totals["repo"] = totals.get("repo", 0) + rb[0]
            totals["ctab"] = totals.get("ctab", 0) + ct
            print(line)
        print("TOTAL", {k: v for k, v in totals.items() if k != "csv"})


if __name__ == "__main__":
    main()
