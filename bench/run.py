"""Run TOON-benchmark retrieval questions through the claude CLI (Haiku, no thinking).

Arms:
  json-pretty, toon, ...  : data in context, prompt identical to TOON's evaluate.ts
  ctab                    : our encoding, same prompt template with ctab primer
  codebook                : the original repo's codebook-json candidate (flat datasets only)
  tool                    : harness arm - schema + 2-row preview in context, full data on
                            disk, model may run python3 to answer (Bash restricted to python3)

Results append to results/<seed>/<arm>.jsonl; already-answered questions are skipped,
so an interrupted run resumes without re-spending calls.

Usage: python run.py out/seed-12345 ARM [ARM...] [--limit N] [--workers 6]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ctab  # noqa: E402  (v2)
import ctab_v1  # noqa: E402
import hook  # noqa: E402

MODEL = "claude-haiku-4-5"
BASE = [
    "claude", "-p", "--model", MODEL,
    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
    "--settings", '{"alwaysThinkingEnabled":false}', "--output-format", "json",
]
SYSTEM = "You are a precise data analyst. Follow the answer format exactly."
ANSWER_RULES = """Answer format requirements:
- Provide only the value itself, no explanation
- For numbers: output digits only (no commas, currency symbols, or units)
- For dates/field names: use the exact string from the data
- For lists: output comma-separated values with no spaces"""

CODEBOOK_PRIMER = (
    "codebook-json: JSON array [columns, dictionaries, rows]. dictionaries=[[columnIndex,[values...]],...]; "
    "in rows, a column that has a dictionary stores the index into that dictionary's values instead of the value."
)


def inline_prompt(primer: str, name: str, fence: str, data: str, question: str) -> str:
    return f"""{primer}

Given the following data in {name} format:

```{fence}
{data}
```

Question: {question}

{ANSWER_RULES}

Answer:""".strip()


def schema_preview(data) -> str:
    """Deterministic summary: shape, counts, top-level fields (nested shown as name{sub,...}),
    optional-field counts, and 2 sample records in ctab. Never sees the question."""
    def typ(v):
        return {bool: "bool", int: "int", float: "number", str: "string", type(None): "null",
                list: "array", dict: "object"}[type(v)]

    def fields_of(rows: list[dict]) -> str:
        order: dict[str, None] = {}
        types: dict[str, set] = {}
        present: dict[str, int] = {}
        subs: dict[str, dict[str, None]] = {}
        for row in rows:
            for k, v in row.items():
                order.setdefault(k)
                types.setdefault(k, set()).add(typ(v))
                present[k] = present.get(k, 0) + 1
                if isinstance(v, dict):
                    for sk in v:
                        subs.setdefault(k, {}).setdefault(sk)
        parts = []
        for k in order:
            label = f"{k}{{{','.join(subs[k])}}}" if k in subs else k
            label += ":" + "|".join(sorted(types[k]))
            if present[k] < len(rows):
                label += f" (optional, in {present[k]}/{len(rows)})"
            parts.append(label)
        return ", ".join(parts)

    lines = []
    items = data.items() if isinstance(data, dict) else [("", data)]
    for key, val in items:
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            lines.append(f'data["{key}"]: list of {len(val)} objects; top-level fields in order: {fields_of(val)}')
            lines.append("first 2 records:\n" + ctab.encode({key: val[:2]}))
        elif isinstance(val, dict) and len(val) > 3 and all(isinstance(v, dict) for v in val.values()):
            lines.append(f'data["{key}"]: object with {len(val)} keyed entries (key -> record); record fields in order: '
                         f'{fields_of(list(val.values()))}')
            lines.append("first 2 entries:\n" + ctab.encode({key: dict(list(val.items())[:2])}))
        else:
            text = json.dumps(val, ensure_ascii=False)
            lines.append(f'data["{key}"]: {typ(val)}' + (f" = {text}" if len(text) < 1500 else f" ({len(text)} chars)"))
    return "\n".join(lines)


def tool_prompt(data, question: str) -> str:
    return f"""The full dataset is in ./data.json (load it with Python's json module). Summary:

{schema_preview(data)}

Question: {question}

Use python3 (via the Bash tool) to compute the answer from ./data.json rather than guessing. Keep scripts short.
When done, reply with ONLY the final answer.

{ANSWER_RULES}""".strip()


def code_prompt(data, question: str) -> str:
    return f"""A dataset is stored in ./data.json. Summary:

{schema_preview(data)}

Question: {question}

Write a short Python 3 script that loads ./data.json with the json module, computes the answer,
and prints ONLY the answer. Printed answer format:
- numbers: plain number with no commas, currency symbols, or units; keep decimals
  (do not truncate or round unless the question asks)
- dates/field names: the exact string from the data
- lists: comma-separated values with no spaces
Reply with only a ```python code block.""".strip()


def nopreview_prompt(question: str) -> str:
    return f"""A dataset is stored in ./data.json.

Question: {question}

Use the Bash tool (python3, jq, head, grep, etc.) to inspect ./data.json and compute the answer.
When done, reply with ONLY the final answer.

{ANSWER_RULES}""".strip()


def codebook_text(data) -> str | None:
    if not (isinstance(data, dict) and len(data) == 1):
        return None
    (_, rows), = data.items()
    if not (isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows)):
        return None
    headers = hook.stable_headers(rows)
    if not hook.rows_are_uniform(rows, headers):
        return None
    return hook.codebook_json_text(rows, headers)


def build_task(arm: str, q: dict, ds: dict, formats: dict):
    data = ds["data"]
    if arm == "code":
        return {"prompt": code_prompt(data, q["prompt"]), "files": {"data.json": json.dumps(data)}, "execute": True}
    if arm == "tool-nopreview":
        return {"prompt": nopreview_prompt(q["prompt"]), "files": {"data.json": json.dumps(data, indent=2)}, "tools": "broad"}
    if arm == "tool":
        return {"prompt": tool_prompt(data, q["prompt"]), "files": {"data.json": json.dumps(data)}, "tools": True}
    if arm in ("ctab", "ctab2"):
        mod = ctab_v1 if arm == "ctab" else ctab
        text = mod.encode(data)
        assert mod.decode(text) == data
        return {"prompt": inline_prompt(mod.PRIMER, "ctab", "text", text, q["prompt"])}
    if arm == "codebook":
        text = codebook_text(data)
        if text is None:
            return None
        return {"prompt": inline_prompt(CODEBOOK_PRIMER, "codebook-json", "json", text, q["prompt"])}
    if arm not in ds["encodings"]:
        return None
    f = formats[arm]
    return {"prompt": inline_prompt(f["primer"], arm, f["fence"], ds["encodings"][arm], q["prompt"])}


def call(task: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in task.get("files", {}).items():
            Path(tmp, name).write_text(content)
        cmd = BASE + ["--system-prompt", SYSTEM]
        if task.get("tools") == "broad":
            cmd += ["--tools", "Bash", "--allowedTools", "Bash(python3:*)", "Bash(cat:*)", "Bash(head:*)",
                    "Bash(tail:*)", "Bash(jq:*)", "Bash(wc:*)", "Bash(grep:*)", "--max-turns", "12"]
        elif task.get("tools"):
            cmd += ["--tools", "Bash", "--allowedTools", "Bash(python3:*)", "--max-turns", "8"]
        else:
            cmd += ["--tools", ""]
        env = {**os.environ, "MAX_THINKING_TOKENS": "0"}
        p = subprocess.run(cmd, input=task["prompt"], capture_output=True, text=True, cwd=tmp, env=env, timeout=300)
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            return {"error": (p.stdout + p.stderr)[-500:]}
        if task.get("execute"):
            reply = d.get("result") or ""
            m = re.search(r"```(?:python)?\n(.*?)```", reply, re.S)
            Path(tmp, "answer.py").write_text(m.group(1) if m else reply)
            try:
                ex = subprocess.run([sys.executable, "-I", "answer.py"], capture_output=True, text=True, cwd=tmp, timeout=30)
                d["result"] = ex.stdout.strip() or ("EXEC_ERROR " + ex.stderr[-300:])
            except subprocess.TimeoutExpired:
                d["result"] = "EXEC_TIMEOUT"
            d["code"] = reply
    u = d.get("usage", {})
    return {
        "actual": (d.get("result") or "").strip(),
        "input_tokens": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "turns": d.get("num_turns"),
        "cost_usd": d.get("total_cost_usd"),
        "is_error": d.get("is_error"),
        **({"code": d["code"]} if "code" in d else {}),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("seed_dir")
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--per-dataset", type=int, default=0, help="evenly spaced sample of K questions per dataset")
    args = ap.parse_args()
    sd = Path(args.seed_dir)
    datasets = {d["name"]: d for d in json.load(open(sd / "accuracy-datasets.json")) if not d["corruption"]}
    questions = [q for q in json.load(open(sd / "questions.json")) if q["dataset"] in datasets]
    if args.limit:
        questions = questions[: args.limit]
    if args.per_dataset:
        picked = []
        for name in datasets:
            qs = [q for q in questions if q["dataset"] == name]
            step = max(1, len(qs) // args.per_dataset)
            picked += qs[::step][: args.per_dataset]
        questions = picked
    formats = json.load(open(sd / "formats.json"))
    outdir = HERE / "results" / sd.name
    outdir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    for arm in args.arms:
        out = outdir / f"{arm}.jsonl"
        done = {json.loads(l)["id"] for l in open(out)} if out.exists() else set()
        todo = []
        for q in questions:
            if q["id"] in done:
                continue
            t = build_task(arm, q, datasets[q["dataset"]], formats)
            if t:
                todo.append((q, t))
        print(f"{arm}: {len(todo)} calls", flush=True)

        def work(item):
            q, t = item
            r = call(t)
            rec = {"id": q["id"], "arm": arm, "dataset": q["dataset"], "type": q["type"],
                   "expected": q["groundTruth"], "answerType": q.get("answerType"),
                   "normalizationOptions": q.get("normalizationOptions"), **r}
            with lock, open(out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")

        with ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(work, todo))


if __name__ == "__main__":
    main()
