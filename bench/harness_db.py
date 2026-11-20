"""Independent-benchmark evaluation on DataBench (cardiffnlp/databench, SemEval-2025 Task 8).

Questions, tables and the official comparator (databench_eval.Evaluator.default_compare)
are used unchanged. We only decide how the model sees the table. Arms mirror harness.py:

  table            whole table in context as CSV (only tables <= IN_CONTEXT_MAX_TOKENS)
  code             preview + single-shot pandas script
  agent            preview + lean text-protocol agent (python only, <= 6 executions)
  agent-nopreview  lean agent, no preview

Splits are fixed in split.json (seeded): `dev` datasets were used to adapt loading and answer
formats; `test` datasets are run once with the frozen harness.

Usage: python harness_db.py <db_dir> <dev|test> ARM [ARM...] [--workers 8] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import harness as H  # noqa: E402

IN_CONTEXT_MAX_TOKENS = 10_000
FORMAT_RULES = """Answer format (the answer type is {type}):
- boolean: True or False
- number: a plain number
- category: the exact value from the data
- list[category] / list[number]: a Python list such as ['a', 'b'] or [1, 2]"""


def table_preview(df: pd.DataFrame) -> str:
    cols = []
    for c in df.columns:
        label = f"{c}:{df[c].dtype}"
        nulls = int(df[c].isna().sum())
        if nulls:
            label += f" ({nulls} nulls)"
        cols.append(label)
    return (
        f'pandas table in ./data.parquet (load with pd.read_parquet("data.parquet")): '
        f"{len(df)} rows x {df.shape[1]} columns\n"
        f"columns in order: {', '.join(cols)}\nfirst 2 rows:\n{df.head(2).to_csv(index=False)}"
    ).strip()


def code_prompt(summary: str, question: str, qtype: str) -> str:
    return f"""A table is stored in ./data.parquet. Summary:

{summary}

Question: {question}

Write a short Python 3 script that loads the table with pandas, computes the answer, and prints ONLY
the answer.
{FORMAT_RULES.format(type=qtype)}
Reply with only a ```python code block.""".strip()


def agent_prompt(summary: str | None, question: str, qtype: str) -> str:
    block = f"\n\nSummary of the file:\n{summary}" if summary else ""
    return f"""A table is stored in ./data.parquet (load it with pandas: pd.read_parquet("data.parquet")), in the
directory where your code runs.{block}

Question: {question}

You may run Python 3 to inspect the table and compute the answer. To run code, reply with exactly one
```python code block and nothing else; you will receive its printed output (truncated to {H.OUT_TRUNC}
characters). You can run code up to {H.MAX_EXEC} times. When you are sure of the answer, reply with a
single line:
FINAL: <answer>

{FORMAT_RULES.format(type=qtype)}""".strip()


def table_prompt(csv: str, question: str, qtype: str) -> str:
    return f"""Given the following table in CSV format:

```csv
{csv}
```

Question: {question}

Provide only the answer, no explanation.
{FORMAT_RULES.format(type=qtype)}

Answer:""".strip()


def run_arm(arm: str, q: dict, db: Path) -> dict | None:
    df = pd.read_parquet(db / "tables" / f"{q['dataset']}.parquet")
    question, qtype = q["question"], q["type"]
    with tempfile.TemporaryDirectory() as tmp:
        if arm == "table":
            d = H.cli(table_prompt(df.to_csv(index=False), question, qtype), tmp)
            if "error" in d:
                return {"error": d["error"]}
            return {"actual": (d.get("result") or "").strip(), "calls": [H.usage_of(d)], "turns": 1}
        df.to_parquet(Path(tmp, "data.parquet"))
        if arm == "code":
            d = H.cli(code_prompt(table_preview(df), question, qtype), tmp)
            if "error" in d:
                return {"error": d["error"]}
            reply = d.get("result") or ""
            block = H.first_python_block(reply)
            out = H.run_python(block if block else reply, tmp, timeout=120)
            return {"actual": out, "calls": [H.usage_of(d)], "turns": 1, "transcript": reply,
                    "exec_error": out.startswith("ERROR")}
        if arm in ("agent", "agent-nopreview"):
            task = agent_prompt(table_preview(df) if arm == "agent" else None, question, qtype)
            convo, calls, final, n_exec = "", [], None, 0
            for _ in range(H.MAX_EXEC + 1):
                d = H.cli(task + convo, tmp)
                if "error" in d:
                    return {"error": d["error"], "calls": calls}
                calls.append(H.usage_of(d))
                reply = (d.get("result") or "").strip()
                block = H.first_python_block(reply)
                if block is not None and n_exec < H.MAX_EXEC:
                    n_exec += 1
                    out = H.run_python(block, tmp, timeout=120)
                    shown = out if len(out) <= H.OUT_TRUNC else out[: H.OUT_TRUNC] + "\n...[truncated]"
                    convo += f"\n\n[your reply]\n{reply}\n\n[execution output]\n{shown}"
                    continue
                m = re.findall(r"^\s*FINAL:\s*(.*)$", reply, re.M)
                final = m[-1].strip() if m else reply
                break
            return {"actual": final if final is not None else "", "calls": calls, "turns": len(calls),
                    "executions": n_exec, "transcript": convo, "hit_limit": final is None}
    raise SystemExit(f"unknown arm {arm}")


def grade(rec: dict) -> bool:
    from databench_eval import Evaluator

    ev = Evaluator.__new__(Evaluator)
    return bool(Evaluator.default_compare(ev, rec["actual"], rec["expected"], rec["type"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db_dir")
    ap.add_argument("split")
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    db = Path(args.db_dir)
    split = json.loads((db / "split.json").read_text())
    names = set(split[args.split])
    qa = [r for r in json.loads((db / "qa.json").read_text()) if r["dataset"] in names]
    for i, r in enumerate(qa):
        r["id"] = f"{r['dataset']}#{i}"
    if args.limit:
        qa = qa[: args.limit]
    outdir = HERE / "results2" / f"databench-{args.split}"
    outdir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    for arm in args.arms:
        out = outdir / f"{arm}.jsonl"
        done = {json.loads(line)["id"] for line in open(out)} if out.exists() else set()
        todo = [q for q in qa if q["id"] not in done]
        if arm == "table":
            todo = [q for q in todo if split["csv_tokens"][q["dataset"]] <= IN_CONTEXT_MAX_TOKENS]
        print(f"{arm}: {len(todo)} calls", flush=True)

        def work(q, arm=arm, out=out):
            r = run_arm(arm, q, db)
            if r is None:
                return
            rec = {"id": q["id"], "arm": arm, "dataset": q["dataset"], "type": q["type"],
                   "expected": q["answer"], "fingerprint": H.prompt_fingerprint(), **r}
            rec["isCorrect"] = False if r.get("error") else grade(rec)
            rec["isCorrectLenient"] = rec["isCorrect"]
            with lock, open(out.with_suffix(".graded.jsonl"), "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            with lock, open(out, "a") as fh:
                fh.write(json.dumps({"id": q["id"]}) + "\n")

        with ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(work, todo))


if __name__ == "__main__":
    main()
