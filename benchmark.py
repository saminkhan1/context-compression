#!/usr/bin/env python3
"""Build and benchmark a diverse structured-context compression corpus."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import hook


DEFAULT_CORPUS_DIR = Path("data/benchmark-corpus")
DEFAULT_REPORT_JSON = Path("reports/benchmark-report.json")
DEFAULT_REPORT_MD = Path("reports/benchmark-report.md")
FORMATS = ("json", "jsonl", "csv", "tsv")
GITHUB_SOURCE = {
    "slug": "github-top-repos",
    "url": (
        "https://api.github.com/search/repositories"
        "?q=stars:%3E50000&sort=stars&order=desc"
    ),
    "description": (
        "Top-starred public GitHub repositories with nested owner/license "
        "metadata."
    ),
}
# Output column -> GitHub search API field.
GITHUB_FIELDS = {
    "full_name": "full_name",
    "description": "description",
    "language": "language",
    "stars": "stargazers_count",
    "forks": "forks_count",
    "open_issues": "open_issues_count",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "license": "license",
    "owner": "owner",
    "topics": "topics",
}


@dataclass(frozen=True)
class HuggingFaceDatasetSpec:
    slug: str
    dataset: str
    config: str
    split: str
    description: str
    benchmark_role: str
    shape_tags: tuple[str, ...]
    minimum_rows: int | None = None


@dataclass(frozen=True)
class BaselineSpec:
    name: str
    path: Path | None = None
    command_template: str | None = None
    manifest_path: Path | None = None


HF_DATASETS = (
    HuggingFaceDatasetSpec(
        slug="hf-titanic-tabular",
        dataset="julien-c/titanic-survival",
        config="default",
        split="train",
        description="Flat passenger records; numeric, categorical, and names.",
        benchmark_role="flat tabular data with repeated categorical fields",
        shape_tags=("flat", "tabular", "categorical", "numeric"),
        minimum_rows=887,
    ),
    HuggingFaceDatasetSpec(
        slug="hf-amazon-reviews",
        dataset="SetFit/amazon_reviews_multi_en",
        config="default",
        split="train",
        description=(
            "Large text-classification records with ids, labels, and review "
            "text."
        ),
        benchmark_role="long text records with repeated schema and labels",
        shape_tags=("flat", "text", "classification", "labels"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-openassistant",
        dataset="OpenAssistant/oasst1",
        config="default",
        split="train",
        description=(
            "Conversation/message rows with nested moderation and label "
            "metadata."
        ),
        benchmark_role="agent-like conversation data with nested metadata",
        shape_tags=("conversation", "nested", "metadata", "text"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-squad",
        dataset="rajpurkar/squad",
        config="plain_text",
        split="train",
        description="Question-answering records with nested answer spans.",
        benchmark_role="question answering rows with nested answer arrays",
        shape_tags=("qa", "nested", "text", "answers"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-code-doc",
        dataset="codeparrot/github-jupyter-code-to-text",
        config="default",
        split="train",
        description=(
            "Code and documentation records from public GitHub notebooks."
        ),
        benchmark_role="code/documentation rows with long cells",
        shape_tags=("code", "documentation", "long_text", "github"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-loghub-2",
        dataset="bolu61/loghub_2",
        config="default",
        split="train",
        description=(
            "LogHub 2.0 system log lines from the benchmark used by recent "
            "dictionary-compression work."
        ),
        benchmark_role=(
            "paper-aligned repetitive system logs for dictionary-compression "
            "comparison"
        ),
        shape_tags=("logs", "text", "repetitive", "paper_aligned"),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser(
        "build", help="Build a local benchmark corpus from public sources."
    )
    add_build_args(build)
    run = subcommands.add_parser(
        "run", help="Benchmark an existing corpus directory."
    )
    add_run_args(run)
    verify_corpus = subcommands.add_parser(
        "verify-corpus",
        help="Verify corpus size and Hugging Face source coverage.",
    )
    verify_corpus.add_argument(
        "--corpus", type=Path, default=DEFAULT_CORPUS_DIR
    )
    all_cmd = subcommands.add_parser(
        "all", help="Build the corpus and run the benchmark."
    )
    add_build_args(all_cmd.add_argument_group("build"))
    add_run_args(all_cmd)

    args = parser.parse_args()
    if args.command == "verify-corpus":
        errors = validate_publication_corpus(args.corpus)
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 1
        print(f"publication corpus ok: {args.corpus}")
        return 0
    if args.command in ("build", "all"):
        build_corpus(
            args.out,
            args.rows,
            parse_formats(args.formats),
            args.force_download,
            args.allow_partial,
        )
    if args.command in ("run", "all"):
        run_benchmark(args)
    return 0


def add_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--formats", default=",".join(FORMATS))
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")


def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--input-price-per-1m", type=float, default=0.0)
    parser.add_argument("--monthly-calls", type=int, default=0)
    parser.add_argument(
        "--provider-input-tokens-per-second",
        type=float,
        default=0.0,
        help=(
            "Optional downstream input throughput estimate; projects whether "
            "local compression overhead is offset by saved input tokens."
        ),
    )
    parser.add_argument(
        "--baseline-dir",
        action="append",
        type=Path,
        default=[],
        help=(
            "Directory of external baseline encodings named "
            "<source-file>[.txt]. Repeatable."
        ),
    )
    parser.add_argument(
        "--baseline-command",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help=(
            "Generate a matched external baseline for every corpus file. "
            "COMMAND may contain {input} and {output}. Repeatable."
        ),
    )
    parser.add_argument(
        "--baseline-out-dir",
        type=Path,
        default=None,
        help=(
            "Directory for --baseline-command outputs. Defaults to "
            "<json-out parent>/generated-baselines."
        ),
    )
    parser.add_argument(
        "--require-publication-corpus",
        action="store_true",
        help="Reject corpora below the Hugging Face publication bar.",
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument(
        "--markdown-out", type=Path, default=DEFAULT_REPORT_MD
    )
    parser.add_argument(
        "--candidate-tier",
        choices=sorted(hook.CANDIDATE_TIERS),
        default="advanced",
    )


def parse_formats(raw: str) -> tuple[str, ...]:
    formats = tuple(
        item.strip().lower() for item in raw.split(",") if item.strip()
    )
    unsupported = set(formats) - set(FORMATS)
    if unsupported:
        raise SystemExit(
            f"Unsupported format(s): {', '.join(sorted(unsupported))}"
        )
    return formats


def dataset_spec_dict(spec: HuggingFaceDatasetSpec) -> dict[str, Any]:
    return asdict(spec) | {"shape_tags": list(spec.shape_tags)}


def build_corpus(
    out_dir: Path,
    rows_per_source: int,
    formats: tuple[str, ...],
    force_download: bool = False,
    allow_partial: bool = False,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        (
            "huggingface-dataset-viewer",
            dataset_spec_dict(spec),
            min(rows_per_source, spec.minimum_rows or rows_per_source),
            partial(fetch_hf_rows, spec, rows_per_source),
        )
        for spec in HF_DATASETS
    ]
    jobs.append((
        "github-rest-search",
        GITHUB_SOURCE,
        min(rows_per_source, 400),
        partial(fetch_github_top_repositories, rows_per_source),
    ))
    sources = []
    errors = []
    for kind, info, min_rows, fetch in jobs:
        existing = existing_source_summary(
            out_dir, info["slug"], formats, min_rows, force_download
        )
        if existing:
            sources.append({"kind": "local-existing-corpus", **info, **existing})
            continue
        try:
            rows, extra = fetch()
            if len(rows) < min_rows:
                raise RuntimeError(
                    f"only fetched {len(rows)} rows; expected at least "
                    f"{min_rows}"
                )
            write_source_files(out_dir, info["slug"], rows, formats)
            sources.append(
                {"kind": kind, **info, "rows_written": len(rows), **extra}
            )
        # Collected rather than raised so --allow-partial can keep the rest.
        except Exception as exc:
            errors.append(f"{info['slug']}: {exc}")
            sources.append(
                {"kind": kind, **info, "rows_written": 0, "error": str(exc)}
            )

    manifest = {
        "generated_at": now_iso(),
        "rows_per_source_requested": rows_per_source,
        "formats": formats,
        "sources": sources,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if errors and not allow_partial:
        raise SystemExit(
            "Corpus build incomplete. Use --allow-partial to keep a partial "
            "corpus. " + "; ".join(errors)
        )


def existing_source_summary(
    out_dir: Path,
    slug: str,
    formats: tuple[str, ...],
    min_rows: int,
    force_download: bool,
) -> dict[str, Any] | None:
    paths = [out_dir / f"{slug}.{fmt}" for fmt in formats]
    if force_download or not all(
        path.exists() and path.stat().st_size > 0 for path in paths
    ):
        return None
    jsonl_path = out_dir / f"{slug}.jsonl"
    json_path = out_dir / f"{slug}.json"
    rows_written = 0
    if jsonl_path.exists():
        with jsonl_path.open(encoding="utf-8") as handle:
            rows_written = sum(1 for line in handle if line.strip())
    elif json_path.exists():
        rows_written = len(json.loads(json_path.read_text(encoding="utf-8")))
    if rows_written < min_rows:
        return None
    return {"rows_written": rows_written, "reused_existing": True}


def fetch_hf_rows(
    spec: HuggingFaceDatasetSpec, target_rows: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    skipped = 0
    offset = 0
    while len(rows) < target_rows:
        length = min(100, target_rows - len(rows))
        params = urllib.parse.urlencode({
            "dataset": spec.dataset,
            "config": spec.config,
            "split": spec.split,
            "offset": offset,
            "length": length,
        })
        page = fetch_json(
            f"https://datasets-server.huggingface.co/rows?{params}"
        )["rows"]
        for item in page:
            if item.get("truncated_cells"):
                skipped += 1
            else:
                rows.append(item["row"])
        offset += len(page)
        if len(page) < length:
            break
        time.sleep(0.2)
    return rows[:target_rows], {"truncated_rows_skipped": skipped}


def fetch_github_top_repositories(
    target_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    for page in range(1, 11):
        if len(rows) >= target_rows:
            break
        params = urllib.parse.urlencode({
            "q": "stars:>50000",
            "sort": "stars",
            "order": "desc",
            "per_page": min(100, target_rows - len(rows)),
            "page": page,
        })
        items = fetch_json(
            f"https://api.github.com/search/repositories?{params}"
        )["items"]
        if not items:
            break
        rows.extend(
            {column: item.get(field) for column, field in GITHUB_FIELDS.items()}
            for item in items
        )
        time.sleep(0.2)
    return rows[:target_rows], {}


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "context-compression-benchmark"}
    )
    # Five retries with backoff on transient errors, then one final attempt.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            retry_after = exc.headers.get("Retry-After") or ""
            time.sleep(
                float(retry_after) if retry_after.isdigit() else 2**attempt
            )
        except URLError:
            time.sleep(2**attempt)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def write_source_files(
    out_dir: Path,
    slug: str,
    rows: list[dict[str, Any]],
    formats: tuple[str, ...],
) -> None:
    if "json" in formats:
        (out_dir / f"{slug}.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if "jsonl" in formats:
        lines = (
            json.dumps(row, separators=(",", ":"), ensure_ascii=False)
            for row in rows
        )
        (out_dir / f"{slug}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    headers = hook.stable_headers(rows)
    for fmt, delimiter in (("csv", ","), ("tsv", "\t")):
        if fmt not in formats:
            continue
        with (out_dir / f"{slug}.{fmt}").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=headers,
                delimiter=delimiter,
                lineterminator="\n",
            )
            writer.writeheader()
            # csv writes None and missing keys as "", scalars via str().
            for row in rows:
                writer.writerow({
                    key: json.dumps(
                        value, separators=(",", ":"), ensure_ascii=False
                    ) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                })


def validate_publication_corpus(corpus: Path) -> list[str]:
    manifest_path = corpus / "manifest.json"
    if not manifest_path.is_file():
        return [f"{manifest_path} is missing"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hf_sources = [
        source for source in manifest["sources"]
        if source.get("slug", "").startswith("hf-") and source.get("dataset")
    ]
    errors = []
    if len(hf_sources) < len(HF_DATASETS):
        errors.append(
            f"expected at least {len(HF_DATASETS)} Hugging Face sources, "
            f"found {len(hf_sources)}"
        )

    requested_rows = manifest.get("rows_per_source_requested") or 1000
    total_hf_rows = 0
    observed_tags = set()
    for spec in HF_DATASETS:
        key = (spec.slug, spec.dataset, spec.config, spec.split)
        source = next(
            (
                item for item in hf_sources
                if (
                    item.get("slug"), item.get("dataset"),
                    item.get("config"), item.get("split"),
                ) == key
            ),
            None,
        )
        if source is None:
            errors.append(
                f"missing Hugging Face source {spec.slug} "
                f"({spec.dataset}/{spec.config}/{spec.split})"
            )
            continue
        role = source.get("benchmark_role")
        if role is not None and role != spec.benchmark_role:
            errors.append(
                f"{spec.slug} benchmark_role drifted: expected "
                f"{spec.benchmark_role!r}, got {role!r}"
            )
        shape_tags = source.get("shape_tags")
        if shape_tags is None:
            shape_tags = spec.shape_tags
        elif sorted(shape_tags) != sorted(spec.shape_tags):
            errors.append(
                f"{spec.slug} shape_tags drifted: expected "
                f"{sorted(spec.shape_tags)}, got {sorted(shape_tags)}"
            )
        observed_tags.update(map(str, shape_tags))
        rows = source.get("rows_written") or 0
        minimum_rows = spec.minimum_rows or requested_rows
        if rows < minimum_rows:
            errors.append(
                f"{spec.slug} has {rows} rows; expected at least "
                f"{minimum_rows}"
            )
        total_hf_rows += rows
        for fmt in FORMATS:
            path = corpus / f"{spec.slug}.{fmt}"
            if not path.is_file() or path.stat().st_size == 0:
                errors.append(f"missing non-empty corpus file {path}")

    minimum_total_hf_rows = sum(
        spec.minimum_rows or requested_rows for spec in HF_DATASETS
    )
    if total_hf_rows < minimum_total_hf_rows:
        errors.append(
            f"expected at least {minimum_total_hf_rows} Hugging Face rows, "
            f"found {total_hf_rows}"
        )
    required_tags = {
        "flat", "tabular", "nested", "text", "conversation", "qa", "code",
        "long_text", "logs", "repetitive",
    }
    missing_tags = sorted(required_tags - observed_tags)
    if missing_tags:
        errors.append(
            "publication corpus is missing required shape coverage: "
            + ", ".join(missing_tags)
        )
    expected_files = len(HF_DATASETS) * len(FORMATS)
    found_files = len(supported_corpus_files(corpus))
    if found_files < expected_files:
        errors.append(
            f"expected at least {expected_files} supported Hugging Face "
            f"files, found {found_files}"
        )
    return errors


def run_benchmark(args: argparse.Namespace) -> None:
    corpus = args.corpus
    if args.require_publication_corpus:
        errors = validate_publication_corpus(corpus)
        if errors:
            raise SystemExit(
                "Corpus does not meet publication benchmark bar:\n"
                + "\n".join(errors)
            )
    files = supported_corpus_files(corpus)
    if not files:
        raise SystemExit(f"No supported corpus files found in {corpus}")

    profile = hook.resolve_model_profile(
        args.model, {"model": args.model}, Path.cwd()
    )
    baselines = resolve_baseline_specs(args.baseline_dir) + run_baseline_commands(
        resolve_baseline_command_specs(args.baseline_command),
        files,
        args.baseline_out_dir or args.json_out.parent / "generated-baselines",
    )
    price = args.input_price_per_1m
    calls = args.monthly_calls
    provider_tps = args.provider_input_tokens_per_second
    # Programmatic callers may omit candidate_tier; the CLI always sets it.
    candidate_tier = getattr(args, "candidate_tier", "advanced")
    results = [
        benchmark_file(
            path, profile, price, calls, baselines, provider_tps,
            candidate_tier,
        )
        for path in files
    ]
    manifest = corpus / "manifest.json"
    report = {
        "generated_at": now_iso(),
        "corpus": str(corpus),
        "corpus_fingerprint": {
            "manifest_sha256": (
                hook.sha256_file(manifest) if manifest.exists() else None
            ),
            "files": [
                {
                    "file": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hook.sha256_file(path),
                }
                for path in files
            ],
        },
        "model_profile": asdict(profile),
        "pricing": {
            "input_price_per_1m": price,
            "monthly_calls": calls,
            "provider_input_tokens_per_second": provider_tps,
        },
        "baseline_dirs": {
            baseline.name: str(baseline.path) for baseline in baselines
        },
        "baseline_provenance": {
            baseline.name: baseline_provenance(baseline)
            for baseline in baselines
        },
        "candidate_tier": candidate_tier,
        "totals": aggregate_results(
            results, baselines, price, calls, provider_tps
        ),
        "results": results,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(markdown_report(report), encoding="utf-8")
    print(f"Wrote {args.json_out}")
    print(f"Wrote {args.markdown_out}")


def supported_corpus_files(corpus: Path) -> list[Path]:
    return sorted(
        path
        for path in corpus.iterdir()
        if path.is_file()
        and path.name != "manifest.json"
        and path.suffix.lower() in hook.SUPPORTED_EXTENSIONS
    )


def benchmark_file(
    path: Path,
    profile: hook.ModelProfile,
    input_price_per_1m: float,
    monthly_calls: int,
    baselines: list[BaselineSpec],
    provider_input_tokens_per_second: float,
    candidate_tier: str,
) -> dict[str, Any]:
    start = time.perf_counter()
    source = hook.load_source(path)
    loaded = time.perf_counter()
    raw_tokens = hook.count_tokens(source.raw_text, profile)
    raw_counted = time.perf_counter()
    candidates = hook.candidates_for_profile(source, profile, candidate_tier)
    generated = time.perf_counter()
    rows = []
    for candidate in candidates:
        total_tokens, payload_tokens, instruction_tokens = (
            hook.candidate_token_metrics(candidate, profile)
        )
        rows.append({
            "candidate": candidate.name,
            "total_tokens": total_tokens,
            "payload_tokens": payload_tokens,
            "instruction_tokens": instruction_tokens,
            **token_savings_fields(
                raw_tokens,
                raw_tokens - total_tokens,
                input_price_per_1m,
                monthly_calls,
            ),
        })
    counted = time.perf_counter()
    rows.sort(key=lambda row: (row["total_tokens"], row["candidate"]))
    best = rows[0]

    baseline_rows = []
    for baseline in sorted(baselines, key=lambda baseline: baseline.name):
        baseline_path = resolve_baseline_file(baseline.path, path.name)
        if baseline_path is None:
            baseline_rows.append({
                "baseline": baseline.name,
                "available": False,
                "source_file": None,
            })
            continue
        total_tokens = hook.count_tokens(
            baseline_path.read_text(encoding="utf-8"), profile
        )
        baseline_rows.append({
            "baseline": baseline.name,
            "available": True,
            "source_file": str(baseline_path),
            "bytes": baseline_path.stat().st_size,
            "total_tokens": total_tokens,
            **token_savings_fields(
                raw_tokens,
                raw_tokens - total_tokens,
                input_price_per_1m,
                monthly_calls,
            ),
        })
    timings = {
        "load_milliseconds": (loaded - start) * 1000,
        "candidate_generation_milliseconds": (generated - raw_counted) * 1000,
        "token_count_milliseconds": (
            (raw_counted - loaded) + (counted - generated)
        ) * 1000,
        "total_milliseconds": (time.perf_counter() - start) * 1000,
    }
    return {
        "file": str(path),
        "kind": source.kind,
        "bytes": path.stat().st_size,
        "raw_tokens": raw_tokens,
        "best": best,
        "candidates": rows,
        "external_baselines": baseline_rows,
        "latency": latency_summary(
            timings, best["token_delta"], provider_input_tokens_per_second
        ),
    }


def latency_summary(
    timings: dict[str, float],
    token_delta: int,
    provider_input_tokens_per_second: float,
) -> dict[str, Any]:
    """Add throughput and break-even projections to raw stage timings."""
    local_ms = timings["total_milliseconds"]
    max_tps = 0.0
    if token_delta > 0 and local_ms > 0:
        max_tps = token_delta / (local_ms / 1000.0)
    saved_ms = None
    net_saved_ms = None
    if max_tps and provider_input_tokens_per_second > 0:
        saved_ms = token_delta * 1000.0 / provider_input_tokens_per_second
        net_saved_ms = saved_ms - local_ms
    return timings | {
        "tokens_saved_per_millisecond": (
            0.0 if local_ms == 0 else token_delta / local_ms
        ),
        "break_even": {
            "max_provider_input_tokens_per_second_for_break_even": max_tps,
            "projected_input_latency_saved_milliseconds": saved_ms,
            "projected_net_latency_saved_milliseconds": net_saved_ms,
            "projected_break_even": (
                None if net_saved_ms is None else net_saved_ms > 0
            ),
        },
    }


def aggregate_results(
    results: list[dict[str, Any]],
    baselines: list[BaselineSpec],
    input_price_per_1m: float,
    monthly_calls: int,
    provider_input_tokens_per_second: float,
) -> dict[str, Any]:
    savings = partial(
        token_savings_fields,
        input_price_per_1m=input_price_per_1m,
        monthly_calls=monthly_calls,
    )
    raw_tokens = sum(item["raw_tokens"] for item in results)
    optimized_tokens = sum(item["best"]["total_tokens"] for item in results)
    token_delta = raw_tokens - optimized_tokens
    timings = {
        field: sum(item["latency"][field] for item in results)
        for field in (
            "load_milliseconds",
            "candidate_generation_milliseconds",
            "token_count_milliseconds",
            "total_milliseconds",
        )
    }
    return {
        "files": len(results),
        "raw_tokens": raw_tokens,
        "optimized_tokens": optimized_tokens,
        **savings(raw_tokens, token_delta),
        "by_extension": aggregate_by_key(
            results,
            lambda path: path.suffix.lower().lstrip("."),
            savings,
        ),
        "by_source": aggregate_by_key(
            results, lambda path: path.stem, savings
        ),
        "latency": latency_summary(
            timings, token_delta, provider_input_tokens_per_second
        ),
        "external_baselines": aggregate_external_baselines(
            results, baselines, savings
        ),
        "candidate_ablation": aggregate_candidate_ablation(results, savings),
    }


def aggregate_by_key(results, key_fn, savings) -> dict[str, dict[str, Any]]:
    grouped = {}
    for item in results:
        best = item["best"]
        bucket = grouped.setdefault(key_fn(Path(item["file"])), {
            "files": 0,
            "raw_tokens": 0,
            "optimized_tokens": 0,
            "token_delta": 0,
            "best_formats": {},
        })
        bucket["files"] += 1
        bucket["raw_tokens"] += item["raw_tokens"]
        bucket["optimized_tokens"] += best["total_tokens"]
        bucket["token_delta"] += item["raw_tokens"] - best["total_tokens"]
        formats = bucket["best_formats"]
        formats[best["candidate"]] = formats.get(best["candidate"], 0) + 1
    for bucket in grouped.values():
        bucket.update(savings(bucket["raw_tokens"], bucket["token_delta"]))
        bucket["best_formats"] = dict(sorted(bucket["best_formats"].items()))
    return dict(sorted(grouped.items()))


def aggregate_external_baselines(
    results, baselines, savings
) -> dict[str, dict[str, Any]]:
    summary = {
        baseline.name: {
            "files_available": 0,
            "files_missing": 0,
            "raw_tokens": 0,
            "baseline_tokens": 0,
            "token_delta": 0,
            "wins_vs_selector": 0,
            "ties_vs_selector": 0,
            "losses_vs_selector": 0,
        }
        for baseline in baselines
    }
    for item in results:
        best_tokens = item["best"]["total_tokens"]
        for row in item["external_baselines"]:
            bucket = summary[row["baseline"]]
            if not row["available"]:
                bucket["files_missing"] += 1
                continue
            bucket["files_available"] += 1
            bucket["raw_tokens"] += item["raw_tokens"]
            bucket["baseline_tokens"] += row["total_tokens"]
            bucket["token_delta"] += row["token_delta"]
            if row["total_tokens"] < best_tokens:
                bucket["wins_vs_selector"] += 1
            elif row["total_tokens"] == best_tokens:
                bucket["ties_vs_selector"] += 1
            else:
                bucket["losses_vs_selector"] += 1
    for bucket in summary.values():
        bucket.update(savings(bucket["raw_tokens"], bucket["token_delta"]))
    return summary


def aggregate_candidate_ablation(
    results, savings
) -> dict[str, dict[str, Any]]:
    summary = {}
    for item in results:
        for rank, candidate in enumerate(item["candidates"], 1):
            name = candidate["candidate"]
            bucket = summary.setdefault(name, {
                "files_available": 0,
                "wins": 0,
                "raw_tokens": 0,
                "candidate_tokens": 0,
                "token_delta": 0,
                "rank_sum": 0,
                "rank_best": rank,
                "rank_worst": rank,
            })
            bucket["files_available"] += 1
            bucket["wins"] += int(name == item["best"]["candidate"])
            bucket["raw_tokens"] += item["raw_tokens"]
            bucket["candidate_tokens"] += candidate["total_tokens"]
            bucket["token_delta"] += candidate["token_delta"]
            bucket["rank_sum"] += rank
            bucket["rank_best"] = min(bucket["rank_best"], rank)
            bucket["rank_worst"] = max(bucket["rank_worst"], rank)
    for bucket in summary.values():
        bucket.update(savings(bucket["raw_tokens"], bucket["token_delta"]))
        bucket["average_rank"] = bucket["rank_sum"] / bucket["files_available"]
    return dict(sorted(
        summary.items(),
        key=lambda item: (-item[1]["wins"], item[1]["average_rank"], item[0]),
    ))


def token_savings_fields(
    raw_tokens: int,
    token_delta: int,
    input_price_per_1m: float,
    monthly_calls: int,
) -> dict[str, float | int]:
    return {
        "token_delta": token_delta,
        "savings_ratio": 0.0 if raw_tokens == 0 else token_delta / raw_tokens,
        "per_call_input_cost_saved": (
            token_delta * input_price_per_1m / 1_000_000
        ),
        "monthly_input_cost_saved": (
            token_delta * input_price_per_1m * monthly_calls / 1_000_000
        ),
    }


def resolve_baseline_specs(paths: list[Path]) -> list[BaselineSpec]:
    specs = {}
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"Baseline directory not found: {path}")
        if path.name in specs:
            raise SystemExit(f"Duplicate baseline directory name: {path.name}")
        specs[path.name] = BaselineSpec(name=path.name, path=path)
    return list(specs.values())


def resolve_baseline_command_specs(raw_specs: list[str]) -> list[BaselineSpec]:
    specs = {}
    for raw in raw_specs:
        name, separator, command = (part.strip() for part in raw.partition("="))
        if not separator:
            raise SystemExit(f"Baseline command must be NAME=COMMAND: {raw}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SystemExit(f"Invalid baseline command name: {name!r}")
        if not command:
            raise SystemExit(f"Baseline command is empty for {name}")
        if name in specs:
            raise SystemExit(f"Duplicate baseline command name: {name}")
        specs[name] = BaselineSpec(name=name, command_template=command)
    return list(specs.values())


def run_baseline_commands(
    specs: list[BaselineSpec], files: list[Path], out_dir: Path
) -> list[BaselineSpec]:
    generated = []
    for spec in specs:
        baseline_dir = out_dir / spec.name
        baseline_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for source in files:
            output = baseline_dir / f"{source.name}.txt"
            output.unlink(missing_ok=True)
            command = spec.command_template.format(
                input=shlex.quote(str(source)),
                output=shlex.quote(str(output)),
            )
            subprocess.run(command, shell=True, check=True, timeout=300)
            assert output.is_file() and output.stat().st_size > 0, (
                f"Baseline command {spec.name!r} did not write a non-empty "
                f"output for {source}"
            )
            entries.append({
                "source": str(source),
                "source_sha256": hook.sha256_file(source),
                "output": str(output),
                "output_sha256": hook.sha256_file(output),
                "command": command,
                "bytes": output.stat().st_size,
            })
        manifest_path = baseline_dir / "baseline-provenance.json"
        manifest = {
            "schema_version": "baseline-provenance/v1",
            "name": spec.name,
            "generated_at": now_iso(),
            "command_template": spec.command_template,
            "files": entries,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        generated.append(replace(
            spec,
            path=baseline_dir.resolve(),
            manifest_path=manifest_path.resolve(),
        ))
    return generated


def baseline_provenance(baseline: BaselineSpec) -> dict[str, str]:
    if baseline.command_template is None:
        return {"source": "directory", "path": str(baseline.path)}
    return {
        "source": "command",
        "path": str(baseline.path),
        "command_template": baseline.command_template,
        "manifest_path": str(baseline.manifest_path),
    }


def resolve_baseline_file(baseline_dir: Path, source_name: str) -> Path | None:
    candidates = (
        baseline_dir / source_name, baseline_dir / f"{source_name}.txt"
    )
    return next((path for path in candidates if path.is_file()), None)


def markdown_table(
    title: str, header: str, align: str, rows: list[str]
) -> list[str]:
    return ["", f"## {title}", "", header, align, *rows]


def markdown_report(report: dict[str, Any]) -> str:
    pricing = report["pricing"]
    totals = report["totals"]
    latency = totals["latency"]
    break_even = latency["break_even"]
    manifest_sha = report["corpus_fingerprint"]["manifest_sha256"] or "none"
    profile = report["model_profile"]
    lines = [
        "# Context Compression Benchmark Report",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Corpus: `{report['corpus']}`",
        f"Corpus manifest SHA-256: `{manifest_sha}`",
        f"Model: `{profile['slug']}` via `{profile['token_counter']}`",
        f"Input price: `${pricing['input_price_per_1m']:.4f}` per 1M tokens",
        f"Monthly calls: `{pricing['monthly_calls']}`",
    ]
    lines += markdown_table(
        "Totals",
        "| Files | Raw tokens | Optimized tokens | Saved tokens | Savings "
        "| Saved / call | Saved / month |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        [
            f"| {totals['files']} | {totals['raw_tokens']} | "
            f"{totals['optimized_tokens']} | {totals['token_delta']} | "
            f"{totals['savings_ratio']:.1%} | "
            f"${totals['per_call_input_cost_saved']:.6f} | "
            f"${totals['monthly_input_cost_saved']:.2f} |"
        ],
    )
    lines += markdown_table(
        "Local Processing Time",
        "| Load ms | Candidate ms | Token-count ms | Total ms "
        "| Saved tokens / ms | Break-even max input tok/s |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        [
            f"| {latency['load_milliseconds']:.1f} | "
            f"{latency['candidate_generation_milliseconds']:.1f} | "
            f"{latency['token_count_milliseconds']:.1f} | "
            f"{latency['total_milliseconds']:.1f} | "
            f"{latency['tokens_saved_per_millisecond']:.1f} | "
            f"{break_even['max_provider_input_tokens_per_second_for_break_even']:.1f} |"
        ],
    )
    lines += [
        "",
        "Break-even interpretation: compression is latency-positive only "
        "when the downstream model's input throughput is at or below the "
        "break-even ceiling above.",
    ]
    lines += markdown_table(
        "By Format",
        "| Format | Files | Raw tokens | Optimized tokens | Saved tokens "
        "| Savings |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        [
            f"| `{extension}` | {bucket['files']} | {bucket['raw_tokens']} | "
            f"{bucket['optimized_tokens']} | {bucket['token_delta']} | "
            f"{bucket['savings_ratio']:.1%} |"
            for extension, bucket in totals["by_extension"].items()
        ],
    )
    lines += markdown_table(
        "By Source Dataset",
        "| Source | Files | Raw tokens | Optimized tokens | Saved tokens "
        "| Savings | Winning formats |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        [
            f"| `{source}` | {bucket['files']} | {bucket['raw_tokens']} | "
            f"{bucket['optimized_tokens']} | {bucket['token_delta']} | "
            f"{bucket['savings_ratio']:.1%} | "
            + ", ".join(
                f"{name} x{count}"
                for name, count in bucket["best_formats"].items()
            )
            + " |"
            for source, bucket in totals["by_source"].items()
        ],
    )
    saved_ms = break_even["projected_input_latency_saved_milliseconds"]
    if saved_ms is not None:
        net_ms = break_even["projected_net_latency_saved_milliseconds"]
        verdict = "yes" if break_even["projected_break_even"] else "no"
        lines += markdown_table(
            "Projected Latency At Configured Throughput",
            "| Configured input tok/s | API-side ms saved "
            "| Net ms saved after local overhead | Break-even |",
            "| ---: | ---: | ---: | --- |",
            [
                f"| {pricing['provider_input_tokens_per_second']:.1f} | "
                f"{saved_ms:.1f} | {net_ms:.1f} | {verdict} |"
            ],
        )
    if totals["external_baselines"]:
        lines += markdown_table(
            "External Baselines",
            "| Baseline | Files | Missing | Tokens | Savings | Saved / month "
            "| W/T/L vs selector |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            [
                f"| `{name}` | {bucket['files_available']} | "
                f"{bucket['files_missing']} | {bucket['baseline_tokens']} | "
                f"{bucket['savings_ratio']:.1%} | "
                f"${bucket['monthly_input_cost_saved']:.2f} | "
                f"{bucket['wins_vs_selector']}/{bucket['ties_vs_selector']}/"
                f"{bucket['losses_vs_selector']} |"
                for name, bucket in sorted(totals["external_baselines"].items())
            ],
        )
    lines += markdown_table(
        "Candidate Ablation",
        "| Candidate | Files | Wins | Tokens | Savings | Avg rank "
        "| Rank range |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        [
            f"| `{name}` | {bucket['files_available']} | {bucket['wins']} | "
            f"{bucket['candidate_tokens']} | {bucket['savings_ratio']:.1%} | "
            f"{bucket['average_rank']:.2f} | "
            f"{bucket['rank_best']}-{bucket['rank_worst']} |"
            for name, bucket in totals["candidate_ablation"].items()
        ],
    )
    lines += markdown_table(
        "Files",
        "| File | Kind | Bytes | Best format | Raw tokens | Optimized tokens "
        "| Savings |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: |",
        [
            f"| `{Path(item['file']).name}` | {item['kind']} | "
            f"{item['bytes']} | {item['best']['candidate']} | "
            f"{item['raw_tokens']} | {item['best']['total_tokens']} | "
            f"{item['best']['savings_ratio']:.1%} |"
            for item in report["results"]
        ],
    )
    return "\n".join(lines) + "\n"


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
