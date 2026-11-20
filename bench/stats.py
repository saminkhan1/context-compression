"""Summarize graded results: accuracy (Wilson 95% CI), tokens, paired exact McNemar vs baselines.

Usage: python stats.py results/seed-12345
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def mcnemar(a: dict, b: dict) -> tuple[int, int, int, float]:
    ids = sorted(set(a) & set(b))
    a_only = sum(1 for i in ids if a[i]["isCorrect"] and not b[i]["isCorrect"])
    b_only = sum(1 for i in ids if b[i]["isCorrect"] and not a[i]["isCorrect"])
    n = a_only + b_only
    # Exact two-sided binomial test at p=0.5 (symmetric, so twice the smaller tail, capped at 1).
    tail = sum(math.comb(n, i) for i in range(min(a_only, b_only) + 1))
    return len(ids), a_only, b_only, min(1.0, 2 * tail / 2**n)


def accuracy(rs: list[dict]) -> float:
    return sum(r["isCorrect"] for r in rs) / len(rs) if rs else float("nan")


def main() -> None:
    arms = {
        f.name.removesuffix(".graded.jsonl"): {
            r["id"]: r
            for r in (json.loads(line) for line in f.open() if line.strip())
        }
        for f in sorted(Path(sys.argv[1]).glob("*.graded.jsonl"))
    }
    print(f"{'arm':12}{'n':>5}{'acc':>8}{'95% CI':>16}{'avg in tok':>12}{'avg $':>9}{'errors':>8}")
    for name, recs in arms.items():
        rs = list(recs.values())
        k = sum(r["isCorrect"] for r in rs)
        lo, hi = wilson(k, len(rs))
        tok = sum(r.get("input_tokens", 0) for r in rs) / len(rs)
        cost = sum(r.get("cost_usd") or 0 for r in rs) / len(rs)
        err = sum(1 for r in rs if r.get("error") or r.get("is_error"))
        ci = f"[{lo:.1%},{hi:.1%}]"
        print(f"{name:12}{len(rs):>5}{k / len(rs):>8.1%}{ci:>16}{tok:>12.0f}{cost:>9.4f}{err:>8}")

    for field, title, width, label_width in [
        ("type", "question type", 16, 14),
        ("dataset", "dataset", 13, 12),
    ]:
        print(f"\nBy {title} (accuracy):")
        keys = sorted({r[field] for recs in arms.values() for r in recs.values()})
        print(f"{'arm':12}" + "".join(f"{key[:label_width]:>{width}}" for key in keys))
        for name, recs in arms.items():
            row = f"{name:12}"
            for key in keys:
                acc = accuracy([r for r in recs.values() if r[field] == key])
                row += f"{acc:>{width}.1%}"
            print(row)

    print("\nPaired exact McNemar (A-only correct / B-only correct, two-sided p):")
    names = list(arms)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            n, ao, bo, p = mcnemar(arms[a], arms[b])
            print(f"  {a:>12} vs {b:<12} n={n:<4} {ao:>3} / {bo:<3} p={p:.3g}")


if __name__ == "__main__":
    main()
