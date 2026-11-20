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
    parser.add_argument(
        "--min-savings-ratio", type=float, default=hook.DEFAULT_MIN_SAVINGS_RATIO
    )
    parser.add_argument(
        "--min-saved-tokens", type=int, default=hook.DEFAULT_MIN_SAVED_TOKENS
    )
    parser.add_argument(
        "--candidate-tier",
        choices=sorted(hook.CANDIDATE_TIERS),
        default=hook.DEFAULT_CANDIDATE_TIER,
        help="Candidate risk tier to evaluate. Default: safe.",
    )
    parser.add_argument("--include-candidates", action="store_true")
    parser.add_argument(
        "--preview-above-tokens",
        type=int,
        default=0,
        help=(
            "Also write a labelled, lossy schema preview (preview_path) for files whose "
            "best lossless view still exceeds this many tokens. 0 disables. See preview.py."
        ),
    )
    parser.add_argument(
        "--verify-report",
        action="store_true",
        help="Validate the emitted context-selector/v1 report and selected files before printing.",
    )
    args = parser.parse_args()

    cwd = args.cwd.expanduser().resolve()
    out_dir = cwd / Path(args.out_dir or ".codex/context-cache").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {"model": args.model}
    if args.model_provider:
        payload["model_provider"] = args.model_provider
    model_profile = hook.resolve_model_profile(args.model, payload, cwd)

    results = [select_path(raw, cwd, out_dir, model_profile, args) for raw in args.paths]
    for result in results:
        best_tokens = result.get("selected_tokens" if result["selected"] else "raw_tokens")
        if args.preview_above_tokens > 0 and (best_tokens or 0) > args.preview_above_tokens:
            add_preview(result, out_dir, model_profile)

    selected = [result for result in results if result["selected"]]
    raw_tokens = sum(result.get("raw_tokens", 0) for result in results)
    selected_tokens = sum(
        result["selected_tokens"] if result["selected"] else result.get("raw_tokens", 0)
        for result in results
    )
    report = {
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
    if args.report_out:
        report_out = (cwd / args.report_out.expanduser()).resolve()
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if args.verify_report:
        from verify_selector_report import validate_report

        errors = validate_report(report, check_files=True)
        for error in errors:
            print(f"selector report verification failed: {error}", file=sys.stderr)
        if errors:
            return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def select_path(
    raw_path: str,
    cwd: Path,
    out_dir: Path,
    model_profile: hook.ModelProfile,
    args: argparse.Namespace,
) -> dict[str, Any]:
    path = (cwd / Path(raw_path).expanduser()).resolve()
    base = {
        "source": str(path),
        "source_name": path.name,
        "selected": False,
        "decision": "error",
        "read_path": str(path),
    }
    if not path.is_file():
        return {**base, "error": "source file not found"}
    if path.suffix.lower() not in hook.SUPPORTED_EXTENSIONS:
        return {
            **base,
            "decision": "unsupported_format",
            "error": f"unsupported extension {path.suffix}",
        }
    size = path.stat().st_size
    if size > args.max_bytes:
        return {**base, "decision": "too_large", "bytes": size, "max_bytes": args.max_bytes}

    source = hook.load_source(path)
    raw_tokens = hook.count_tokens(source.raw_text, model_profile)
    base |= {
        "kind": source.kind,
        "bytes": size,
        "sha256": hook.sha256_file(path),
        "raw_tokens": raw_tokens,
    }
    no_candidates = {**base, "error": "no reversible candidates generated"}
    if args.include_candidates:
        rows = candidate_metrics(source, model_profile, args.candidate_tier, raw_tokens)
        if not rows:
            return no_candidates
        best = rows[0]
    else:
        try:
            choice = hook.choose_best(
                source,
                model_profile,
                out_dir,
                args.candidate_tier,
                persist_sidecar=False,
                raw_tokens=raw_tokens,
            )
        except ValueError:
            return no_candidates
        best = {
            "candidate": choice.candidate,
            "name": choice.candidate.name,
            "total_tokens": choice.total_tokens,
            "payload_tokens": choice.payload_tokens,
            "instruction_tokens": choice.instruction_tokens,
            "savings_ratio": choice.savings_ratio,
            "notes": list(choice.candidate.notes),
        }

    savings_ratio = best["savings_ratio"]
    saved_tokens = raw_tokens - best["total_tokens"]
    if best["name"] == "raw":
        decision = "raw_best"
    elif savings_ratio < args.min_savings_ratio:
        decision = "below_threshold"
    elif saved_tokens < args.min_saved_tokens:
        decision = "below_min_saved_tokens"
    else:
        decision = "selected"
    selected = decision == "selected"
    output_path = None
    if selected:
        output_path = hook.write_candidate_sidecar(
            source, best["candidate"], out_dir, model_profile, args.candidate_tier
        )
    result = {
        **base,
        "selected": selected,
        "decision": decision,
        "read_path": str(output_path or path),
        "selected_format": best["name"],
        "selected_tokens": best["total_tokens"],
        "payload_tokens": best["payload_tokens"],
        "instruction_tokens": best["instruction_tokens"],
        "saved_tokens": saved_tokens,
        "savings_ratio": savings_ratio,
        "token_counter_label": (
            "estimated" if model_profile.token_counter == "deterministic-fallback" else "exact"
        ),
        "output_path": str(output_path) if selected else None,
        "output_sha256": hook.sha256_file(output_path) if selected else None,
        "notes": best["notes"],
    }
    if args.include_candidates:
        result["candidates"] = [
            {key: value for key, value in row.items() if key != "candidate"} for row in rows
        ]
    return result


def add_preview(result: dict[str, Any], out_dir: Path, model_profile: hook.ModelProfile) -> None:
    import preview

    source_path = Path(result["source"])
    source = hook.load_source(source_path)
    text = preview.labelled_preview(source.value, result["source"], hook.describe_source(source))
    preview_path = out_dir / f"{source_path.stem}.{result['sha256'][:16]}.preview.txt"
    preview_path.write_text(text + "\n", encoding="utf-8")
    result["preview_path"] = str(preview_path)
    result["preview_tokens"] = hook.count_tokens(text, model_profile)
    result["preview_lossless"] = False


def candidate_metrics(
    source: hook.SourceData,
    model_profile: hook.ModelProfile,
    candidate_tier: str,
    raw_tokens: int,
) -> list[dict[str, Any]]:
    rows = []
    for candidate in hook.candidates_for_profile(source, model_profile, candidate_tier):
        total_tokens, payload_tokens, instruction_tokens = hook.candidate_token_metrics(
            candidate, model_profile
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
