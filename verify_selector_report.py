#!/usr/bin/env python3
"""Validate a context-selector/v1 report before an adapter trusts read_path."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import hook

DECISIONS = {
    "selected", "unsupported_format", "too_large", "raw_best", "below_threshold",
    "below_min_saved_tokens", "error",
}
REPORT_KEYS = [
    "schema_version", "adapter", "cwd", "out_dir", "model_profile", "policy", "summary", "results",
]
POLICY_KEYS = [
    "supported_extensions", "max_bytes", "candidate_tier", "min_savings_ratio",
    "min_saved_tokens", "include_candidates",
]
RESULT_KEYS = ["source", "source_name", "selected", "decision", "read_path"]
SELECTED_KEYS = [
    "kind", "bytes", "sha256", "raw_tokens", "selected_format", "selected_tokens",
    "payload_tokens", "instruction_tokens", "saved_tokens", "savings_ratio",
    "token_counter_label", "output_path", "output_sha256", "notes",
]
SUMMARY_KEYS = [
    "files", "selected_files", "raw_tokens", "selected_tokens", "saved_tokens", "savings_ratio",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Selector report JSON file, or '-' for stdin.")
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="Verify referenced files exist, source SHA-256 matches, and selected sidecars "
        "round-trip.",
    )
    args = parser.parse_args()

    try:
        if str(args.report) == "-":
            text = sys.stdin.read()
        else:
            text = args.report.read_text(encoding="utf-8")
        errors = validate_report(json.loads(text), check_files=args.check_files)
    except Exception as exc:
        errors = [f"invalid report JSON: {exc}"]

    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("context-selector/v1 report ok")
    return 0


def validate_report(report: dict[str, Any], check_files: bool = False) -> list[str]:
    errors = missing_keys(report, REPORT_KEYS, "report")
    if report.get("schema_version") != hook.SCHEMA_VERSION:
        errors.append(f"report.schema_version must be {hook.SCHEMA_VERSION}")
    errors += validate_policy(report.get("policy"))
    results = report.get("results")
    if not isinstance(results, list):
        return errors + ["report.results must be a list"]

    raw_tokens = selected_tokens = selected_files = 0
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            errors.append(f"results[{index}] must be an object")
            continue
        errors += validate_result(result, f"results[{index}]", check_files)
        raw = int(result.get("raw_tokens") or 0)
        raw_tokens += raw
        if result.get("selected"):
            selected_files += 1
            selected_tokens += int(result.get("selected_tokens") or 0)
        else:
            selected_tokens += raw

    summary = report.get("summary")
    if not isinstance(summary, dict):
        return errors + ["report.summary must be an object"]
    errors += missing_keys(summary, SUMMARY_KEYS, "summary")
    expected = {
        "files": len(results),
        "selected_files": selected_files,
        "raw_tokens": raw_tokens,
        "selected_tokens": selected_tokens,
        "saved_tokens": raw_tokens - selected_tokens,
    }
    errors += [
        f"summary.{key} must be {value}"
        for key, value in expected.items()
        if summary.get(key) != value
    ]
    if not ratio_matches(summary.get("savings_ratio", -1), raw_tokens, selected_tokens):
        errors.append("summary.savings_ratio does not match token counts")
    return errors


def validate_policy(policy: Any) -> list[str]:
    if not isinstance(policy, dict):
        return ["report.policy must be an object"]
    errors = missing_keys(policy, POLICY_KEYS, "policy")
    supported = policy.get("supported_extensions")
    if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
        errors.append("policy.supported_extensions must be a list of strings")
    if not non_negative(policy.get("max_bytes"), int):
        errors.append("policy.max_bytes must be a non-negative integer")
    if policy.get("candidate_tier") not in hook.CANDIDATE_TIERS:
        errors.append("policy.candidate_tier is invalid")
    if not non_negative(policy.get("min_savings_ratio"), (int, float)):
        errors.append("policy.min_savings_ratio must be a non-negative number")
    if not non_negative(policy.get("min_saved_tokens"), int):
        errors.append("policy.min_saved_tokens must be a non-negative integer")
    if not isinstance(policy.get("include_candidates"), bool):
        errors.append("policy.include_candidates must be boolean")
    return errors


def validate_result(result: dict[str, Any], prefix: str, check_files: bool) -> list[str]:
    errors = missing_keys(result, RESULT_KEYS, prefix)
    decision = result.get("decision")
    selected = result.get("selected")
    source = result.get("source")
    read_path = result.get("read_path")
    output_path = result.get("output_path")

    if decision not in DECISIONS:
        errors.append(f"{prefix}.decision is invalid: {decision!r}")
    if not isinstance(selected, bool):
        errors.append(f"{prefix}.selected must be boolean")
    if selected is True and decision != "selected":
        errors.append(f"{prefix}.selected=true requires decision=selected")
    if decision == "selected" and selected is not True:
        errors.append(f"{prefix}.decision=selected requires selected=true")
    if not isinstance(source, str) or not source:
        errors.append(f"{prefix}.source must be a non-empty string")
    if not isinstance(read_path, str) or not read_path:
        errors.append(f"{prefix}.read_path must be a non-empty string")

    if selected is True:
        errors += missing_keys(result, SELECTED_KEYS, prefix)
        if not output_path:
            errors.append(f"{prefix}.output_path is required when selected")
        elif read_path != output_path:
            errors.append(f"{prefix}.read_path must equal output_path when selected")
        errors += validate_token_math(result, prefix)
    else:
        if output_path is not None:
            errors.append(f"{prefix}.output_path must be null or absent when not selected")
        if isinstance(source, str) and isinstance(read_path, str) and read_path != source:
            errors.append(f"{prefix}.read_path must equal source when not selected")

    if result.get("token_counter_label") not in {None, "exact", "estimated"}:
        errors.append(f"{prefix}.token_counter_label must be exact or estimated")
    if check_files:
        errors += validate_files(result, prefix)
    return errors


def validate_token_math(result: dict[str, Any], prefix: str) -> list[str]:
    try:
        raw_tokens = int(result["raw_tokens"])
        selected_tokens = int(result["selected_tokens"])
        saved_tokens = int(result["saved_tokens"])
        savings_ratio = float(result["savings_ratio"])
    except Exception as exc:
        return [f"{prefix} token fields must be numeric: {exc}"]

    errors = []
    if raw_tokens < 0 or selected_tokens < 0:
        errors.append(f"{prefix} token counts must be non-negative")
    if saved_tokens != raw_tokens - selected_tokens:
        errors.append(f"{prefix}.saved_tokens must equal raw_tokens - selected_tokens")
    if not ratio_matches(savings_ratio, raw_tokens, selected_tokens):
        errors.append(f"{prefix}.savings_ratio does not match token counts")
    if selected_tokens > raw_tokens:
        errors.append(f"{prefix}.selected_tokens must not exceed raw_tokens when selected")
    return errors


def validate_files(result: dict[str, Any], prefix: str) -> list[str]:
    errors = []
    source = Path(str(result.get("source", "")))
    read_path = Path(str(result.get("read_path", "")))
    if not source.is_file():
        errors.append(f"{prefix}.source does not exist")
    if not read_path.is_file():
        errors.append(f"{prefix}.read_path does not exist")
    if not source.is_file():
        return errors
    expected_sha = result.get("sha256")
    if expected_sha and hook.sha256_file(source) != expected_sha:
        errors.append(f"{prefix}.sha256 does not match source")
    if result.get("selected") is not True or not read_path.is_file():
        return errors

    output_sha = result.get("output_sha256")
    if not isinstance(output_sha, str) or not output_sha:
        errors.append(f"{prefix}.output_sha256 must be a non-empty string when selected")
    elif hook.sha256_file(read_path) != output_sha:
        errors.append(f"{prefix}.output_sha256 does not match read_path")

    selected_format = result.get("selected_format")
    kind = result.get("kind")
    if not isinstance(selected_format, str) or not selected_format:
        return errors + [f"{prefix}.selected_format is required for round-trip check"]
    if not isinstance(kind, str) or not kind:
        return errors + [f"{prefix}.kind is required for round-trip check"]
    try:
        value = hook.load_source(source).value
        sidecar_text = read_path.read_text(encoding="utf-8")
        candidate_text = hook.candidate_text_from_blob(selected_format, sidecar_text)
        decoded = hook.decode_candidate_value(selected_format, candidate_text, kind)
    except Exception as exc:
        return errors + [f"{prefix}.read_path failed round-trip decode: {exc}"]
    if decoded != value:
        errors.append(f"{prefix}.read_path does not decode to source value")
    return errors


def missing_keys(value: dict[str, Any], keys: list[str], prefix: str) -> list[str]:
    return [f"{prefix}.{key} is required" for key in keys if key not in value]


def non_negative(value: Any, types: type | tuple[type, ...]) -> bool:
    return isinstance(value, types) and value >= 0


def ratio_matches(ratio: Any, raw_tokens: int, selected_tokens: int) -> bool:
    expected = 0.0 if raw_tokens == 0 else 1.0 - (selected_tokens / raw_tokens)
    return math.isclose(float(ratio), expected, rel_tol=1e-12, abs_tol=1e-12)


if __name__ == "__main__":
    raise SystemExit(main())
