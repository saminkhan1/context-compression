#!/usr/bin/env python3
"""Run the lean local evidence gate for the selector layer."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-tests",
        action="store_true",
        help="Run the full unittest suite before evidence smokes.",
    )
    parser.add_argument(
        "--benchmark-corpus",
        type=Path,
        default=None,
        help="Supported corpus for eval/benchmark smokes. Defaults to a temporary checked-in fixture corpus.",
    )
    parser.add_argument(
        "--toon-baseline-smoke",
        action="store_true",
        help="Also run a benchmark smoke that generates a real TOON comparator via npm.",
    )
    args = parser.parse_args()

    steps: list[tuple[str, list[str], str | None]] = []
    if args.full_tests:
        steps.append(("unit suite", [python(), "-m", "unittest", "discover", "-s", "tests"], None))

    with tempfile.TemporaryDirectory(prefix="context-evidence-") as tmp:
        tmp_dir = Path(tmp)
        selector_report = tmp_dir / "selector-report.json"
        eval_dataset = tmp_dir / "context-quality.jsonl"
        benchmark_json = tmp_dir / "benchmark-report.json"
        benchmark_md = tmp_dir / "benchmark-report.md"
        toon_benchmark_json = tmp_dir / "benchmark-report.toon.json"
        toon_benchmark_md = tmp_dir / "benchmark-report.toon.md"
        smoke_corpus = args.benchmark_corpus or build_smoke_corpus(tmp_dir / "corpus-smoke")

        steps.extend(
            [
                (
                    "selector report",
                    [
                        python(),
                        "selector.py",
                        "--cwd",
                        str(ROOT),
                        "--model",
                        "gpt-5.5",
                        "--report-out",
                        str(selector_report),
                        "sample-repetitive.json",
                    ],
                    None,
                ),
                (
                    "selector report verifier",
                    [python(), "verify_selector_report.py", "--check-files", str(selector_report)],
                    None,
                ),
                (
                    "codex hook rewrite smoke",
                    ["./run-hook.sh"],
                    json.dumps(
                        {
                            "hook_event_name": "PreToolUse",
                            "cwd": str(ROOT),
                            "model": "gpt-5.5",
                            "tool_name": "Bash",
                            "tool_input": {"command": "cat sample-repetitive.json"},
                        }
                    )
                    + "\n",
                ),
                (
                    "eval dataset build",
                    [
                        python(),
                        "evals/build_context_quality_dataset.py",
                        "--corpus",
                        str(smoke_corpus),
                        "--max-files",
                        "2",
                        "--out",
                        str(eval_dataset),
                    ],
                    None,
                ),
                (
                    "eval dataset verifier",
                    [python(), "evals/verify_context_quality_dataset.py", str(eval_dataset)],
                    None,
                ),
                (
                    "benchmark smoke with generated baseline",
                    [
                        python(),
                        "benchmark.py",
                        "run",
                        "--corpus",
                        str(smoke_corpus),
                        "--json-out",
                        str(benchmark_json),
                        "--markdown-out",
                        str(benchmark_md),
                        "--baseline-command",
                        "rawcopy=cp {input} {output}",
                    ],
                    None,
                ),
            ]
        )
        if args.toon_baseline_smoke:
            steps.append(
                (
                    "benchmark smoke with TOON baseline",
                    [
                        python(),
                        "benchmark.py",
                        "run",
                        "--corpus",
                        str(smoke_corpus),
                        "--json-out",
                        str(toon_benchmark_json),
                        "--markdown-out",
                        str(toon_benchmark_md),
                        "--baseline-command",
                        (
                            "toon=/opt/homebrew/bin/npm exec --yes "
                            "--package @toon-format/toon@2.3.0 -- "
                            "node scripts/toon_baseline.mjs --fallback-raw-on-fail {input} {output}"
                        ),
                    ],
                    None,
                )
            )

        for name, command, stdin in steps:
            stdout = run_step(name, command, stdin)
            if name == "codex hook rewrite smoke":
                validate_hook_rewrite(stdout)

        validate_benchmark_smoke(benchmark_json)
        if args.toon_baseline_smoke:
            validate_benchmark_smoke(toon_benchmark_json, baseline="toon")

    print("evidence gate ok")
    return 0


def python() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else Path(sys.executable))


def build_smoke_corpus(corpus: Path) -> Path:
    corpus.mkdir(parents=True, exist_ok=True)
    for source in [
        ROOT / "sample-repetitive.json",
        ROOT / "sample-data.json",
        ROOT / "tests" / "fixtures" / "hf-julien-c-titanic-survival.json",
    ]:
        shutil.copyfile(source, corpus / source.name)
    return corpus


def run_step(name: str, command: list[str], stdin: str | None) -> str:
    print(f"[evidence] {name}")
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"{name} failed with exit {result.returncode}")
    return result.stdout


def validate_benchmark_smoke(path: Path, baseline: str = "rawcopy") -> None:
    report = json.loads(path.read_text(encoding="utf-8"))
    totals = report.get("totals", {})
    required = ["latency", "external_baselines", "candidate_ablation"]
    missing = [key for key in required if key not in totals]
    if missing:
        raise SystemExit(f"benchmark report missing totals fields: {', '.join(missing)}")
    baseline_summary = totals["external_baselines"].get(baseline)
    if (
        not baseline_summary
        or baseline_summary.get("files_missing") != 0
        or baseline_summary.get("files_available", 0) == 0
    ):
        raise SystemExit(f"benchmark {baseline} baseline did not cover the smoke corpus")
    if not totals["candidate_ablation"]:
        raise SystemExit("benchmark candidate ablation is empty")


def validate_hook_rewrite(stdout: str) -> None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"hook smoke did not return JSON: {exc}") from exc
    hook_output = payload.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        raise SystemExit("hook smoke did not emit hookSpecificOutput")
    if hook_output.get("hookEventName") != "PreToolUse":
        raise SystemExit("hook smoke did not emit a PreToolUse output")
    updated_input = hook_output.get("updatedInput")
    if not isinstance(updated_input, dict):
        raise SystemExit("hook smoke did not emit updatedInput")
    command = updated_input.get("command")
    if not isinstance(command, str) or not command.startswith("cat -- "):
        raise SystemExit("hook smoke did not rewrite the cat command")
    if "sample-repetitive.json" in command:
        raise SystemExit("hook smoke command still reads the original source")
    if ".codex/context-cache/" not in command or ".column-json.txt" not in command:
        raise SystemExit("hook smoke command does not read the expected optimized sidecar")
    if "additionalContext" in hook_output:
        raise SystemExit("hook smoke should remain invisible by default; unexpected additionalContext")


if __name__ == "__main__":
    raise SystemExit(main())
