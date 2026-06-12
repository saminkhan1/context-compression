#!/usr/bin/env python3
"""Generate Inspect records that compare raw and optimized structured context."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hook  # noqa: E402


DEFAULT_OUT = Path("evals/context-quality.generated.jsonl")


@dataclass(frozen=True)
class EvalCase:
    question_type: str
    question: str
    target: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("data/benchmark-corpus"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--max-files", type=int, default=0, help="0 means all supported files.")
    args = parser.parse_args()

    records = build_records(args.corpus, args.model, args.max_files)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out} ({len(records)} records)")
    return 0


def build_records(corpus: Path, model: str, max_files: int = 0) -> list[dict[str, Any]]:
    files = sorted(
        path
        for path in corpus.iterdir()
        if path.is_file() and path.suffix.lower() in hook.SUPPORTED_EXTENSIONS
    )
    if max_files:
        files = files[:max_files]

    profile = hook.resolve_model_profile(model, {"model": model}, ROOT)
    cache_dir = ROOT / ".codex" / "context-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for path in files:
        source = hook.load_source(path)
        rows = hook.rows_from_value(source.value)
        if not rows:
            continue
        cases = build_cases(rows)
        if not cases:
            continue
        choice = hook.choose_best(source, profile, cache_dir)
        contexts = {
            "raw": (source.raw_text, "raw"),
            "optimized": (hook.candidate_blob(choice.candidate), choice.candidate.name),
        }
        for variant, (context, context_format) in contexts.items():
            for case in cases:
                records.append(
                    {
                        "id": f"{path.stem}/{variant}/{case.question_type}",
                        "variant": variant,
                        "source_file": str(path),
                        "source_kind": source.kind,
                        "context_format": context_format,
                        "question_type": case.question_type,
                        "input": f"Data:\n{context}\n\nQuestion: {case.question}",
                        "target": case.target,
                    }
                )
    return records


def build_cases(rows: list[dict[str, Any]]) -> list[EvalCase]:
    headers = hook.stable_headers(rows)
    cases = [
        EvalCase("count", "How many rows are in the data? Answer as an integer.", str(len(rows))),
    ]

    lookup = lookup_case(rows, headers)
    if lookup:
        cases.append(lookup)

    numeric = numeric_sum_case(rows, headers)
    if numeric:
        cases.append(numeric)

    nested = nested_value_case(rows, headers)
    if nested:
        cases.append(nested)

    repeated = repeated_value_count_case(rows, headers)
    if repeated:
        cases.append(repeated)

    nullable = nullable_value_case(rows, headers)
    if nullable:
        cases.append(nullable)

    missing = missing_key_case(rows, headers)
    if missing:
        cases.append(missing)

    delimiter = delimiter_string_case(rows, headers)
    if delimiter:
        cases.append(delimiter)

    reconstruction = first_row_case(rows)
    if reconstruction:
        cases.append(reconstruction)

    return cases


def lookup_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    key = first_unique_scalar_header(rows, headers)
    if key is None:
        return None
    target_header = next((header for header in headers if header != key and scalar_values(rows, header)), None)
    if target_header is None:
        return None
    row = rows[min(1, len(rows) - 1)]
    key_value = row.get(key)
    target = row.get(target_header)
    return EvalCase(
        "lookup",
        (
            f"What is the JSON value of field {json_string(target_header)} "
            f"for the row where {json_string(key)} equals {json_value(key_value)}?"
        ),
        json_value(target),
    )


def numeric_sum_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    for header in headers:
        values = [row.get(header) for row in rows]
        if values and all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            return EvalCase(
                "integer_sum",
                f"What is the sum of field {json_string(header)} across all rows? Answer as a JSON number.",
                json_value(sum(values)),
            )
    return None


def nested_value_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    key = first_unique_scalar_header(rows, headers)
    if key is None:
        return None
    for row in rows:
        key_value = row.get(key)
        for header in headers:
            value = row.get(header)
            if isinstance(value, (dict, list)):
                return EvalCase(
                    "nested_value",
                    (
                        f"What is the JSON value of field {json_string(header)} "
                        f"for the row where {json_string(key)} equals {json_value(key_value)}?"
                    ),
                    json_value(value),
                )
    return None


def repeated_value_count_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    for header in headers:
        values = [row.get(header) for row in rows]
        if not values or not all(value is None or isinstance(value, (str, int, float, bool)) for value in values):
            continue
        counts: dict[str, int] = {}
        for value in values:
            encoded = json_value(value)
            counts[encoded] = counts.get(encoded, 0) + 1
        repeated = sorted((value, count) for value, count in counts.items() if count > 1)
        if repeated:
            target_value, count = repeated[0]
            return EvalCase(
                "repeated_value_count",
                (
                    f"How many rows have field {json_string(header)} equal to {target_value}? "
                    "Answer as an integer."
                ),
                str(count),
            )
    return None


def nullable_value_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    key = first_unique_scalar_header(rows, headers)
    if key is None:
        return None
    for row in rows:
        key_value = row.get(key)
        for header in headers:
            if row.get(header) is None:
                return EvalCase(
                    "null_lookup",
                    (
                        f"What is the JSON value of field {json_string(header)} "
                        f"for the row where {json_string(key)} equals {json_value(key_value)}?"
                    ),
                    "null",
                )
    return None


def missing_key_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    key = first_unique_scalar_header(rows, headers)
    if key is None:
        return None
    for row in rows:
        key_value = row.get(key)
        for header in headers:
            if header not in row:
                return EvalCase(
                    "missing_key",
                    (
                        f"Does field {json_string(header)} exist on the row where "
                        f"{json_string(key)} equals {json_value(key_value)}? "
                        "Answer exactly true or false."
                    ),
                    "false",
                )
    return None


def delimiter_string_case(rows: list[dict[str, Any]], headers: list[str]) -> EvalCase | None:
    key = first_unique_scalar_header(rows, headers)
    if key is None:
        return None
    for row in rows:
        key_value = row.get(key)
        for header in headers:
            value = row.get(header)
            if isinstance(value, str) and any(marker in value for marker in (",", "|", "\t", "\n", "\"")):
                return EvalCase(
                    "delimiter_string",
                    (
                        f"What is the exact JSON string value of field {json_string(header)} "
                        f"for the row where {json_string(key)} equals {json_value(key_value)}?"
                    ),
                    json_value(value),
                )
    return None


def first_row_case(rows: list[dict[str, Any]]) -> EvalCase | None:
    if not rows:
        return None
    return EvalCase(
        "first_row",
        "Return the first row as compact JSON with the original field names.",
        json_value(rows[0]),
    )


def first_unique_scalar_header(rows: list[dict[str, Any]], headers: list[str]) -> str | None:
    preferred = sorted(headers, key=lambda header: (header.lower() not in {"id", "uuid", "name"}, headers.index(header)))
    for header in preferred:
        values = [row.get(header) for row in rows]
        if scalar_values(rows, header) and len(set(json_value(value) for value in values)) == len(values):
            return header
    return None


def scalar_values(rows: list[dict[str, Any]], header: str) -> bool:
    return all(
        value is None or isinstance(value, (str, int, float, bool))
        for value in (row.get(header) for row in rows)
    )


def json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
