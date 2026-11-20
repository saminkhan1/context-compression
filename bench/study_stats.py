"""Study statistics: strict + lenient accuracy, tokens, cost under stated schedules, and
cluster-robust paired comparisons (cluster = seed x dataset).

Usage: python study_stats.py ARM_A ARM_B [--sets large-777 ...] [--margin 0.03]
       python study_stats.py --table --sets large-777 [--arms a b c]
Reads results2/<set>/<arm>.graded.jsonl (grade.ts output).
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

HERE = Path(__file__).parent
P_IN, P_OUT = 1.0, 5.0          # $/M tokens, claude-haiku-4-5 list price
P_CACHE_READ = 0.1              # x input price


def load(set_name: str, arm: str) -> dict[str, dict]:
    f = HERE / "results2" / set_name / f"{arm}.graded.jsonl"
    return {json.loads(l)["id"]: {**json.loads(l), "set": set_name} for l in open(f)} if f.exists() else {}


def raw_tokens(r: dict) -> int:
    return sum(c["input"] + c["cache_write"] + c["cache_read"] + c["output"] for c in r["calls"])


def cost_flat(r: dict) -> float:
    """No caching anywhere: every input token at list price (what the stateless runs paid)."""
    return sum((c["input"] + c["cache_write"] + c["cache_read"]) * P_IN + c["output"] * P_OUT for c in r["calls"]) / 1e6


def cost_cached(r: dict) -> float:
    """Schedule most favourable to multi-turn agents: the previous turn's prompt+output is re-read at 0.1x.
    Native-tool arm: its logged cache_read at 0.1x, everything else at 1x."""
    calls, total, prev = r["calls"], 0.0, 0
    if len(calls) == 1:
        c = calls[0]
        return ((c["input"] + c["cache_write"]) * P_IN + c["cache_read"] * P_IN * P_CACHE_READ + c["output"] * P_OUT) / 1e6
    for c in calls:
        tot_in = c["input"] + c["cache_write"] + c["cache_read"]
        cached = min(prev, tot_in)
        total += (cached * P_IN * P_CACHE_READ + (tot_in - cached) * P_IN + c["output"] * P_OUT)
        prev = tot_in + c["output"]
    return total / 1e6


def wilson(k: int, n: int, z: float = 1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def mcnemar(a: dict, b: dict):
    ids = sorted(set(a) & set(b))
    x = sum(1 for i in ids if a[i]["isCorrect"] and not b[i]["isCorrect"])
    y = sum(1 for i in ids if b[i]["isCorrect"] and not a[i]["isCorrect"])
    n = x + y
    tail = sum(math.comb(n, i) for i in range(min(x, y) + 1))
    return x, y, (min(1.0, 2 * tail / 2**n) if n else 1.0)


def clusters(a: dict, b: dict):
    ids = sorted(set(a) & set(b), key=str)
    by: dict[tuple, list] = {}
    for i in ids:
        by.setdefault((a[i]["set"], a[i]["dataset"]), []).append(i)
    return by


def boot(a: dict, b: dict, key: str, n_boot: int = 5000, seed: int = 1):
    """Cluster bootstrap over (set, dataset). Returns (acc_diff a-b, token_ratio b/a) draws."""
    by = clusters(a, b)
    keys = list(by)
    stat = {k: (
        sum(a[i][key] for i in by[k]), sum(b[i][key] for i in by[k]), len(by[k]),
        sum(raw_tokens(a[i]) for i in by[k]), sum(raw_tokens(b[i]) for i in by[k]),
        sum(cost_flat(a[i]) for i in by[k]), sum(cost_flat(b[i]) for i in by[k]),
        sum(cost_cached(a[i]) for i in by[k]), sum(cost_cached(b[i]) for i in by[k]),
    ) for k in keys}
    rnd = random.Random(seed)
    out = {"dacc": [], "tok": [], "flat": [], "cached": []}
    for _ in range(n_boot):
        s = [stat[rnd.choice(keys)] for _ in keys]
        n = sum(x[2] for x in s)
        out["dacc"].append((sum(x[0] for x in s) - sum(x[1] for x in s)) / n)
        out["tok"].append(sum(x[4] for x in s) / sum(x[3] for x in s))
        out["flat"].append(sum(x[6] for x in s) / sum(x[5] for x in s))
        out["cached"].append(sum(x[8] for x in s) / sum(x[7] for x in s))
    return out


def signflip_p(a: dict, b: dict, key: str, n_perm: int = 100000, seed: int = 2) -> float:
    by = clusters(a, b)
    d = [sum(a[i][key] - b[i][key] for i in v) / len(v) for v in by.values()]
    obs = abs(sum(d) / len(d))
    rnd = random.Random(seed)
    hits = sum(abs(sum(x * rnd.choice((-1, 1)) for x in d) / len(d)) >= obs - 1e-12 for _ in range(n_perm))
    return (hits + 1) / (n_perm + 1)


def pct(v, lo=2.5, hi=97.5):
    v = sorted(v)
    return v[int(len(v) * lo / 100)], v[min(len(v) - 1, int(len(v) * hi / 100))]


def summarize(arm: str, recs: dict) -> str:
    rs = list(recs.values())
    n = len(rs)
    if not n:
        return f"{arm:<18} (no data)"
    k = sum(r["isCorrect"] for r in rs)
    lo, hi = wilson(k, n)
    kl = sum(r.get("isCorrectLenient", r["isCorrect"]) for r in rs)
    err = sum(1 for r in rs if r.get("error"))
    return (f"{arm:<18} n={n:<5} strict {100*k/n:5.1f}% [{100*lo:4.1f},{100*hi:4.1f}]  lenient {100*kl/n:5.1f}%  "
            f"tok/Q {sum(map(raw_tokens, rs))/n:>8,.0f}  $/1kQ flat {1000*sum(map(cost_flat, rs))/n:6.2f} "
            f"cached {1000*sum(map(cost_cached, rs))/n:6.2f}  turns {sum((r.get('turns') or 1) for r in rs)/n:3.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="*")
    ap.add_argument("--sets", nargs="+", default=["large-777"])
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--margin", type=float, default=0.03)
    args = ap.parse_args()
    if args.table:
        for arm in args.arms:
            recs = {}
            for s in args.sets:
                recs.update({f"{s}:{i}": r for i, r in load(s, arm).items()})
            print(summarize(arm, recs))
        return
    A, B = args.arms
    a, b = {}, {}
    for s in args.sets:
        a.update({f"{s}:{i}": r for i, r in load(s, A).items()})
        b.update({f"{s}:{i}": r for i, r in load(s, B).items()})
    ids = set(a) & set(b)
    print(f"A={A}  B={B}  paired n={len(ids)}  clusters={len(clusters(a, b))}")
    for key in ("isCorrect", "isCorrectLenient"):
        for r in a.values():
            r.setdefault("isCorrectLenient", r["isCorrect"])
        for r in b.values():
            r.setdefault("isCorrectLenient", r["isCorrect"])
        o = boot(a, b, key)
        lo, hi = pct(o["dacc"])
        x, y, p = mcnemar({i: a[i] for i in ids}, {i: b[i] for i in ids})
        one_lo = pct(o["dacc"], 5, 95)[0]
        print(f"{key:<17} acc diff A-B {100*sum(o['dacc'])/len(o['dacc']):+5.1f}pp  cluster95% [{100*lo:+.1f},{100*hi:+.1f}]  "
              f"sign-flip p={signflip_p(a, b, key):.4f}  (question-level McNemar {x}/{y} p={p:.3g}, anti-conservative)  "
              f"non-inferior@{100*args.margin:.0f}pp (one-sided 95% lower {100*one_lo:+.1f}): {one_lo > -args.margin}")
    for name, label in (("tok", "raw tokens"), ("flat", "$ no-cache"), ("cached", "$ ideal-cache")):
        lo, hi = pct(o[name])
        print(f"B/A {label:<14} {sum(o[name])/len(o[name]):6.2f}x  cluster95% [{lo:.2f}, {hi:.2f}]")


if __name__ == "__main__":
    main()
