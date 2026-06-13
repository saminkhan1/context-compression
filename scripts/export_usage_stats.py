#!/usr/bin/env python3
"""Export local selector usage analytics from decision reports."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import verify_selector_report


SCHEMA_VERSION = "context-selector-usage/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports",
        nargs="*",
        type=Path,
        help="Report JSON files to aggregate. Defaults to .codex/context-cache/reports/*.json under --cwd.",
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--include-paths",
        action="store_true",
        help="Include absolute source/read/output/report paths. Default export redacts paths.",
    )
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="Include valid reports and list invalid reports instead of failing.",
    )
    args = parser.parse_args()

    cwd = args.cwd.expanduser().resolve()
    report_paths = resolve_report_paths(args.reports, cwd)
    export, invalid = build_usage_export(report_paths, include_paths=args.include_paths)
    if invalid and not args.allow_invalid:
        for item in invalid:
            print(f"{item['path']}: {'; '.join(item['errors'])}", file=sys.stderr)
        return 1
    if invalid:
        export["invalid_reports"] = invalid
    print(json.dumps(export, indent=2, sort_keys=True))
    return 0


def resolve_report_paths(raw_paths: list[Path], cwd: Path) -> list[Path]:
    if raw_paths:
        return sorted(resolve_path(path, cwd) for path in raw_paths)
    report_dir = cwd / ".codex" / "context-cache" / "reports"
    if not report_dir.is_dir():
        return []
    return sorted(path.resolve() for path in report_dir.glob("*.json") if path.is_file())


def resolve_path(path: Path, cwd: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = cwd / expanded
    return expanded.resolve()


def build_usage_export(report_paths: list[Path], include_paths: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    by_adapter: dict[str, dict[str, Any]] = defaultdict(empty_bucket)
    by_format: dict[str, dict[str, Any]] = defaultdict(empty_bucket)
    by_kind: dict[str, dict[str, Any]] = defaultdict(empty_bucket)
    by_model: dict[str, dict[str, Any]] = defaultdict(empty_bucket)
    summary = empty_bucket()

    valid_reports = 0
    for report_path in report_paths:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append({"path": str(report_path), "errors": [f"invalid JSON: {exc}"]})
            continue

        errors = verify_selector_report.validate_report(report, check_files=False)
        if errors:
            invalid.append({"path": str(report_path), "errors": errors})
            continue

        valid_reports += 1
        adapter = str(report.get("adapter") or "unknown")
        model = str(report.get("model_profile", {}).get("slug") or "unknown")
        for result in report.get("results", []):
            event = event_from_result(report, result, report_path, include_paths)
            events.append(event)
            add_event(summary, event)
            add_event(by_adapter[adapter], event)
            add_event(by_model[model], event)
            add_event(by_kind[event["kind"]], event)
            add_event(by_format[event["selected_format"]], event)

    finalize_bucket(summary)
    for bucket_group in (by_adapter, by_format, by_kind, by_model):
        for bucket in bucket_group.values():
            finalize_bucket(bucket)

    return (
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source": {
                "reports_found": len(report_paths),
                "reports_used": valid_reports,
                "paths_included": include_paths,
            },
            "summary": summary,
            "by_adapter": dict(sorted(by_adapter.items())),
            "by_format": dict(sorted(by_format.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_model": dict(sorted(by_model.items())),
            "events": events,
        },
        invalid,
    )


def event_from_result(
    report: dict[str, Any],
    result: dict[str, Any],
    report_path: Path,
    include_paths: bool,
) -> dict[str, Any]:
    selected = bool(result.get("selected"))
    raw_tokens = int(result.get("raw_tokens", 0) or 0)
    selected_tokens = int(result.get("selected_tokens", raw_tokens) or raw_tokens)
    if not selected:
        selected_tokens = raw_tokens
    saved_tokens = raw_tokens - selected_tokens
    selected_format = str(result.get("selected_format") or "raw")
    model_profile = report.get("model_profile", {})
    event = {
        "adapter": str(report.get("adapter") or "unknown"),
        "model": str(model_profile.get("slug") or "unknown"),
        "token_counter": str(model_profile.get("token_counter") or "unknown"),
        "token_counter_label": str(result.get("token_counter_label") or token_counter_label(model_profile)),
        "source_sha256": result.get("sha256"),
        "output_sha256": result.get("output_sha256"),
        "kind": str(result.get("kind") or Path(str(result.get("source", ""))).suffix.lstrip(".") or "unknown"),
        "selected": selected,
        "decision": str(result.get("decision") or "unknown"),
        "selected_format": selected_format,
        "raw_tokens": raw_tokens,
        "selected_tokens": selected_tokens,
        "saved_tokens": saved_tokens,
        "savings_ratio": 0.0 if raw_tokens == 0 else saved_tokens / raw_tokens,
    }
    if include_paths:
        event.update(
            {
                "report_path": str(report_path),
                "source_name": str(result.get("source_name") or Path(str(result.get("source", ""))).name),
                "source": result.get("source"),
                "read_path": result.get("read_path"),
                "output_path": result.get("output_path"),
            }
        )
    return event


def token_counter_label(model_profile: dict[str, Any]) -> str:
    return "estimated" if model_profile.get("token_counter") == "deterministic-fallback" else "exact"


def empty_bucket() -> dict[str, Any]:
    return {
        "files": 0,
        "selected_files": 0,
        "raw_tokens": 0,
        "selected_tokens": 0,
        "saved_tokens": 0,
        "savings_ratio": 0.0,
    }


def add_event(bucket: dict[str, Any], event: dict[str, Any]) -> None:
    bucket["files"] += 1
    if event["selected"]:
        bucket["selected_files"] += 1
    bucket["raw_tokens"] += int(event["raw_tokens"])
    bucket["selected_tokens"] += int(event["selected_tokens"])
    bucket["saved_tokens"] += int(event["saved_tokens"])


def finalize_bucket(bucket: dict[str, Any]) -> None:
    raw_tokens = int(bucket["raw_tokens"])
    bucket["savings_ratio"] = 0.0 if raw_tokens == 0 else int(bucket["saved_tokens"]) / raw_tokens


if __name__ == "__main__":
    raise SystemExit(main())
