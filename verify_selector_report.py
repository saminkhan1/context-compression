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


SCHEMA_VERSION = "context-selector/v1"
DECISIONS = {"selected", "unsupported_format", "too_large", "raw_best", "below_threshold", "below_min_saved_tokens", "error"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Selector report JSON file, or '-' for stdin.")
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="Verify referenced files exist, source SHA-256 matches, and selected sidecars round-trip.",
    )
    args = parser.parse_args()

    try:
        text = sys.stdin.read() if str(args.report) == "-" else args.report.read_text(encoding="utf-8")
        errors = validate_report(json.loads(text), check_files=args.check_files)
    except Exception as exc:
        errors = [f"invalid report JSON: {exc}"]

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("context-selector/v1 report ok")
    return 0


def validate_report(report: dict[str, Any], check_files: bool = False) -> list[str]:
    errors: list[str] = []
    require_keys(
        report,
        ["schema_version", "adapter", "cwd", "out_dir", "model_profile", "policy", "summary", "results"],
        "report",
        errors,
    )
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"report.schema_version must be {SCHEMA_VERSION}")
    errors.extend(validate_policy(report.get("policy")))
    if not isinstance(report.get("results"), list):
        errors.append("report.results must be a list")
        return errors

    result_errors = []
    raw_tokens = 0
    selected_tokens = 0
    selected_files = 0
    for index, result in enumerate(report["results"]):
        if not isinstance(result, dict):
            result_errors.append(f"results[{index}] must be an object")
            continue
        result_errors.extend(validate_result(result, index, check_files))
        raw = int(result.get("raw_tokens", 0) or 0)
        raw_tokens += raw
        if result.get("selected"):
            selected_files += 1
            selected_tokens += int(result.get("selected_tokens", 0) or 0)
        else:
            selected_tokens += raw
    errors.extend(result_errors)
    errors.extend(validate_summary(report.get("summary"), len(report["results"]), selected_files, raw_tokens, selected_tokens))
    return errors


def validate_policy(policy: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(policy, dict):
        return ["report.policy must be an object"]
    require_keys(
        policy,
        [
            "supported_extensions",
            "max_bytes",
            "candidate_tier",
            "min_savings_ratio",
            "min_saved_tokens",
            "include_candidates",
        ],
        "policy",
        errors,
    )
    supported = policy.get("supported_extensions")
    if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
        errors.append("policy.supported_extensions must be a list of strings")
    if not isinstance(policy.get("max_bytes"), int) or policy.get("max_bytes") < 0:
        errors.append("policy.max_bytes must be a non-negative integer")
    if policy.get("candidate_tier") not in hook.CANDIDATE_TIERS:
        errors.append("policy.candidate_tier is invalid")
    if not isinstance(policy.get("min_savings_ratio"), (int, float)) or policy.get("min_savings_ratio") < 0:
        errors.append("policy.min_savings_ratio must be a non-negative number")
    if not isinstance(policy.get("min_saved_tokens"), int) or policy.get("min_saved_tokens") < 0:
        errors.append("policy.min_saved_tokens must be a non-negative integer")
    if not isinstance(policy.get("include_candidates"), bool):
        errors.append("policy.include_candidates must be boolean")
    return errors


def validate_result(result: dict[str, Any], index: int, check_files: bool) -> list[str]:
    errors: list[str] = []
    prefix = f"results[{index}]"
    require_keys(result, ["source", "source_name", "selected", "decision", "read_path"], prefix, errors)

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
        require_keys(
            result,
            [
                "kind",
                "bytes",
                "sha256",
                "raw_tokens",
                "selected_format",
                "selected_tokens",
                "payload_tokens",
                "instruction_tokens",
                "saved_tokens",
                "savings_ratio",
                "token_counter_label",
                "output_path",
                "output_sha256",
                "notes",
            ],
            prefix,
            errors,
        )
        if not output_path:
            errors.append(f"{prefix}.output_path is required when selected")
        elif read_path != output_path:
            errors.append(f"{prefix}.read_path must equal output_path when selected")
        errors.extend(validate_token_math(result, prefix))
    else:
        if output_path is not None:
            errors.append(f"{prefix}.output_path must be null or absent when not selected")
        if isinstance(source, str) and isinstance(read_path, str) and read_path != source:
            errors.append(f"{prefix}.read_path must equal source when not selected")

    label = result.get("token_counter_label")
    if label is not None and label not in {"exact", "estimated"}:
        errors.append(f"{prefix}.token_counter_label must be exact or estimated")

    if check_files:
        errors.extend(validate_files(result, prefix))
    return errors


def validate_token_math(result: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    try:
        raw_tokens = int(result["raw_tokens"])
        selected_tokens = int(result["selected_tokens"])
        saved_tokens = int(result["saved_tokens"])
        savings_ratio = float(result["savings_ratio"])
    except Exception as exc:
        return [f"{prefix} token fields must be numeric: {exc}"]

    if raw_tokens < 0 or selected_tokens < 0:
        errors.append(f"{prefix} token counts must be non-negative")
    if saved_tokens != raw_tokens - selected_tokens:
        errors.append(f"{prefix}.saved_tokens must equal raw_tokens - selected_tokens")
    expected_ratio = 0.0 if raw_tokens == 0 else 1.0 - (selected_tokens / raw_tokens)
    if not math.isclose(savings_ratio, expected_ratio, rel_tol=1e-12, abs_tol=1e-12):
        errors.append(f"{prefix}.savings_ratio does not match token counts")
    if selected_tokens > raw_tokens:
        errors.append(f"{prefix}.selected_tokens must not exceed raw_tokens when selected")
    return errors


def validate_summary(
    summary: Any,
    files: int,
    selected_files: int,
    raw_tokens: int,
    selected_tokens: int,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(summary, dict):
        return ["report.summary must be an object"]
    require_keys(summary, ["files", "selected_files", "raw_tokens", "selected_tokens", "saved_tokens", "savings_ratio"], "summary", errors)
    expected = {
        "files": files,
        "selected_files": selected_files,
        "raw_tokens": raw_tokens,
        "selected_tokens": selected_tokens,
        "saved_tokens": raw_tokens - selected_tokens,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            errors.append(f"summary.{key} must be {value}")
    expected_ratio = 0.0 if raw_tokens == 0 else 1.0 - (selected_tokens / raw_tokens)
    if not math.isclose(float(summary.get("savings_ratio", -1)), expected_ratio, rel_tol=1e-12, abs_tol=1e-12):
        errors.append("summary.savings_ratio does not match token counts")
    return errors


def validate_files(result: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    source = Path(str(result.get("source", "")))
    read_path = Path(str(result.get("read_path", "")))
    if not source.is_file():
        errors.append(f"{prefix}.source does not exist")
    if not read_path.is_file():
        errors.append(f"{prefix}.read_path does not exist")
    expected_sha = result.get("sha256")
    if expected_sha and source.is_file() and hook.sha256_file(source) != expected_sha:
        errors.append(f"{prefix}.sha256 does not match source")
    if result.get("selected") is True and source.is_file() and read_path.is_file():
        expected_output_sha = result.get("output_sha256")
        if not isinstance(expected_output_sha, str) or not expected_output_sha:
            errors.append(f"{prefix}.output_sha256 must be a non-empty string when selected")
        elif hook.sha256_file(read_path) != expected_output_sha:
            errors.append(f"{prefix}.output_sha256 does not match read_path")
        errors.extend(validate_round_trip(result, source, read_path, prefix))
    return errors


def validate_round_trip(result: dict[str, Any], source_path: Path, read_path: Path, prefix: str) -> list[str]:
    selected_format = result.get("selected_format")
    kind = result.get("kind")
    if not isinstance(selected_format, str) or not selected_format:
        return [f"{prefix}.selected_format is required for round-trip check"]
    if not isinstance(kind, str) or not kind:
        return [f"{prefix}.kind is required for round-trip check"]
    try:
        source = hook.load_source(source_path)
        sidecar_text = read_path.read_text(encoding="utf-8")
        candidate_text = strip_decoder_instructions(sidecar_text, selected_format)
        decoded = hook.decode_candidate_value(selected_format, candidate_text, kind)
    except Exception as exc:
        return [f"{prefix}.read_path failed round-trip decode: {exc}"]
    if decoded != source.value:
        return [f"{prefix}.read_path does not decode to source value"]
    return []


def strip_decoder_instructions(sidecar_text: str, selected_format: str) -> str:
    if selected_format == "raw":
        return sidecar_text
    _, separator, rest = sidecar_text.partition("\n")
    if not separator:
        raise ValueError("selected sidecar is missing decoder instruction line")
    return rest


def require_keys(value: dict[str, Any], keys: list[str], prefix: str, errors: list[str]) -> None:
    for key in keys:
        if key not in value:
            errors.append(f"{prefix}.{key} is required")


if __name__ == "__main__":
    raise SystemExit(main())
