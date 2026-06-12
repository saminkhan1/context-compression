#!/usr/bin/env python3
"""Agent-neutral structured-context selector.

This CLI exposes the same deterministic selector used by the Codex hook as a
JSON decision report that other agents can call from tools, extensions, or RPC
adapters.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import hook

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Structured data files to evaluate.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--model-provider", default=None)
    parser.add_argument("--adapter", default="generic-cli")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--report-out", type=Path, default=None)
    parser.add_argument("--max-bytes", type=int, default=hook.DEFAULT_MAX_BYTES)
    parser.add_argument("--min-savings-ratio", type=float, default=hook.DEFAULT_MIN_SAVINGS_RATIO)
    parser.add_argument("--min-saved-tokens", type=int, default=hook.DEFAULT_MIN_SAVED_TOKENS)
    parser.add_argument(
        "--candidate-tier",
        choices=sorted(hook.CANDIDATE_TIERS),
        default=hook.DEFAULT_CANDIDATE_TIER,
        help="Candidate risk tier to evaluate. Default: safe.",
    )
    parser.add_argument("--include-candidates", action="store_true")
    parser.add_argument(
        "--verify-report",
        action="store_true",
        help="Validate the emitted context-selector/v1 report and selected files before printing.",
    )
    args = parser.parse_args()

    cwd = args.cwd.expanduser().resolve()
    out_dir = (args.out_dir or (cwd / ".codex" / "context-cache")).expanduser()
    if not out_dir.is_absolute():
        out_dir = cwd / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {"model": args.model}
    if args.model_provider:
        payload["model_provider"] = args.model_provider
    model_profile = hook.resolve_model_profile(args.model, payload, cwd)

    results = [
        select_path(
            raw_path,
            cwd,
            out_dir,
            model_profile,
            args.max_bytes,
            args.min_savings_ratio,
            args.min_saved_tokens,
            args.candidate_tier,
            args.include_candidates,
        )
        for raw_path in args.paths
    ]
    report = build_report(results, cwd, out_dir, model_profile, args)
    if args.report_out:
        report_out = resolve_path(args.report_out, cwd)
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.verify_report:
        from verify_selector_report import validate_report

        errors = validate_report(report, check_files=True)
        if errors:
            for error in errors:
                print(f"selector report verification failed: {error}", file=sys.stderr)
            return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def select_path(
    raw_path: str,
    cwd: Path,
    out_dir: Path,
    model_profile: hook.ModelProfile,
    max_bytes: int,
    min_savings_ratio: float,
    min_saved_tokens: int,
    candidate_tier: str,
    include_candidates: bool,
) -> dict[str, Any]:
    path = resolve_path(raw_path, cwd)
    base: dict[str, Any] = {
        "source": str(path),
        "source_name": path.name,
        "selected": False,
        "decision": "error",
        "read_path": str(path),
    }
    if not path.exists() or not path.is_file():
        return {**base, "error": "source file not found"}
    if path.suffix.lower() not in hook.SUPPORTED_EXTENSIONS:
        return {**base, "decision": "unsupported_format", "error": f"unsupported extension {path.suffix}"}
    if path.stat().st_size > max_bytes:
        return {**base, "decision": "too_large", "bytes": path.stat().st_size, "max_bytes": max_bytes}

    source = hook.load_source(path)
    raw_tokens = hook.count_tokens(source.raw_text, model_profile)
    choice: hook.Choice | None = None
    candidate_rows: list[dict[str, Any]] | None = None
    if include_candidates:
        candidate_rows = candidate_metrics(source, model_profile, candidate_tier, raw_tokens)
        if not candidate_rows:
            return {
                **base,
                "kind": source.kind,
                "bytes": path.stat().st_size,
                "sha256": hook.sha256_file(path),
                "raw_tokens": raw_tokens,
                "error": "no reversible candidates generated",
            }
        best = candidate_rows[0]
    else:
        try:
            choice = hook.choose_best(
                source,
                model_profile,
                out_dir,
                candidate_tier,
                persist_sidecar=False,
                raw_tokens=raw_tokens,
            )
        except ValueError:
            return {
                **base,
                "kind": source.kind,
                "bytes": path.stat().st_size,
                "sha256": hook.sha256_file(path),
                "raw_tokens": raw_tokens,
                "error": "no reversible candidates generated",
            }
        best = {
            "candidate": choice.candidate,
            "name": choice.candidate.name,
            "total_tokens": choice.total_tokens,
            "payload_tokens": choice.payload_tokens,
            "instruction_tokens": choice.instruction_tokens,
            "savings_ratio": choice.savings_ratio,
            "notes": list(choice.candidate.notes),
        }
    if not best:
        return {
            **base,
            "kind": source.kind,
            "bytes": path.stat().st_size,
            "sha256": hook.sha256_file(path),
            "raw_tokens": raw_tokens,
            "error": "no reversible candidates generated",
        }

    savings_ratio = best["savings_ratio"]
    saved_tokens = raw_tokens - best["total_tokens"]
    selected = best["name"] != "raw" and savings_ratio >= min_savings_ratio and saved_tokens >= min_saved_tokens
    decision = selection_decision(best["name"], savings_ratio, saved_tokens, min_savings_ratio, min_saved_tokens)
    if selected and choice is not None:
        choice = hook.choice_with_sidecar(choice, source, out_dir, model_profile, candidate_tier)
        output_path = hook.require_output_path(choice)
    elif selected:
        output_path = hook.write_candidate_sidecar(source, best["candidate"], out_dir, model_profile, candidate_tier)
    else:
        output_path = None
    output_sha256 = hook.sha256_file(output_path) if output_path else None
    result = {
        **base,
        "selected": selected,
        "decision": decision,
        "read_path": str(output_path or path),
        "kind": source.kind,
        "bytes": path.stat().st_size,
        "sha256": hook.sha256_file(path),
        "raw_tokens": raw_tokens,
        "selected_format": best["name"],
        "selected_tokens": best["total_tokens"],
        "payload_tokens": best["payload_tokens"],
        "instruction_tokens": best["instruction_tokens"],
        "saved_tokens": raw_tokens - best["total_tokens"],
        "savings_ratio": savings_ratio,
        "token_counter_label": "estimated" if model_profile.token_counter == "deterministic-fallback" else "exact",
        "output_path": str(output_path) if output_path else None,
        "output_sha256": output_sha256,
        "notes": best["notes"],
    }
    if include_candidates and candidate_rows is not None:
        result["candidates"] = [
            {key: value for key, value in row.items() if key != "candidate"}
            for row in candidate_rows
        ]
    return result


def candidate_metrics(
    source: hook.SourceData,
    model_profile: hook.ModelProfile,
    candidate_tier: str,
    raw_tokens: int | None = None,
) -> list[dict[str, Any]]:
    if raw_tokens is None:
        raw_tokens = hook.count_tokens(source.raw_text, model_profile)
    rows = []
    for candidate in hook.candidates_for_profile(source, model_profile, candidate_tier):
        total_tokens, payload_tokens, instruction_tokens = hook.candidate_token_metrics(candidate, model_profile)
        rows.append(
            {
                "candidate": candidate,
                "name": candidate.name,
                "total_tokens": total_tokens,
                "payload_tokens": payload_tokens,
                "instruction_tokens": instruction_tokens,
                "saved_tokens": raw_tokens - total_tokens,
                "savings_ratio": 0.0 if raw_tokens == 0 else 1.0 - (total_tokens / raw_tokens),
                "notes": list(candidate.notes),
            }
        )
    rows.sort(
        key=lambda row: hook.candidate_rank_key(
            row["candidate"],
            (row["total_tokens"], row["payload_tokens"], row["instruction_tokens"]),
        )
    )
    return rows


def build_report(
    results: list[dict[str, Any]],
    cwd: Path,
    out_dir: Path,
    model_profile: hook.ModelProfile,
    args: argparse.Namespace,
) -> dict[str, Any]:
    selected = [result for result in results if result.get("selected")]
    raw_tokens = sum(int(result.get("raw_tokens", 0)) for result in results)
    selected_tokens = sum(
        int(result.get("selected_tokens", 0)) if result.get("selected") else int(result.get("raw_tokens", 0))
        for result in results
    )
    return {
        "schema_version": hook.SCHEMA_VERSION,
        "adapter": args.adapter,
        "cwd": str(cwd),
        "out_dir": str(out_dir),
        "model_profile": asdict(model_profile),
        "policy": {
            "supported_extensions": sorted(hook.SUPPORTED_EXTENSIONS),
            "max_bytes": args.max_bytes,
            "candidate_tier": args.candidate_tier,
            "min_savings_ratio": args.min_savings_ratio,
            "min_saved_tokens": args.min_saved_tokens,
            "include_candidates": args.include_candidates,
            "verify_report": args.verify_report,
        },
        "summary": {
            "files": len(results),
            "selected_files": len(selected),
            "raw_tokens": raw_tokens,
            "selected_tokens": selected_tokens,
            "saved_tokens": raw_tokens - selected_tokens,
            "savings_ratio": 0.0 if raw_tokens == 0 else 1.0 - (selected_tokens / raw_tokens),
        },
        "results": results,
    }


def resolve_path(raw_path: str | Path, cwd: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def selection_decision(
    candidate_name: str,
    savings_ratio: float,
    saved_tokens: int,
    min_savings_ratio: float,
    min_saved_tokens: int,
) -> str:
    if candidate_name == "raw":
        return "raw_best"
    if savings_ratio < min_savings_ratio:
        return "below_threshold"
    if saved_tokens < min_saved_tokens:
        return "below_min_saved_tokens"
    return "selected"


if __name__ == "__main__":
    raise SystemExit(main())
