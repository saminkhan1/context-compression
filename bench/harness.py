"""Controlled harness for the context-compression study (claude-haiku-4-5 only).

Every arm sees the byte-identical data file (json.dumps(data, indent=2)), uses the same
answer rules and records raw per-call usage. Arms:

  in-context (tools off):   toon | json-pretty | ctab2  [-think]
  code (single shot):       code  | code-nosample | code-namesonly
                            (model writes one script; the harness runs it)
  lean agent (tools off, text protocol, python only, <=6 executions):
                            agent | agent-nopreview
  native Claude Code Bash:  tool | tool-nopreview

Results append to results2/<set>/<arm>[@rN].jsonl and resume per question id.
Usage: python harness.py <set_dir> ARM [ARM...] [--workers 8] [--rep N] [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ctab  # noqa: E402
import run as legacy  # noqa: E402  (frozen prompts and templates)

MODEL = "claude-haiku-4-5"
SYSTEM = legacy.SYSTEM
ANSWER_RULES = legacy.ANSWER_RULES
MAX_EXEC = 6
OUT_TRUNC = 3000
TYPE_NAMES = {
    bool: "bool", int: "int", float: "number", str: "string", type(None): "null",
    list: "array", dict: "object",
}


def base_cmd(think: bool) -> list[str]:
    return [
        "claude", "-p", "--model", MODEL,
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
        "--settings", json.dumps({"alwaysThinkingEnabled": think}),
        "--output-format", "json", "--system-prompt", SYSTEM,
    ]


def cli(prompt: str, cwd: str, tools: list[str] | None = None, think: bool = False) -> dict:
    cmd = base_cmd(think) + (tools if tools is not None else ["--tools", ""])
    env = {**os.environ, "MAX_THINKING_TOKENS": "8000" if think else "0"}
    last = {"error": "no attempt"}
    waited = 0
    attempt = 0
    while attempt < 4:  # infrastructure failures (throttling, timeouts) are retried, not scored as wrong
        attempt += 1
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=600)
            d = json.loads(p.stdout)
            if not d.get("is_error"):
                return d
            last = {"error": str(d.get("result"))[-500:]}
            if "hit your" in str(d.get("result")) and "limit" in str(d.get("result")) and waited < 12 * 3600:
                time.sleep(600)  # usage-limit window: wait it out (up to 12 h) instead of recording an error
                waited += 600
                attempt -= 1
                continue
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            last = {"error": f"{type(e).__name__}: {(locals().get('p') and (p.stdout + p.stderr)[-300:]) or ''}"}
        time.sleep(5 * (attempt + 1))
    return last


def usage_of(d: dict) -> dict:
    u = d.get("usage", {})
    return {
        "input": u.get("input_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "output": u.get("output_tokens", 0),
    }


def run_python(code: str, cwd: str, timeout: int = 60) -> str:
    Path(cwd, "step.py").write_text(code)
    try:
        ex = subprocess.run(
            [sys.executable, "-I", "step.py"], capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
        out = ex.stdout if ex.returncode == 0 else ex.stdout + "\nERROR:\n" + ex.stderr[-1500:]
    except subprocess.TimeoutExpired:
        out = "ERROR: timeout"
    return out.strip()


def first_python_block(text: str) -> str | None:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return m.group(1) if m else None


# ---------------------------------------------------------------- previews
def preview(data, samples: bool = True, types: bool = True) -> str:
    """Question-independent description of the file. Mode flags are ablations."""
    def label(k, tset, subs, present, n):
        out = f"{k}{{{','.join(subs[k])}}}" if k in subs else k
        if types:
            out += ":" + "|".join(sorted(tset))
            if present < n:
                out += f" (optional, in {present}/{n})"
        return out

    def fields_of(rows):
        types_: dict[str, set] = {}
        present: Counter = Counter()
        subs: dict[str, dict[str, None]] = {}
        for row in rows:
            for k, v in row.items():
                types_.setdefault(k, set()).add(TYPE_NAMES[type(v)])
                present[k] += 1
                if isinstance(v, dict) and v:
                    subs.setdefault(k, {}).update(dict.fromkeys(v))
        return ", ".join(label(k, t, subs, present[k], len(rows)) for k, t in types_.items())

    lines = []
    items = data.items() if isinstance(data, dict) else [("", data)]
    for key, val in items:
        name = f'data["{key}"]'
        if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
            lines.append(f"{name}: list of {len(val)} objects; top-level fields in order: {fields_of(val)}")
            if samples:
                lines.append("first 2 records:\n" + ctab.encode({key: val[:2]}))
        elif isinstance(val, dict) and len(val) > 3 and all(isinstance(v, dict) for v in val.values()):
            lines.append(
                f"{name}: object with {len(val)} keyed entries (key -> record); "
                f"record fields in order: {fields_of(list(val.values()))}"
            )
            if samples:
                lines.append("first 2 entries:\n" + ctab.encode({key: dict(list(val.items())[:2])}))
        else:
            text = json.dumps(val, ensure_ascii=False)
            detail = f" = {text}" if len(text) < 1500 else f" ({len(text)} chars)"
            lines.append(f"{name}: {TYPE_NAMES[type(val)]}{detail}")
    return "\n".join(lines)


PREVIEW_MODES = {
    "full": dict(), "nosample": dict(samples=False), "namesonly": dict(samples=False, types=False),
}

# ---------------------------------------------------------------- prompts (frozen)
CODE_RULES = """Printed answer format:
- numbers: plain number with no commas, currency symbols, or units; keep decimals
  (do not truncate or round unless the question asks)
- dates/field names: the exact string from the data
- lists: comma-separated values with no spaces"""


def code_prompt(summary: str, question: str) -> str:
    return f"""A dataset is stored in ./data.json. Summary:

{summary}

Question: {question}

Write a short Python 3 script that loads ./data.json with the json module, computes the answer,
and prints ONLY the answer. {CODE_RULES}
Reply with only a ```python code block.""".strip()


def agent_prompt(summary: str | None, question: str) -> str:
    block = f"\n\nSummary of the file:\n{summary}" if summary else ""
    return f"""A dataset is stored in ./data.json (in the directory where your code runs).{block}

Question: {question}

You may run Python 3 to inspect the data and compute the answer. To run code, reply with exactly one
```python code block and nothing else; you will receive its printed output (truncated to {OUT_TRUNC}
characters). You can run code up to {MAX_EXEC} times. When you are sure of the answer, reply with a
single line:
FINAL: <answer>

{ANSWER_RULES}""".strip()


def native_prompt(summary: str | None, question: str) -> str:
    block = f" Summary:\n\n{summary}\n" if summary else ""
    return f"""The full dataset is in ./data.json.{block}

Question: {question}

Use the Bash tool (python3, jq, head, grep, etc.) to compute the answer from ./data.json rather than
guessing. When done, reply with ONLY the final answer.

{ANSWER_RULES}""".strip()


def prompt_fingerprint() -> str:
    h = hashlib.sha256()
    for f in (code_prompt("S", "Q"), agent_prompt("S", "Q"), agent_prompt(None, "Q"), native_prompt("S", "Q"),
              native_prompt(None, "Q"), SYSTEM, ANSWER_RULES, CODE_RULES, legacy.inline_prompt("P", "n", "f", "D", "Q")):
        h.update(f.encode())
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- arms
def file_text(data) -> str:
    return json.dumps(data, indent=2)


def answer_from(arm_kind: str, **kw) -> dict:
    return kw


def run_arm(arm: str, q: dict, ds: dict, formats: dict) -> dict | None:
    data, question = ds["data"], q["prompt"]
    think = arm.endswith("-think")
    base = arm.removesuffix("-think")
    with tempfile.TemporaryDirectory() as tmp:
        if base in ("toon", "json-pretty", "json-compact", "ctab2"):
            if base == "ctab2":
                text = ctab.encode(data)
                prompt = legacy.inline_prompt(ctab.PRIMER, "ctab", "text", text, question)
            else:
                if base not in ds["encodings"]:
                    return None
                f = formats[base]
                prompt = legacy.inline_prompt(f["primer"], base, f["fence"], ds["encodings"][base], question)
            d = cli(prompt, tmp, think=think)
            if "error" in d:
                return {"error": d["error"]}
            return {"actual": (d.get("result") or "").strip(), "calls": [usage_of(d)], "turns": 1}

        Path(tmp, "data.json").write_text(file_text(data))
        if base.startswith("code"):
            mode = base.removeprefix("code").lstrip("-") or "full"
            d = cli(code_prompt(preview(data, **PREVIEW_MODES[mode]), question), tmp)
            if "error" in d:
                return {"error": d["error"]}
            reply = d.get("result") or ""
            block = first_python_block(reply)
            out = run_python(block if block else reply, tmp, timeout=30)
            return {"actual": out, "calls": [usage_of(d)], "turns": 1, "transcript": reply,
                    "exec_error": out.startswith("ERROR")}

        if base in ("agent", "agent-nopreview"):
            summary = preview(data) if base == "agent" else None
            task = agent_prompt(summary, question)
            convo, calls, final, n_exec = "", [], None, 0
            for _ in range(MAX_EXEC + 1):
                d = cli(task + convo, tmp)
                if "error" in d:
                    return {"error": d["error"], "calls": calls}
                calls.append(usage_of(d))
                reply = (d.get("result") or "").strip()
                block = first_python_block(reply)
                if block is not None and n_exec < MAX_EXEC:
                    n_exec += 1
                    out = run_python(block, tmp)
                    shown = out if len(out) <= OUT_TRUNC else out[:OUT_TRUNC] + "\n...[truncated]"
                    convo += f"\n\n[your reply]\n{reply}\n\n[execution output]\n{shown}"
                    continue
                m = re.findall(r"^\s*FINAL:\s*(.*)$", reply, re.M)
                final = m[-1].strip() if m else reply
                break
            return {"actual": final if final is not None else "", "calls": calls, "turns": len(calls),
                    "executions": n_exec, "transcript": convo, "hit_limit": final is None}

        if base in ("tool", "tool-nopreview"):
            summary = preview(data) if base == "tool" else None
            flags = ["--tools", "Bash", "--allowedTools", "Bash(python3:*)", "Bash(cat:*)", "Bash(head:*)",
                     "Bash(tail:*)", "Bash(jq:*)", "Bash(wc:*)", "Bash(grep:*)", "--max-turns", "12"]
            d = cli(native_prompt(summary, question), tmp, tools=flags)
            if "error" in d:
                return {"error": d["error"]}
            return {"actual": (d.get("result") or "").strip(), "calls": [usage_of(d)],
                    "turns": d.get("num_turns"), "iterations": d.get("usage", {}).get("iterations")}
    raise SystemExit(f"unknown arm {arm}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir")
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    sd = Path(args.set_dir)
    datasets = {d["name"]: d for d in json.loads((sd / "accuracy-datasets.json").read_text()) if not d["corruption"]}
    questions = [q for q in json.loads((sd / "questions.json").read_text()) if q["dataset"] in datasets]
    if args.limit:
        questions = questions[: args.limit]
    formats = json.loads((sd / "formats.json").read_text())
    outdir = HERE / "results2" / sd.name
    outdir.mkdir(parents=True, exist_ok=True)
    fp = prompt_fingerprint()
    lock = threading.Lock()
    for arm in args.arms:
        out = outdir / (f"{arm}@r{args.rep}.jsonl" if args.rep > 1 else f"{arm}.jsonl")
        done = {json.loads(line)["id"] for line in open(out)} if out.exists() else set()
        todo = [q for q in questions if q["id"] not in done]
        print(f"{arm}: {len(todo)} calls (fingerprint {fp})", flush=True)

        def work(q, arm=arm, out=out):
            r = run_arm(arm, q, datasets[q["dataset"]], formats)
            if r is None:
                return
            rec = {"id": q["id"], "arm": arm, "dataset": q["dataset"], "type": q["type"],
                   "expected": q["groundTruth"], "answerType": q.get("answerType"),
                   "normalizationOptions": q.get("normalizationOptions"), "fingerprint": fp,
                   "rep": args.rep, **r}
            with lock, open(out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")

        with ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(work, todo))


if __name__ == "__main__":
    main()
