#!/usr/bin/env python3
"""Deterministically verify raw/optimized eval pairs without calling a model."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hook  # noqa: E402
from evals import build_context_quality_dataset as builder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()

    records = load_jsonl(args.dataset)
    errors = verify_records(records)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"context quality dataset ok: {len(records)} records")
    return 0


def verify_records(records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)

    for index, record in enumerate(records):
        for key in ("id", "variant", "source_file", "question_type", "target", "input"):
            if key not in record:
                errors.append(f"record {index} missing {key}")
        source_file = str(record.get("source_file", ""))
        question_type = str(record.get("question_type", ""))
        variant = str(record.get("variant", ""))
        if variant not in {"raw", "optimized"}:
            errors.append(f"record {index} invalid variant {variant!r}")
            continue
        grouped[(source_file, question_type)][variant] = record

    for (source_file, question_type), variants in sorted(grouped.items()):
        if set(variants) != {"raw", "optimized"}:
            errors.append(f"{source_file}/{question_type} must have raw and optimized variants")
            continue
        raw = variants["raw"]
        optimized = variants["optimized"]
        if raw.get("target") != optimized.get("target"):
            errors.append(f"{source_file}/{question_type} raw and optimized targets differ")
            continue
        try:
            source = hook.load_source(Path(source_file))
            rows = hook.rows_from_value(source.value)
            answer = expected_answer(rows, question_type)
        except Exception as exc:
            errors.append(f"{source_file}/{question_type} failed deterministic answer: {exc}")
            continue
        if answer != raw.get("target"):
            errors.append(
                f"{source_file}/{question_type} target mismatch: expected {answer!r}, got {raw.get('target')!r}"
            )
        for variant_name, record in variants.items():
            try:
                context_rows = rows_from_record_context(record, source.kind, source.value)
                context_answer = expected_answer(context_rows, question_type)
            except Exception as exc:
                errors.append(f"{source_file}/{question_type}/{variant_name} failed context answer: {exc}")
                continue
            if context_answer != record.get("target"):
                errors.append(
                    f"{source_file}/{question_type}/{variant_name} context target mismatch: "
                    f"expected {context_answer!r}, got {record.get('target')!r}"
                )
    return errors


def expected_answer(rows: list[dict[str, Any]], question_type: str) -> str:
    cases = builder.build_cases(rows)
    for case in cases:
        if case.question_type == question_type:
            return case.target
    raise ValueError(f"question type not generated: {question_type}")


def rows_from_record_context(record: dict[str, Any], source_kind: str, source_value: Any) -> list[dict[str, Any]]:
    context = extract_data_context(str(record.get("input", "")))
    context_format = str(record.get("context_format") or ("raw" if record.get("variant") == "raw" else ""))
    if context_format == "raw":
        value = hook.decode_candidate_value("raw", context, source_kind)
    elif context_format:
        candidate_text = strip_decoder_instructions(context)
        value = hook.decode_candidate_value(context_format, candidate_text, source_kind)
    else:
        value = infer_optimized_value(context, source_kind, source_value)
    rows = hook.rows_from_value(value)
    if not rows:
        raise ValueError("context did not decode to row data")
    return rows


def extract_data_context(input_text: str) -> str:
    prefix = "Data:\n"
    separator = "\n\nQuestion:"
    if not input_text.startswith(prefix) or separator not in input_text:
        raise ValueError("input must contain Data and Question sections")
    return input_text[len(prefix) : input_text.index(separator)]


def strip_decoder_instructions(context: str) -> str:
    _, separator, rest = context.partition("\n")
    if not separator:
        raise ValueError("optimized context is missing decoder instruction line")
    return rest


def infer_optimized_value(context: str, source_kind: str, source_value: Any) -> Any:
    candidate_text = strip_decoder_instructions(context)
    candidates = sorted(set().union(*hook.CANDIDATE_TIERS.values()) - {"raw"})
    for candidate in candidates:
        try:
            value = hook.decode_candidate_value(candidate, candidate_text, source_kind)
        except Exception:
            continue
        if value == source_value:
            return value
    raise ValueError("optimized context did not decode with any known candidate format")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(record)
    return records


if __name__ == "__main__":
    raise SystemExit(main())
