"""Token-efficiency comparison on the TOON benchmark's token datasets (no LLM calls).

Usage: python tokens.py out/seed-12345 [out/seed-101 ...]
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import ctab  # noqa: E402
import hook  # noqa: E402

ENC = tiktoken.get_encoding(os.environ.get("ENC", "o200k_base"))


def ntok(s: str) -> int:
    return len(ENC.encode(s, disallowed_special=()))


def repo_best(data) -> tuple[int, str]:
    """(tokens, name) of the best repo candidate (advanced tier) on the dataset's inner list."""
    if isinstance(data, dict) and len(data) == 1 and isinstance(next(iter(data.values())), list):
        data = next(iter(data.values()))
    src = hook.SourceData(
        path=Path("x.json"), kind="json", value=data, raw_text=hook.compact_json(data)
    )
    scored = [
        (ntok(c.instructions + "\n" + c.text if c.instructions else c.text), c.name)
        for c in hook.validated_candidates(src, hook.CANDIDATE_TIERS["advanced"])
    ]
    return min(scored, key=lambda item: item[0])


def main() -> None:
    fmts = ["json-pretty", "json-compact", "yaml", "xml", "toon", "csv"]
    for d in sys.argv[1:]:
        rows = json.loads((Path(d) / "token-datasets.json").read_text())
        print(f"\n== {d}")
        header = "".join(f"{f:>13}" for f in fmts)
        print(f"{'dataset':14}{header}{'repo-best':>18}{'ctab':>9}")
        totals = Counter()
        for r in rows:
            c = ctab.encode(r["data"])
            assert ctab.decode(c) == r["data"], f"ctab roundtrip failed on {r['name']}"
            line = f"{r['name']:14}"
            for f in fmts:
                if f in r["encodings"]:
                    t = ntok(r["encodings"][f])
                    totals[f] += t
                    line += f"{t:>13}"
                else:
                    line += f"{'-':>13}"
            rb_tokens, rb_name = repo_best(r["data"])
            ct = ntok(c)
            totals["repo"] += rb_tokens
            totals["ctab"] += ct
            print(f"{line}{rb_tokens:>9} {rb_name[:8]:>8}{ct:>9}")
        print("TOTAL", {k: v for k, v in totals.items() if k != "csv"})


if __name__ == "__main__":
    main()
