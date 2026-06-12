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
from urllib.error import HTTPError, URLError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import hook


DEFAULT_CORPUS_DIR = Path("data/benchmark-corpus")
DEFAULT_REPORT_JSON = Path("reports/benchmark-report.json")
DEFAULT_REPORT_MD = Path("reports/benchmark-report.md")
DATASET_VIEWER_PAGE_SIZE = 100


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


def dataset_spec_dict(spec: HuggingFaceDatasetSpec) -> dict[str, Any]:
    data = asdict(spec)
    data["shape_tags"] = list(spec.shape_tags)
    return data


@dataclass(frozen=True)
class BaselineSpec:
    name: str
    path: Path
    source: str = "directory"
    command_template: str | None = None
    manifest_path: Path | None = None


@dataclass(frozen=True)
class BaselineCommandSpec:
    name: str
    command_template: str


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
        description="Large text-classification records with ids, labels, and review text.",
        benchmark_role="long text records with repeated schema and labels",
        shape_tags=("flat", "text", "classification", "labels"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-openassistant",
        dataset="OpenAssistant/oasst1",
        config="default",
        split="train",
        description="Conversation/message rows with nested moderation and label metadata.",
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
        description="Code and documentation records from public GitHub notebooks.",
        benchmark_role="code/documentation rows with long cells",
        shape_tags=("code", "documentation", "long_text", "github"),
    ),
    HuggingFaceDatasetSpec(
        slug="hf-loghub-2",
        dataset="bolu61/loghub_2",
        config="default",
        split="train",
        description="LogHub 2.0 system log lines from the benchmark used by recent dictionary-compression work.",
        benchmark_role="paper-aligned repetitive system logs for dictionary-compression comparison",
        shape_tags=("logs", "text", "repetitive", "paper_aligned"),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="Build a local benchmark corpus from public sources.")
    add_build_args(build)

    run = subcommands.add_parser("run", help="Benchmark an existing corpus directory.")
    add_run_args(run)

    verify_corpus = subcommands.add_parser("verify-corpus", help="Verify corpus size and Hugging Face source coverage.")
    verify_corpus.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)

    all_cmd = subcommands.add_parser("all", help="Build the corpus and run the benchmark.")
    add_build_args(all_cmd.add_argument_group("build"))
    add_run_args(all_cmd)

    args = parser.parse_args()
    if args.command == "build":
        build_corpus(
            args.out,
            args.rows,
            parse_formats(args.formats),
            args.allow_truncated,
            args.force_download,
            args.allow_partial,
        )
        return 0
    if args.command == "run":
        run_benchmark(args)
        return 0
    if args.command == "verify-corpus":
        errors = validate_publication_corpus(args.corpus)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"publication corpus ok: {args.corpus}")
        return 0
    if args.command == "all":
        build_corpus(
            args.out,
            args.rows,
            parse_formats(args.formats),
            args.allow_truncated,
            args.force_download,
            args.allow_partial,
        )
        run_benchmark(args)
        return 0
    raise AssertionError(args.command)


def add_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--formats", default="json,jsonl,csv,tsv")
    parser.add_argument("--allow-truncated", action="store_true")
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
            "Optional estimate of the downstream model's input-processing throughput. "
            "When set, the report projects whether local compression overhead is offset by saved input tokens."
        ),
    )
    parser.add_argument(
        "--baseline-dir",
        action="append",
        type=Path,
        default=[],
        help="Directory containing external baseline encodings named <source-file>[.txt]. Repeat for multiple baselines.",
    )
    parser.add_argument(
        "--baseline-command",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help=(
            "Generate a matched external baseline for every corpus file before benchmarking. "
            "COMMAND may contain {input} and {output}; generated files are counted with the same tokenizer. "
            "Repeat for multiple baselines."
        ),
    )
    parser.add_argument(
        "--baseline-out-dir",
        type=Path,
        default=None,
        help="Directory for --baseline-command outputs. Defaults to <json-out parent>/generated-baselines.",
    )
    parser.add_argument(
        "--require-publication-corpus",
        action="store_true",
        help="Reject tiny/toy corpora; require the Hugging Face benchmark corpus evidence bar before running.",
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument(
        "--candidate-tier",
        choices=sorted(hook.CANDIDATE_TIERS),
        default="advanced",
        help="Candidate tier to benchmark. Defaults to advanced to preserve the checked-in evidence surface.",
    )


def parse_formats(raw: str) -> tuple[str, ...]:
    formats = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    unsupported = set(formats) - {"json", "jsonl", "csv", "tsv"}
    if unsupported:
        raise SystemExit(f"Unsupported format(s): {', '.join(sorted(unsupported))}")
    return formats


def build_corpus(
    out_dir: Path,
    rows_per_source: int,
    formats: tuple[str, ...],
    allow_truncated: bool,
    force_download: bool = False,
    allow_partial: bool = False,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "generated_at": now_iso(),
        "rows_per_source_requested": rows_per_source,
        "formats": formats,
        "sources": [],
    }
    errors: list[str] = []

    for spec in HF_DATASETS:
        min_rows = min(rows_per_source, spec.minimum_rows or rows_per_source)
        existing = existing_source_summary(out_dir, spec.slug, formats, min_rows, force_download)
        if existing:
            manifest["sources"].append(
                {
                    "kind": "local-existing-corpus",
                    **dataset_spec_dict(spec),
                    **existing,
                }
            )
            continue
        try:
            rows, skipped = fetch_hf_rows(spec, rows_per_source, allow_truncated)
            if len(rows) < min_rows:
                raise RuntimeError(f"only fetched {len(rows)} rows; expected at least {min_rows}")
            write_source_files(out_dir, spec.slug, rows, formats)
            manifest["sources"].append(
                {
                    "kind": "huggingface-dataset-viewer",
                    **dataset_spec_dict(spec),
                    "rows_written": len(rows),
                    "truncated_rows_skipped": skipped,
                }
            )
        except Exception as exc:
            errors.append(f"{spec.slug}: {exc}")
            manifest["sources"].append(
                {
                    "kind": "huggingface-dataset-viewer",
                    **dataset_spec_dict(spec),
                    "rows_written": 0,
                    "error": str(exc),
                }
            )

    existing = existing_source_summary(
        out_dir,
        "github-top-repos",
        formats,
        min(rows_per_source, 400),
        force_download,
    )
    if existing:
        manifest["sources"].append(
            {
                "kind": "local-existing-corpus",
                "slug": "github-top-repos",
                "description": "Top-starred public GitHub repositories with nested owner/license metadata.",
                **existing,
            }
        )
    else:
        try:
            github_rows = fetch_github_top_repositories(rows_per_source)
            if len(github_rows) < min(rows_per_source, 400):
                raise RuntimeError(f"only fetched {len(github_rows)} rows; expected at least {min(rows_per_source, 400)}")
            write_source_files(out_dir, "github-top-repos", github_rows, formats)
            manifest["sources"].append(
                {
                    "kind": "github-rest-search",
                    "slug": "github-top-repos",
                    "url": "https://api.github.com/search/repositories?q=stars:%3E50000&sort=stars&order=desc",
                    "description": "Top-starred public GitHub repositories with nested owner/license metadata.",
                    "rows_written": len(github_rows),
                }
            )
        except Exception as exc:
            errors.append(f"github-top-repos: {exc}")
            manifest["sources"].append(
                {
                    "kind": "github-rest-search",
                    "slug": "github-top-repos",
                    "rows_written": 0,
                    "error": str(exc),
                }
            )

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if errors and not allow_partial:
        raise SystemExit("Corpus build incomplete. Use --allow-partial to keep a partial corpus. " + "; ".join(errors))


def existing_source_summary(
    out_dir: Path,
    slug: str,
    formats: tuple[str, ...],
    min_rows: int,
    force_download: bool,
) -> dict[str, Any] | None:
    if force_download:
        return None
    files = [out_dir / f"{slug}.{fmt}" for fmt in formats]
    if not all(path.exists() and path.stat().st_size > 0 for path in files):
        return None
    rows_written = count_existing_rows(out_dir, slug)
    if rows_written < min_rows:
        return None
    return {
        "rows_written": rows_written,
        "reused_existing": True,
    }


def count_existing_rows(out_dir: Path, slug: str) -> int:
    jsonl_path = out_dir / f"{slug}.jsonl"
    if jsonl_path.exists():
        with jsonl_path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    json_path = out_dir / f"{slug}.json"
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    return 0


def fetch_hf_rows(
    spec: HuggingFaceDatasetSpec,
    target_rows: int,
    allow_truncated: bool,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    offset = 0
    while len(rows) < target_rows:
        length = min(DATASET_VIEWER_PAGE_SIZE, target_rows - len(rows))
        params = urllib.parse.urlencode(
            {
                "dataset": spec.dataset,
                "config": spec.config,
                "split": spec.split,
                "offset": offset,
                "length": length,
            }
        )
        data = fetch_json(f"https://datasets-server.huggingface.co/rows?{params}")
        page = data.get("rows", [])
        if not page:
            break
        for item in page:
            truncated = item.get("truncated_cells") or []
            if truncated and not allow_truncated:
                skipped += 1
                continue
            row = item.get("row")
            if isinstance(row, dict):
                rows.append(row)
        offset += len(page)
        if len(page) < length:
            break
        time.sleep(0.2)
    return rows[:target_rows], skipped


def fetch_github_top_repositories(target_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < target_rows and page <= 10:
        per_page = min(100, target_rows - len(rows))
        params = urllib.parse.urlencode(
            {
                "q": "stars:>50000",
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            }
        )
        data = fetch_json(f"https://api.github.com/search/repositories?{params}")
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            rows.append(
                {
                    "full_name": item.get("full_name"),
                    "description": item.get("description"),
                    "language": item.get("language"),
                    "stars": item.get("stargazers_count"),
                    "forks": item.get("forks_count"),
                    "open_issues": item.get("open_issues_count"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "license": item.get("license"),
                    "owner": item.get("owner"),
                    "topics": item.get("topics", []),
                }
            )
        page += 1
        time.sleep(0.2)
    return rows[:target_rows]


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "context-compression-benchmark"})
    delay = 1.0
    last_error: Exception | None = None
    for _ in range(6):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            retry_after = exc.headers.get("Retry-After")
            sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else delay
            time.sleep(sleep_for)
            delay *= 2
        except URLError as exc:
            last_error = exc
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def write_source_files(out_dir: Path, slug: str, rows: list[dict[str, Any]], formats: tuple[str, ...]) -> None:
    if "json" in formats:
        (out_dir / f"{slug}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    if "jsonl" in formats:
        (out_dir / f"{slug}.jsonl").write_text(
            "\n".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
    if "csv" in formats:
        write_delimited(out_dir / f"{slug}.csv", rows, ",")
    if "tsv" in formats:
        write_delimited(out_dir / f"{slug}.tsv", rows, "\t")


def write_delimited(path: Path, rows: list[dict[str, Any]], delimiter: str) -> None:
    headers = hook.stable_headers(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({header: delimited_cell(row.get(header)) for header in headers})


def delimited_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def validate_publication_corpus(corpus: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = corpus / "manifest.json"
    if not manifest_path.is_file():
        return [f"{manifest_path} is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{manifest_path} is not valid JSON: {exc}"]

    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return ["manifest.sources must be a list"]

    hf_sources = [
        source for source in sources
        if isinstance(source, dict) and str(source.get("slug", "")).startswith("hf-") and source.get("dataset")
    ]
    if len(hf_sources) < len(HF_DATASETS):
        errors.append(f"expected at least {len(HF_DATASETS)} Hugging Face sources, found {len(hf_sources)}")

    requested_rows = int(manifest.get("rows_per_source_requested", 0) or 1000)
    total_hf_rows = 0
    observed_tags: set[str] = set()
    for spec in HF_DATASETS:
        source = next(
            (
                item for item in hf_sources
                if item.get("slug") == spec.slug
                and item.get("dataset") == spec.dataset
                and item.get("config") == spec.config
                and item.get("split") == spec.split
            ),
            None,
        )
        if source is None:
            errors.append(f"missing Hugging Face source {spec.slug} ({spec.dataset}/{spec.config}/{spec.split})")
            continue
        role = source.get("benchmark_role")
        if role is not None and role != spec.benchmark_role:
            errors.append(f"{spec.slug} benchmark_role drifted: expected {spec.benchmark_role!r}, got {role!r}")
        shape_tags = source.get("shape_tags")
        if shape_tags is not None:
            if sorted(shape_tags) != sorted(spec.shape_tags):
                errors.append(f"{spec.slug} shape_tags drifted: expected {sorted(spec.shape_tags)}, got {sorted(shape_tags)}")
            observed_tags.update(str(tag) for tag in shape_tags)
        else:
            observed_tags.update(spec.shape_tags)
        rows = int(source.get("rows_written", 0) or 0)
        minimum_rows = spec.minimum_rows or requested_rows
        if rows < minimum_rows:
            errors.append(f"{spec.slug} has {rows} rows; expected at least {minimum_rows}")
        total_hf_rows += rows
        for fmt in ("json", "jsonl", "csv", "tsv"):
            path = corpus / f"{spec.slug}.{fmt}"
            if not path.is_file() or path.stat().st_size == 0:
                errors.append(f"missing non-empty corpus file {path}")

    minimum_total_hf_rows = sum(spec.minimum_rows or requested_rows for spec in HF_DATASETS)
    if total_hf_rows < minimum_total_hf_rows:
        errors.append(f"expected at least {minimum_total_hf_rows} Hugging Face rows, found {total_hf_rows}")

    required_tags = {"flat", "tabular", "nested", "text", "conversation", "qa", "code", "long_text", "logs", "repetitive"}
    missing_tags = sorted(required_tags - observed_tags)
    if missing_tags:
        errors.append(f"publication corpus is missing required shape coverage: {', '.join(missing_tags)}")

    supported_files = supported_corpus_files(corpus)
    if len(supported_files) < len(HF_DATASETS) * 4:
        errors.append(f"expected at least {len(HF_DATASETS) * 4} supported Hugging Face files, found {len(supported_files)}")
    return errors


def run_benchmark(args: argparse.Namespace) -> None:
    corpus = args.corpus
    if getattr(args, "require_publication_corpus", False):
        errors = validate_publication_corpus(corpus)
        if errors:
            raise SystemExit("Corpus does not meet publication benchmark bar:\n" + "\n".join(errors))
    files = supported_corpus_files(corpus)
    if not files:
        raise SystemExit(f"No supported corpus files found in {corpus}")

    profile = hook.resolve_model_profile(args.model, {"model": args.model}, Path.cwd())
    candidate_tier = getattr(args, "candidate_tier", "advanced")
    baseline_commands = resolve_baseline_command_specs(args.baseline_command)
    generated_baselines = run_baseline_commands(
        baseline_commands,
        files,
        args.baseline_out_dir or (args.json_out.parent / "generated-baselines"),
    )
    baselines = resolve_baseline_specs(args.baseline_dir) + generated_baselines
    results = [
        benchmark_file(
            path,
            profile,
            args.input_price_per_1m,
            args.monthly_calls,
            baselines,
            args.provider_input_tokens_per_second,
            candidate_tier,
        )
        for path in files
    ]
    totals = aggregate_results(
        results,
        baselines,
        args.input_price_per_1m,
        args.monthly_calls,
        args.provider_input_tokens_per_second,
    )
    report = {
        "generated_at": now_iso(),
        "corpus": str(corpus),
        "corpus_fingerprint": corpus_fingerprint(corpus, files),
        "model_profile": asdict(profile),
        "pricing": {
            "input_price_per_1m": args.input_price_per_1m,
            "monthly_calls": args.monthly_calls,
            "provider_input_tokens_per_second": args.provider_input_tokens_per_second,
        },
        "baseline_dirs": {baseline.name: str(baseline.path) for baseline in baselines},
        "baseline_provenance": baseline_provenance(baselines),
        "candidate_tier": candidate_tier,
        "totals": totals,
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
        if path.is_file() and path.name != "manifest.json" and path.suffix.lower() in hook.SUPPORTED_EXTENSIONS
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
    total_start = time.perf_counter()
    load_start = time.perf_counter()
    source = hook.load_source(path)
    load_milliseconds = elapsed_milliseconds(load_start)

    token_count_milliseconds = 0.0
    token_start = time.perf_counter()
    raw_tokens = hook.count_tokens(source.raw_text, profile)
    token_count_milliseconds += elapsed_milliseconds(token_start)

    candidate_start = time.perf_counter()
    candidates = hook.candidates_for_profile(source, profile, candidate_tier)
    candidate_generation_milliseconds = elapsed_milliseconds(candidate_start)

    rows = []
    for candidate in candidates:
        token_start = time.perf_counter()
        total_tokens, payload_tokens, instruction_tokens = hook.candidate_token_metrics(candidate, profile)
        token_count_milliseconds += elapsed_milliseconds(token_start)
        token_delta = raw_tokens - total_tokens
        rows.append(
            {
                "candidate": candidate.name,
                "total_tokens": total_tokens,
                "payload_tokens": payload_tokens,
                "instruction_tokens": instruction_tokens,
                **token_savings_fields(raw_tokens, token_delta, input_price_per_1m, monthly_calls),
            }
        )
    rows.sort(key=lambda row: (row["total_tokens"], row["candidate"]))
    best = rows[0]
    baseline_rows = benchmark_baselines(path, profile, input_price_per_1m, monthly_calls, baselines, raw_tokens)
    total_milliseconds = elapsed_milliseconds(total_start)
    best_token_delta = best["token_delta"]
    return {
        "file": str(path),
        "kind": source.kind,
        "bytes": path.stat().st_size,
        "raw_tokens": raw_tokens,
        "best": best,
        "candidates": rows,
        "external_baselines": baseline_rows,
        "latency": {
            "load_milliseconds": load_milliseconds,
            "candidate_generation_milliseconds": candidate_generation_milliseconds,
            "token_count_milliseconds": token_count_milliseconds,
            "total_milliseconds": total_milliseconds,
            "tokens_saved_per_millisecond": (
                0.0 if total_milliseconds == 0 else best_token_delta / total_milliseconds
            ),
            "break_even": build_break_even_metrics(
                best_token_delta,
                total_milliseconds,
                provider_input_tokens_per_second,
            ),
        },
    }


def benchmark_baselines(
    path: Path,
    profile: hook.ModelProfile,
    input_price_per_1m: float,
    monthly_calls: int,
    baselines: list[BaselineSpec],
    raw_tokens: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for baseline in baselines:
        baseline_path = resolve_baseline_file(baseline.path, path.name)
        if baseline_path is None:
            rows.append(
                {
                    "baseline": baseline.name,
                    "available": False,
                    "source_file": None,
                }
            )
            continue
        text = baseline_path.read_text(encoding="utf-8")
        total_tokens = hook.count_tokens(text, profile)
        token_delta = raw_tokens - total_tokens
        rows.append(
            {
                "baseline": baseline.name,
                "available": True,
                "source_file": str(baseline_path),
                "bytes": baseline_path.stat().st_size,
                "total_tokens": total_tokens,
                **token_savings_fields(raw_tokens, token_delta, input_price_per_1m, monthly_calls),
            }
        )
    rows.sort(key=lambda row: row["baseline"])
    return rows


def aggregate_results(
    results: list[dict[str, Any]],
    baselines: list[BaselineSpec],
    input_price_per_1m: float,
    monthly_calls: int,
    provider_input_tokens_per_second: float,
) -> dict[str, Any]:
    raw_tokens = sum(item["raw_tokens"] for item in results)
    optimized_tokens = sum(item["best"]["total_tokens"] for item in results)
    token_delta = raw_tokens - optimized_tokens
    by_extension = aggregate_by_key(results, lambda item: Path(item["file"]).suffix.lower().lstrip("."), input_price_per_1m, monthly_calls)
    by_source = aggregate_by_key(results, source_group_name, input_price_per_1m, monthly_calls)
    latency = aggregate_latency(results, token_delta, provider_input_tokens_per_second)
    external_baselines = aggregate_external_baselines(results, baselines, input_price_per_1m, monthly_calls)
    candidate_ablation = aggregate_candidate_ablation(results, input_price_per_1m, monthly_calls)
    return {
        "files": len(results),
        "raw_tokens": raw_tokens,
        "optimized_tokens": optimized_tokens,
        **token_savings_fields(raw_tokens, token_delta, input_price_per_1m, monthly_calls),
        "by_extension": by_extension,
        "by_source": by_source,
        "latency": latency,
        "external_baselines": external_baselines,
        "candidate_ablation": candidate_ablation,
    }


def aggregate_by_key(
    results: list[dict[str, Any]],
    key_fn: Any,
    input_price_per_1m: float,
    monthly_calls: int,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in results:
        key = str(key_fn(item))
        best_format = item["best"]["candidate"]
        bucket = grouped.setdefault(
            key,
            {
                "files": 0,
                "raw_tokens": 0,
                "optimized_tokens": 0,
                "token_delta": 0,
                "best_formats": {},
            },
        )
        bucket["files"] += 1
        bucket["raw_tokens"] += item["raw_tokens"]
        bucket["optimized_tokens"] += item["best"]["total_tokens"]
        bucket["token_delta"] += item["raw_tokens"] - item["best"]["total_tokens"]
        bucket["best_formats"][best_format] = bucket["best_formats"].get(best_format, 0) + 1
    for bucket in grouped.values():
        raw = bucket["raw_tokens"]
        delta = bucket["token_delta"]
        bucket.update(token_savings_fields(raw, delta, input_price_per_1m, monthly_calls, include_delta=False))
        bucket["best_formats"] = dict(sorted(bucket["best_formats"].items()))
    return dict(sorted(grouped.items()))


def source_group_name(item: dict[str, Any]) -> str:
    path = Path(item["file"])
    return path.name[: -len(path.suffix)] if path.suffix else path.name


def aggregate_latency(
    results: list[dict[str, Any]],
    token_delta: int,
    provider_input_tokens_per_second: float,
) -> dict[str, Any]:
    fields = (
        "load_milliseconds",
        "candidate_generation_milliseconds",
        "token_count_milliseconds",
        "total_milliseconds",
    )
    totals = {
        field: sum(float(item["latency"][field]) for item in results)
        for field in fields
    }
    total_milliseconds = totals["total_milliseconds"]
    totals["tokens_saved_per_millisecond"] = 0.0 if total_milliseconds == 0 else token_delta / total_milliseconds
    totals["break_even"] = build_break_even_metrics(
        token_delta,
        total_milliseconds,
        provider_input_tokens_per_second,
    )
    return totals


def build_break_even_metrics(
    token_delta: int,
    local_milliseconds: float,
    provider_input_tokens_per_second: float,
) -> dict[str, Any]:
    if token_delta <= 0 or local_milliseconds <= 0:
        return {
            "max_provider_input_tokens_per_second_for_break_even": 0.0,
            "projected_input_latency_saved_milliseconds": None,
            "projected_net_latency_saved_milliseconds": None,
            "projected_break_even": None,
        }

    max_provider_tps = token_delta / (local_milliseconds / 1000.0)
    if provider_input_tokens_per_second <= 0:
        return {
            "max_provider_input_tokens_per_second_for_break_even": max_provider_tps,
            "projected_input_latency_saved_milliseconds": None,
            "projected_net_latency_saved_milliseconds": None,
            "projected_break_even": None,
        }

    projected_saved_ms = token_delta * 1000.0 / provider_input_tokens_per_second
    projected_net_saved_ms = projected_saved_ms - local_milliseconds
    return {
        "max_provider_input_tokens_per_second_for_break_even": max_provider_tps,
        "projected_input_latency_saved_milliseconds": projected_saved_ms,
        "projected_net_latency_saved_milliseconds": projected_net_saved_ms,
        "projected_break_even": projected_net_saved_ms > 0,
    }


def elapsed_milliseconds(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def resolve_baseline_specs(paths: list[Path]) -> list[BaselineSpec]:
    specs: list[BaselineSpec] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise SystemExit(f"Baseline directory not found: {path}")
        name = path.name
        if name in seen:
            raise SystemExit(f"Duplicate baseline directory name: {name}")
        seen.add(name)
        specs.append(BaselineSpec(name=name, path=path))
    return specs


def resolve_baseline_command_specs(raw_specs: list[str]) -> list[BaselineCommandSpec]:
    specs: list[BaselineCommandSpec] = []
    seen: set[str] = set()
    for raw in raw_specs:
        if "=" not in raw:
            raise SystemExit(f"Baseline command must be NAME=COMMAND: {raw}")
        name, command = raw.split("=", 1)
        name = name.strip()
        command = command.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SystemExit(f"Invalid baseline command name: {name!r}")
        if not command:
            raise SystemExit(f"Baseline command is empty for {name}")
        if name in seen:
            raise SystemExit(f"Duplicate baseline command name: {name}")
        seen.add(name)
        specs.append(BaselineCommandSpec(name=name, command_template=command))
    return specs


def run_baseline_commands(
    specs: list[BaselineCommandSpec],
    files: list[Path],
    out_dir: Path,
) -> list[BaselineSpec]:
    generated: list[BaselineSpec] = []
    if not specs:
        return generated

    out_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        baseline_dir = out_dir / spec.name
        baseline_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for source in files:
            output = baseline_dir / f"{source.name}.txt"
            rendered_command = run_baseline_command(spec, source, output)
            entries.append(
                {
                    "source": str(source),
                    "source_sha256": hook.sha256_file(source),
                    "output": str(output),
                    "output_sha256": hook.sha256_file(output),
                    "command": rendered_command,
                    "bytes": output.stat().st_size,
                }
            )
        manifest_path = write_baseline_manifest(spec, baseline_dir, entries)
        generated.append(
            BaselineSpec(
                name=spec.name,
                path=baseline_dir.resolve(),
                source="command",
                command_template=spec.command_template,
                manifest_path=manifest_path.resolve(),
            )
        )
    return generated


def run_baseline_command(spec: BaselineCommandSpec, source: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    command = render_baseline_command(spec.command_template, source, output)
    result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=300)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise SystemExit(
            f"Baseline command {spec.name!r} failed for {source}: exit {result.returncode}"
            + (f": {stderr}" if stderr else "")
        )
    if not output.exists() or not output.is_file() or output.stat().st_size == 0:
        raise SystemExit(f"Baseline command {spec.name!r} did not write a non-empty output for {source}")
    return command


def write_baseline_manifest(
    spec: BaselineCommandSpec,
    baseline_dir: Path,
    entries: list[dict[str, Any]],
) -> Path:
    manifest_path = baseline_dir / "baseline-provenance.json"
    manifest = {
        "schema_version": "baseline-provenance/v1",
        "name": spec.name,
        "generated_at": now_iso(),
        "command_template": spec.command_template,
        "files": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest_path


def baseline_provenance(baselines: list[BaselineSpec]) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for baseline in baselines:
        entry: dict[str, Any] = {
            "source": baseline.source,
            "path": str(baseline.path),
        }
        if baseline.command_template is not None:
            entry["command_template"] = baseline.command_template
        if baseline.manifest_path is not None:
            entry["manifest_path"] = str(baseline.manifest_path)
        provenance[baseline.name] = entry
    return provenance


def render_baseline_command(template: str, source: Path, output: Path) -> str:
    replacements = {
        "input": shlex.quote(str(source)),
        "output": shlex.quote(str(output)),
    }
    try:
        return template.format(**replacements)
    except KeyError as exc:
        raise SystemExit(f"Unknown baseline command placeholder: {exc}") from exc


def resolve_baseline_file(baseline_dir: Path, source_name: str) -> Path | None:
    direct = baseline_dir / source_name
    text = baseline_dir / f"{source_name}.txt"
    for candidate in (direct, text):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def aggregate_external_baselines(
    results: list[dict[str, Any]],
    baselines: list[BaselineSpec],
    input_price_per_1m: float,
    monthly_calls: int,
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for baseline in baselines:
        summary[baseline.name] = {
            "files_available": 0,
            "files_missing": 0,
            "raw_tokens": 0,
            "baseline_tokens": 0,
            "token_delta": 0,
            "wins_vs_selector": 0,
            "ties_vs_selector": 0,
            "losses_vs_selector": 0,
        }
    for item in results:
        best_tokens = item["best"]["total_tokens"]
        for baseline_row in item.get("external_baselines", []):
            name = baseline_row["baseline"]
            bucket = summary[name]
            if not baseline_row.get("available"):
                bucket["files_missing"] += 1
                continue
            bucket["files_available"] += 1
            bucket["raw_tokens"] += item["raw_tokens"]
            bucket["baseline_tokens"] += baseline_row["total_tokens"]
            bucket["token_delta"] += baseline_row["token_delta"]
            if baseline_row["total_tokens"] < best_tokens:
                bucket["wins_vs_selector"] += 1
            elif baseline_row["total_tokens"] == best_tokens:
                bucket["ties_vs_selector"] += 1
            else:
                bucket["losses_vs_selector"] += 1
    for bucket in summary.values():
        raw = bucket["raw_tokens"]
        delta = bucket["token_delta"]
        bucket.update(token_savings_fields(raw, delta, input_price_per_1m, monthly_calls, include_delta=False))
    return summary


def aggregate_candidate_ablation(
    results: list[dict[str, Any]],
    input_price_per_1m: float,
    monthly_calls: int,
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for item in results:
        best_name = item["best"]["candidate"]
        candidates = item.get("candidates", [])
        for rank, candidate in enumerate(candidates, 1):
            name = candidate["candidate"]
            bucket = summary.setdefault(
                name,
                {
                    "files_available": 0,
                    "wins": 0,
                    "raw_tokens": 0,
                    "candidate_tokens": 0,
                    "token_delta": 0,
                    "rank_sum": 0,
                    "rank_best": None,
                    "rank_worst": None,
                },
            )
            bucket["files_available"] += 1
            bucket["wins"] += int(name == best_name)
            bucket["raw_tokens"] += item["raw_tokens"]
            bucket["candidate_tokens"] += candidate["total_tokens"]
            bucket["token_delta"] += candidate["token_delta"]
            bucket["rank_sum"] += rank
            bucket["rank_best"] = rank if bucket["rank_best"] is None else min(bucket["rank_best"], rank)
            bucket["rank_worst"] = rank if bucket["rank_worst"] is None else max(bucket["rank_worst"], rank)

    for bucket in summary.values():
        raw = bucket["raw_tokens"]
        delta = bucket["token_delta"]
        files = bucket["files_available"]
        bucket.update(token_savings_fields(raw, delta, input_price_per_1m, monthly_calls, include_delta=False))
        bucket["average_rank"] = 0.0 if files == 0 else bucket["rank_sum"] / files
    return dict(sorted(summary.items(), key=lambda item: (-item[1]["wins"], item[1]["average_rank"], item[0])))


def token_savings_fields(
    raw_tokens: int,
    token_delta: int,
    input_price_per_1m: float,
    monthly_calls: int,
    *,
    include_delta: bool = True,
) -> dict[str, float | int]:
    fields: dict[str, float | int] = {}
    if include_delta:
        fields["token_delta"] = token_delta
    fields.update({
        "savings_ratio": 0.0 if raw_tokens == 0 else token_delta / raw_tokens,
        "per_call_input_cost_saved": token_delta * input_price_per_1m / 1_000_000,
        "monthly_input_cost_saved": token_delta * input_price_per_1m * monthly_calls / 1_000_000,
    })
    return fields


def corpus_fingerprint(corpus: Path, files: list[Path]) -> dict[str, Any]:
    manifest = corpus / "manifest.json"
    entries = []
    for path in files:
        entries.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": hook.sha256_file(path),
            }
        )
    return {
        "manifest_sha256": hook.sha256_file(manifest) if manifest.exists() else None,
        "files": entries,
    }


def markdown_report(report: dict[str, Any]) -> str:
    pricing = report["pricing"]
    totals = report["totals"]
    break_even = totals["latency"]["break_even"]
    lines = [
        "# Context Compression Benchmark Report",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Corpus: `{report['corpus']}`",
        f"Corpus manifest SHA-256: `{report['corpus_fingerprint']['manifest_sha256'] or 'none'}`",
        f"Model: `{report['model_profile']['slug']}` via `{report['model_profile']['token_counter']}`",
        f"Input price: `${pricing['input_price_per_1m']:.4f}` per 1M tokens",
        f"Monthly calls: `{pricing['monthly_calls']}`",
        "",
        "## Totals",
        "",
        "| Files | Raw tokens | Optimized tokens | Saved tokens | Savings | Saved / call | Saved / month |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {totals['files']} | {totals['raw_tokens']} | {totals['optimized_tokens']} | "
            f"{totals['token_delta']} | {totals['savings_ratio']:.1%} | "
            f"${totals['per_call_input_cost_saved']:.6f} | ${totals['monthly_input_cost_saved']:.2f} |"
        ),
        "",
        "## Local Processing Time",
        "",
        "| Load ms | Candidate ms | Token-count ms | Total ms | Saved tokens / ms | Break-even max input tok/s |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {totals['latency']['load_milliseconds']:.1f} | "
            f"{totals['latency']['candidate_generation_milliseconds']:.1f} | "
            f"{totals['latency']['token_count_milliseconds']:.1f} | "
            f"{totals['latency']['total_milliseconds']:.1f} | "
            f"{totals['latency']['tokens_saved_per_millisecond']:.1f} | "
            f"{break_even['max_provider_input_tokens_per_second_for_break_even']:.1f} |"
        ),
        "",
        "Break-even interpretation: compression is latency-positive only when the downstream model's "
        "input throughput is at or below the break-even ceiling above.",
        "",
        "## By Format",
        "",
        "| Format | Files | Raw tokens | Optimized tokens | Saved tokens | Savings |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for extension, bucket in sorted(totals["by_extension"].items()):
        lines.append(
            f"| `{extension}` | {bucket['files']} | {bucket['raw_tokens']} | "
            f"{bucket['optimized_tokens']} | {bucket['token_delta']} | {bucket['savings_ratio']:.1%} |"
        )
    if totals["by_source"]:
        lines.extend(
            [
                "",
                "## By Source Dataset",
                "",
                "| Source | Files | Raw tokens | Optimized tokens | Saved tokens | Savings | Winning formats |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for source, bucket in sorted(totals["by_source"].items()):
            winning = ", ".join(f"{name} x{count}" for name, count in bucket["best_formats"].items())
            lines.append(
                f"| `{source}` | {bucket['files']} | {bucket['raw_tokens']} | "
                f"{bucket['optimized_tokens']} | {bucket['token_delta']} | "
                f"{bucket['savings_ratio']:.1%} | {winning} |"
            )
    if pricing["provider_input_tokens_per_second"] > 0:
        projected = break_even["projected_net_latency_saved_milliseconds"]
        lines.extend(
            [
                "",
                "## Projected Latency At Configured Throughput",
                "",
                "| Configured input tok/s | API-side ms saved | Net ms saved after local overhead | Break-even |",
                "| ---: | ---: | ---: | --- |",
                (
                    f"| {pricing['provider_input_tokens_per_second']:.1f} | "
                    f"{break_even['projected_input_latency_saved_milliseconds']:.1f} | "
                    f"{projected:.1f} | "
                    f"{'yes' if break_even['projected_break_even'] else 'no'} |"
                ),
                "",
            ]
        )
    if totals["external_baselines"]:
        lines.extend(
            [
                "",
                "## External Baselines",
                "",
                "| Baseline | Files | Missing | Tokens | Savings | Saved / month | W/T/L vs selector |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for name, bucket in sorted(totals["external_baselines"].items()):
            lines.append(
                f"| `{name}` | {bucket['files_available']} | {bucket['files_missing']} | "
                f"{bucket['baseline_tokens']} | {bucket['savings_ratio']:.1%} | "
                f"${bucket['monthly_input_cost_saved']:.2f} | "
                f"{bucket['wins_vs_selector']}/{bucket['ties_vs_selector']}/{bucket['losses_vs_selector']} |"
            )
    if totals["candidate_ablation"]:
        lines.extend(
            [
                "",
                "## Candidate Ablation",
                "",
                "| Candidate | Files | Wins | Tokens | Savings | Avg rank | Rank range |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for name, bucket in totals["candidate_ablation"].items():
            lines.append(
                f"| `{name}` | {bucket['files_available']} | {bucket['wins']} | "
                f"{bucket['candidate_tokens']} | {bucket['savings_ratio']:.1%} | "
                f"{bucket['average_rank']:.2f} | {bucket['rank_best']}-{bucket['rank_worst']} |"
            )
    lines.extend(
        [
            "",
            "## Files",
            "",
            "| File | Kind | Bytes | Best format | Raw tokens | Optimized tokens | Savings |",
            "| --- | --- | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for item in report["results"]:
        best = item["best"]
        lines.append(
            f"| `{Path(item['file']).name}` | {item['kind']} | {item['bytes']} | "
            f"{best['candidate']} | {item['raw_tokens']} | {best['total_tokens']} | "
            f"{best['savings_ratio']:.1%} |"
        )
    lines.append("")
    return "\n".join(lines)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
