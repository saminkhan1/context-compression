#!/usr/bin/env python3
"""Codex hook prototype for deterministic context-format selection.

This hook is intentionally conservative:
- It only optimizes files explicitly referenced in the user prompt.
- It only emits lossless representations for JSON/CSV/TSV input.
- It chooses the lowest model-token count from a fixed candidate set.

The selector is deterministic. Better token counters can be plugged in without
changing the selection contract.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_EXTENSIONS = {".json", ".jsonl", ".csv", ".tsv"}
SAFE_CANDIDATES = {"raw", "compact-json", "column-json", "csv", "tsv"}
CANDIDATE_TIERS = {
    "safe": SAFE_CANDIDATES,
    "advanced": SAFE_CANDIDATES | {"codebook-json", "typed-csv", "typed-tsv"},
}
DEFAULT_CANDIDATE_TIER = "safe"
SCHEMA_VERSION = "context-selector/v1"
DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_INLINE_MAX_CHARS = 12_000
DEFAULT_MIN_SAVINGS_RATIO = 0.05
DEFAULT_MIN_SAVED_TOKENS = 128
DEFAULT_PROVIDER_INPUT_TOKENS_PER_SECOND = 0.0
DEFAULT_MIN_NET_LATENCY_SAVED_MS = 0.0
DEFAULT_MAX_HOOK_LATENCY_MS = 500
RAW_INTENT_RE = re.compile(
    r"\b("
    r"exact bytes|verbatim|original formatting|whitespace|line numbers?|raw text|"
    r"show (?:me )?the file|quote the file|as-is|line-by-line|delimiter|comma|tab|json syntax"
    r")\b",
    re.I,
)

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)


@dataclass(frozen=True)
class SourceData:
    path: Path
    kind: str
    value: Any
    raw_text: str


@dataclass(frozen=True)
class Candidate:
    name: str
    text: str
    reversible: bool
    instructions: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelProfile:
    slug: str
    provider: str
    tokenizer_family: str
    token_counter: str
    context_window: int | None
    auto_compact_token_limit: int | None
    source: str


@dataclass(frozen=True)
class Choice:
    source: Path
    candidate: Candidate
    raw_tokens: int
    payload_tokens: int
    instruction_tokens: int
    total_tokens: int
    savings_ratio: float
    output_path: Path | None = None


@dataclass(frozen=True)
class RewritePlan:
    choices: tuple[Choice, ...]
    updated_command: str


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        return emit_error(f"Invalid hook JSON: {exc}")

    event_name = payload.get("hook_event_name") or payload.get("hookEventName")
    if event_name == "UserPromptSubmit":
        return handle_user_prompt_submit(payload)
    if event_name == "PreToolUse":
        return handle_pre_tool_use(payload)
    return emit_noop()


def handle_user_prompt_submit(payload: dict[str, Any]) -> int:
    if os.environ.get("CONTEXT_OPTIMIZER_VISIBLE_PROMPT_INJECTION") != "1":
        return emit_noop()

    prompt = str(payload.get("prompt") or "")
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    model = str(payload.get("model") or payload.get("model_id") or "unknown")
    model_profile = resolve_model_profile(model, payload, cwd)

    max_bytes = int(os.environ.get("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES))
    inline_max_chars = int(os.environ.get("CONTEXT_OPTIMIZER_INLINE_MAX_CHARS", DEFAULT_INLINE_MAX_CHARS))
    policy = savings_policy_from_env()
    candidate_tier = candidate_tier_from_env()
    report_skips = os.environ.get("CONTEXT_OPTIMIZER_REPORT_SKIPS") == "1"

    if prompt_requests_raw_file(prompt):
        return emit_noop()

    paths = discover_paths(prompt, cwd)
    choices: list[Choice] = []
    skipped: list[str] = []

    cache_dir = cwd / ".codex" / "context-cache"

    for path in paths:
        try:
            if path.stat().st_size > max_bytes:
                skipped.append(f"{path}: skipped; over {max_bytes} bytes")
                continue
            source = load_source(path)
            choice = choose_best(source, model_profile, cache_dir, candidate_tier, persist_sidecar=False)
            if should_inject(choice, policy):
                choices.append(choice_with_sidecar(choice, source, cache_dir, model_profile, candidate_tier))
            elif report_skips:
                skipped.append(
                    f"{path}: no injection; best={choice.candidate.name}, savings={choice.savings_ratio:.1%}"
                )
        except Exception as exc:
            if report_skips:
                skipped.append(f"{path}: skipped; {exc}")

    if not choices and not skipped:
        return emit_noop()

    context = build_additional_context(choices, skipped, model_profile, inline_max_chars)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def handle_pre_tool_use(payload: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool_name != "Bash":
        return emit_noop()

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return emit_noop()

    command = str(tool_input.get("command") or "")
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    paths = plain_cat_paths(command, cwd)
    if not paths:
        return emit_noop()

    try:
        plan = build_rewrite_plan(paths, payload, cwd)
        if plan is None:
            return emit_noop()
    except Exception:
        return emit_noop()

    model = str(payload.get("model") or payload.get("model_id") or "unknown")
    model_profile = resolve_model_profile(model, payload, cwd)
    report_path = write_hook_report(plan.choices, cwd, model_profile, "codex-pre-tool-use")
    if not verify_hook_report(report_path):
        return emit_noop()
    hook_output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": plan.updated_command},
    }
    if os.environ.get("CONTEXT_OPTIMIZER_EXPLAIN_REWRITES") == "1":
        summary = "; ".join(rewrite_summary(choice) for choice in plan.choices)
        hook_output["additionalContext"] = (
            "Context optimizer rewrote a whole-file context read to optimized sidecar file(s). "
            f"{summary}"
        )
    print(
        json.dumps(
            {
                "hookSpecificOutput": hook_output
            },
            ensure_ascii=False,
        )
    )
    return 0


def emit_noop() -> int:
    print("{}")
    return 0


def emit_error(message: str) -> int:
    print(f"Context optimizer hook error: {message}", file=sys.stderr)
    return emit_noop()


def prompt_requests_raw_file(prompt: str) -> bool:
    return bool(RAW_INTENT_RE.search(prompt))


def discover_paths(prompt: str, cwd: Path) -> list[Path]:
    """Find plausible local data paths mentioned in the prompt."""
    candidates: set[Path] = set()

    quoted = re.findall(r"""['"`]([^'"`\n]+\.(?:jsonl?|csv|tsv))['"`]""", prompt, flags=re.I)
    bare = re.findall(r"""(?<![\w:/.-])((?:\./|\.\./|/|[A-Za-z0-9_.-]+/)?[A-Za-z0-9_./-]+\.(?:jsonl?|csv|tsv))(?![\w.-])""", prompt, flags=re.I)

    for raw in [*quoted, *bare]:
        raw = raw.strip()
        if not raw:
            continue
        resolved = resolve_supported_file(raw, cwd)
        if resolved is not None:
            candidates.add(resolved)

    return sorted(candidates)


def plain_cat_paths(command: str, cwd: Path) -> list[Path]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return []

    if len(parts) >= 3 and parts[0] == "cat" and parts[1] == "--":
        raw_paths = parts[2:]
    elif len(parts) >= 2 and parts[0] == "cat":
        raw_paths = parts[1:]
    else:
        return []

    paths: list[Path] = []
    for raw_path in raw_paths:
        if raw_path.startswith("-"):
            return []
        resolved = resolve_supported_file(raw_path, cwd)
        if resolved is None:
            return []
        paths.append(resolved)
    return paths


def resolve_supported_file(raw_path: str, cwd: Path) -> Path | None:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.exists() and resolved.is_file() and resolved.suffix.lower() in SUPPORTED_EXTENSIONS:
        return resolved
    return None


def build_rewrite_plan(paths: list[Path], payload: dict[str, Any], cwd: Path) -> RewritePlan | None:
    if not paths:
        return None

    started_at = time.perf_counter()
    max_bytes = int(os.environ.get("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES))
    max_hook_ms = int(os.environ.get("CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS", DEFAULT_MAX_HOOK_LATENCY_MS))
    policy = savings_policy_from_env()
    candidate_tier = candidate_tier_from_env()
    model = str(payload.get("model") or payload.get("model_id") or "unknown")
    model_profile = resolve_model_profile(model, payload, cwd)
    cache_dir = cwd / ".codex" / "context-cache"

    pending: list[tuple[SourceData, Choice]] = []
    for path in paths:
        if pending and elapsed_milliseconds(started_at) > max_hook_ms:
            return None
        if path.stat().st_size > max_bytes:
            return None
        source = load_source(path)
        choice = choose_best(source, model_profile, cache_dir, candidate_tier, persist_sidecar=False)
        if not should_inject(choice, policy):
            return None
        pending.append((source, choice))

    local_milliseconds = elapsed_milliseconds(started_at)
    choices = [choice for _source, choice in pending]
    if not should_rewrite_for_latency(choices, local_milliseconds, latency_policy_from_env()):
        return None

    persisted = [
        choice_with_sidecar(choice, source, cache_dir, model_profile, candidate_tier)
        for source, choice in pending
    ]
    updated_command = "cat -- " + " ".join(shlex.quote(str(require_output_path(choice))) for choice in persisted)
    return RewritePlan(tuple(persisted), updated_command)


def rewrite_summary(choice: Choice) -> str:
    percent = round(choice.savings_ratio * 100, 1)
    return (
        f"Source: {choice.source}. Optimized: {require_output_path(choice)}. "
        f"Selected format: {choice.candidate.name}. "
        f"{token_count_label(choice)}: {choice.total_tokens} vs raw {choice.raw_tokens} ({percent}% savings)."
    )


def write_hook_report(
    choices: tuple[Choice, ...],
    cwd: Path,
    model_profile: ModelProfile,
    adapter: str,
) -> Path:
    for choice in choices:
        require_output_path(choice)
    report_dir = cwd / ".codex" / "context-cache" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    digest_input = "\n".join(
        f"{choice.source}\0{choice.output_path}\0{choice.total_tokens}"
        for choice in choices
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    report_path = report_dir / f"{adapter}.{digest}.json"
    raw_tokens = sum(choice.raw_tokens for choice in choices)
    selected_tokens = sum(choice.total_tokens for choice in choices)
    report = {
        "schema_version": SCHEMA_VERSION,
        "adapter": adapter,
        "cwd": str(cwd),
        "out_dir": str(cwd / ".codex" / "context-cache"),
        "model_profile": asdict(model_profile),
        "policy": {
            "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            "max_bytes": int(os.environ.get("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES)),
            "max_hook_latency_milliseconds": int(
                os.environ.get("CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS", DEFAULT_MAX_HOOK_LATENCY_MS)
            ),
            "candidate_tier": candidate_tier_from_env(),
            **savings_policy_from_env(),
            **latency_policy_from_env(),
            "include_candidates": False,
        },
        "summary": {
            "files": len(choices),
            "selected_files": len(choices),
            "raw_tokens": raw_tokens,
            "selected_tokens": selected_tokens,
            "saved_tokens": raw_tokens - selected_tokens,
            "savings_ratio": 0.0 if raw_tokens == 0 else 1.0 - (selected_tokens / raw_tokens),
        },
        "results": [choice_report(choice, model_profile) for choice in choices],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report_path


def verify_hook_report(report_path: Path) -> bool:
    try:
        from verify_selector_report import validate_report

        report = json.loads(report_path.read_text(encoding="utf-8"))
        errors = validate_report(report, check_files=True)
    except Exception:
        return False
    return not errors


def choice_report(choice: Choice, model_profile: ModelProfile) -> dict[str, Any]:
    output_path = require_output_path(choice)
    return {
        "source": str(choice.source),
        "source_name": choice.source.name,
        "selected": True,
        "decision": "selected",
        "read_path": str(output_path),
        "kind": choice.source.suffix.lower().lstrip("."),
        "bytes": choice.source.stat().st_size,
        "sha256": sha256_file(choice.source),
        "raw_tokens": choice.raw_tokens,
        "selected_format": choice.candidate.name,
        "selected_tokens": choice.total_tokens,
        "payload_tokens": choice.payload_tokens,
        "instruction_tokens": choice.instruction_tokens,
        "saved_tokens": choice.raw_tokens - choice.total_tokens,
        "savings_ratio": choice.savings_ratio,
        "token_counter_label": "estimated" if model_profile.token_counter == "deterministic-fallback" else "exact",
        "output_path": str(output_path),
        "output_sha256": sha256_file(output_path),
        "notes": list(choice.candidate.notes),
    }


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def load_source(path: Path) -> SourceData:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")

    if suffix == ".json":
        return SourceData(path=path, kind="json", value=json.loads(text), raw_text=text)

    if suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return SourceData(path=path, kind="jsonl", value=rows, raw_text=text)

    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = [dict(row) for row in reader]
        return SourceData(path=path, kind=suffix[1:], value=rows, raw_text=text)

    raise ValueError(f"unsupported extension {suffix}")


def choose_best(
    source: SourceData,
    model_profile: ModelProfile,
    cache_dir: Path,
    candidate_tier: str | None = None,
    persist_sidecar: bool = True,
    raw_tokens: int | None = None,
) -> Choice:
    if raw_tokens is None:
        raw_tokens = count_tokens(source.raw_text, model_profile)
    tier = normalize_candidate_tier(candidate_tier or DEFAULT_CANDIDATE_TIER)
    candidates = generated_candidates_for_profile(source, model_profile, tier)

    if not candidates:
        raise ValueError("no safe candidates generated")

    ranked = sorted(
        ((candidate_token_metrics(candidate, model_profile), candidate) for candidate in candidates),
        key=lambda item: candidate_rank_key(item[1], item[0]),
    )
    for metrics, candidate in ranked:
        if candidate_matches_source(source, candidate):
            best = candidate_with_roundtrip_note(candidate)
            break
    else:
        raise ValueError("no reversible candidates generated")
    total_tokens, payload_tokens, instruction_tokens = metrics

    output_path = (
        write_candidate_sidecar(source, best, cache_dir, model_profile, tier)
        if persist_sidecar
        else None
    )

    savings_ratio = 0.0 if raw_tokens == 0 else 1.0 - (total_tokens / raw_tokens)
    return Choice(
        source=source.path,
        candidate=best,
        raw_tokens=raw_tokens,
        payload_tokens=payload_tokens,
        instruction_tokens=instruction_tokens,
        total_tokens=total_tokens,
        savings_ratio=savings_ratio,
        output_path=output_path,
    )


def choice_with_sidecar(
    choice: Choice,
    source: SourceData,
    cache_dir: Path,
    model_profile: ModelProfile,
    candidate_tier: str,
) -> Choice:
    output_path = write_candidate_sidecar(source, choice.candidate, cache_dir, model_profile, candidate_tier)
    return replace(choice, output_path=output_path)


def require_output_path(choice: Choice) -> Path:
    if choice.output_path is None:
        raise ValueError("selected choice is missing optimized sidecar path")
    return choice.output_path


def candidate_token_metrics(candidate: Candidate, model_profile: ModelProfile) -> tuple[int, int, int]:
    return (
        count_tokens(candidate_blob(candidate), model_profile),
        count_tokens(candidate.text, model_profile),
        count_tokens(candidate.instructions, model_profile),
    )


def candidate_rank_key(candidate: Candidate, metrics: tuple[int, int, int]) -> tuple[int, int, int, str]:
    total_tokens, payload_tokens, _instruction_tokens = metrics
    return (total_tokens, payload_tokens, len(candidate.text), candidate.name)


def savings_policy_from_env() -> dict[str, float | int]:
    return {
        "min_savings_ratio": float(os.environ.get("CONTEXT_OPTIMIZER_MIN_SAVINGS_RATIO", DEFAULT_MIN_SAVINGS_RATIO)),
        "min_saved_tokens": int(os.environ.get("CONTEXT_OPTIMIZER_MIN_SAVED_TOKENS", DEFAULT_MIN_SAVED_TOKENS)),
    }


def latency_policy_from_env() -> dict[str, float | bool]:
    provider_tps = float(
        os.environ.get(
            "CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND",
            DEFAULT_PROVIDER_INPUT_TOKENS_PER_SECOND,
        )
    )
    min_net_saved_ms = float(
        os.environ.get(
            "CONTEXT_OPTIMIZER_MIN_NET_LATENCY_SAVED_MS",
            DEFAULT_MIN_NET_LATENCY_SAVED_MS,
        )
    )
    return {
        "latency_gate_enabled": provider_tps > 0,
        "provider_input_tokens_per_second": provider_tps,
        "min_net_latency_saved_milliseconds": min_net_saved_ms,
    }


def candidate_tier_from_env() -> str:
    return normalize_candidate_tier(os.environ.get("CONTEXT_OPTIMIZER_CANDIDATE_TIER", DEFAULT_CANDIDATE_TIER))


def normalize_candidate_tier(raw_tier: str) -> str:
    tier = raw_tier.strip().lower()
    if tier not in CANDIDATE_TIERS:
        return DEFAULT_CANDIDATE_TIER
    return tier


def should_inject(choice: Choice, policy: dict[str, float | int]) -> bool:
    saved_tokens = choice.raw_tokens - choice.total_tokens
    return (
        choice.candidate.name != "raw"
        and choice.savings_ratio >= float(policy["min_savings_ratio"])
        and saved_tokens >= int(policy["min_saved_tokens"])
    )


def should_rewrite_for_latency(
    choices: list[Choice],
    local_milliseconds: float,
    policy: dict[str, float | bool],
) -> bool:
    provider_tps = float(policy["provider_input_tokens_per_second"])
    if provider_tps <= 0:
        return True
    saved_tokens = sum(choice.raw_tokens - choice.total_tokens for choice in choices)
    projected_saved_ms = saved_tokens * 1000.0 / provider_tps
    return projected_saved_ms - local_milliseconds >= float(policy["min_net_latency_saved_milliseconds"])


def elapsed_milliseconds(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def with_fallback_note(candidates: list[Candidate]) -> list[Candidate]:
    return [
        Candidate(
            candidate.name,
            candidate.text,
            candidate.reversible,
            candidate.instructions,
            (*candidate.notes, "fallback token estimate"),
        )
        for candidate in candidates
    ]


def candidates_for_profile(
    source: SourceData,
    model_profile: ModelProfile,
    candidate_tier: str | None = None,
) -> list[Candidate]:
    return verify_candidates(source, generated_candidates_for_profile(source, model_profile, candidate_tier))


def generated_candidates_for_profile(
    source: SourceData,
    model_profile: ModelProfile,
    candidate_tier: str | None = None,
) -> list[Candidate]:
    tier = normalize_candidate_tier(candidate_tier or DEFAULT_CANDIDATE_TIER)
    candidates = generate_candidates(source, CANDIDATE_TIERS[tier])
    if model_profile.token_counter != "deterministic-fallback":
        return candidates
    return with_fallback_note(
        [candidate for candidate in candidates if candidate.name in SAFE_CANDIDATES]
    )


def sidecar_digest(source: SourceData, candidate: Candidate, model_profile: ModelProfile, candidate_tier: str) -> str:
    stat = source.path.stat()
    digest_input = {
        "schema_version": SCHEMA_VERSION,
        "source": str(source.path),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_sha256": sha256_file(source.path),
        "model_slug": model_profile.slug,
        "token_counter": model_profile.token_counter,
        "tokenizer_family": model_profile.tokenizer_family,
        "candidate_tier": candidate_tier,
        "candidate_name": candidate.name,
        "candidate_text_sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def write_candidate_sidecar(
    source: SourceData,
    candidate: Candidate,
    out_dir: Path,
    model_profile: ModelProfile,
    candidate_tier: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = sidecar_digest(source, candidate, model_profile, candidate_tier)
    output_path = out_dir / f"{source.path.stem}.{digest}.{candidate.name}.txt"
    if not output_path.exists():
        output_path.write_text(candidate_blob(candidate), encoding="utf-8")
    return output_path


def generate_candidates(source: SourceData, allowed_names: set[str] | None = None) -> list[Candidate]:
    allowed = allowed_names or set().union(*CANDIDATE_TIERS.values())
    value = source.value
    candidates = [
        Candidate(
            "raw",
            source.raw_text,
            True,
            "",
            ("original bytes normalized as UTF-8 text",),
        ),
        Candidate(
            "compact-json",
            compact_json(value),
            True,
            "Minified JSON.",
            ("lossless parsed data",),
        ),
    ]

    rows = rows_from_value(value)
    if rows:
        headers = stable_headers(rows)
        if headers and rows_are_uniform(rows, headers):
            if "column-json" in allowed:
                candidates.append(
                    Candidate(
                        "column-json",
                        column_json_text(rows, headers),
                        True,
                        "JSON [columns,rows].",
                        ("lossless columnar JSON",),
                    )
                )
            if "codebook-json" in allowed:
                codebook_json = codebook_json_text(rows, headers)
                if codebook_json:
                    candidates.append(
                        Candidate(
                            "codebook-json",
                            codebook_json,
                            True,
                            "JSON [cols,dicts,rows]; dicts=[col,values]; codes=indexes.",
                            ("lossless columnar JSON with categorical dictionaries",),
                        )
                    )
            if {"typed-csv", "typed-tsv"} & allowed:
                typed_columns = infer_typed_columns(rows, headers)
            else:
                typed_columns = None
            if typed_columns:
                if "typed-csv" in allowed:
                    candidates.append(
                        Candidate(
                            "typed-csv",
                            typed_table_text(rows, headers, typed_columns, ","),
                            True,
                            "Types: i=int n=num b=bool s=str ?=nullable ~=null.",
                            ("lossless typed table",),
                        )
                    )
                if "typed-tsv" in allowed:
                    candidates.append(
                        Candidate(
                            "typed-tsv",
                            typed_table_text(rows, headers, typed_columns, "\t"),
                            True,
                            "Types: i=int n=num b=bool s=str ?=nullable ~=null.",
                            ("lossless typed table",),
                        )
                    )
            if "csv" in allowed:
                candidates.append(
                    Candidate(
                        "csv",
                        table_text(rows, headers, ","),
                        True,
                        "Cells=JSON CSV.",
                        ("lossless parsed table",),
                    )
                )
            if "tsv" in allowed:
                candidates.append(
                    Candidate(
                        "tsv",
                        table_text(rows, headers, "\t"),
                        True,
                        "Cells=JSON TSV.",
                        ("lossless parsed table",),
                    ),
                )
    return dedupe_candidates(candidates)


def validated_candidates(source: SourceData, allowed_names: set[str] | None = None) -> list[Candidate]:
    return verify_candidates(source, generate_candidates(source, allowed_names))


def verify_candidates(source: SourceData, candidates: Iterable[Candidate]) -> list[Candidate]:
    verified: list[Candidate] = []
    for candidate in candidates:
        if not candidate.reversible:
            continue
        if candidate_matches_source(source, candidate):
            verified.append(candidate_with_roundtrip_note(candidate))
    return verified


def candidate_with_roundtrip_note(candidate: Candidate) -> Candidate:
    if "roundtrip verified" in candidate.notes:
        return candidate
    return Candidate(
        candidate.name,
        candidate.text,
        candidate.reversible,
        candidate.instructions,
        (*candidate.notes, "roundtrip verified"),
    )


def candidate_matches_source(source: SourceData, candidate: Candidate) -> bool:
    try:
        return decode_candidate_value(candidate.name, candidate.text, source.kind) == source.value
    except Exception:
        return False


def candidate_blob(candidate: Candidate) -> str:
    if not candidate.instructions:
        return candidate.text
    return candidate.instructions + "\n" + candidate.text


def candidate_text_from_blob(candidate_name: str, blob: str) -> str:
    if candidate_name == "raw":
        return blob
    _instructions, separator, text = blob.partition("\n")
    if not separator:
        raise ValueError("candidate blob is missing decoder instruction line")
    return text


def rows_from_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]
    if isinstance(value, dict):
        for key in ("rows", "data", "items", "records", "results"):
            nested = value.get(key)
            if isinstance(nested, list) and all(isinstance(item, dict) for item in nested):
                return [dict(item) for item in nested]
    return []


def stable_headers(rows: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(key) for row in rows for key in row))


def rows_are_uniform(rows: list[dict[str, Any]], headers: list[str]) -> bool:
    expected = set(headers)
    return all(set(str(key) for key in row) == expected for row in rows)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def table_text(rows: list[dict[str, Any]], headers: list[str], delimiter: str) -> str:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([cell_json(row.get(header)) for header in headers])
    return output.getvalue().rstrip("\n")


def column_json_text(rows: list[dict[str, Any]], headers: list[str]) -> str:
    return json.dumps(
        [headers, [[row.get(header) for header in headers] for row in rows]],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def codebook_json_text(rows: list[dict[str, Any]], headers: list[str]) -> str | None:
    cell_rows = [[cell_json(row.get(header)) for header in headers] for row in rows]
    dictionaries = build_json_cell_dictionaries(cell_rows, headers)
    if not dictionaries:
        return None

    dicts: list[list[Any]] = []
    for index, header in enumerate(headers):
        mapping = dictionaries.get(header)
        if not mapping:
            continue
        values: list[Any] = [None] * len(mapping)
        for raw, code in mapping.items():
            values[code] = json.loads(raw)
        dicts.append([index, values])

    encoded_rows: list[list[Any]] = []
    for row, cell_row in zip(rows, cell_rows):
        encoded_row: list[Any] = []
        for index, header in enumerate(headers):
            mapping = dictionaries.get(header)
            encoded_row.append(mapping[cell_row[index]] if mapping else row.get(header))
        encoded_rows.append(encoded_row)

    return json.dumps([headers, dicts, encoded_rows], ensure_ascii=False, separators=(",", ":"))


def build_json_cell_dictionaries(rows: list[list[str]], headers: list[str]) -> dict[str, dict[str, int]]:
    dictionaries: dict[str, dict[str, int]] = {}

    for index, header in enumerate(headers):
        values = [row[index] for row in rows]
        unique = list(dict.fromkeys(values))
        if len(unique) == len(values):
            continue

        mapping = {value: code for code, value in enumerate(unique)}
        raw_len = sum(len(value) for value in values)
        encoded_len = sum(len(str(mapping[value])) for value in values)
        dict_values = [json.loads(value) for value in unique]
        dict_entry_len = len(json.dumps([index, dict_values], ensure_ascii=False, separators=(",", ":")))
        if raw_len > encoded_len + dict_entry_len + 1:
            dictionaries[header] = mapping

    return dictionaries


def typed_table_text(
    rows: list[dict[str, Any]],
    headers: list[str],
    typed_columns: dict[str, str],
    delimiter: str,
) -> str:
    output = io.StringIO()
    output.write("t:" + ",".join(typed_columns[header] for header in headers) + "\n")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([typed_cell(row.get(header), typed_columns[header]) for header in headers])
    return output.getvalue().rstrip("\n")


def infer_typed_columns(rows: list[dict[str, Any]], headers: list[str]) -> dict[str, str] | None:
    typed: dict[str, str] = {}
    for header in headers:
        values = [row.get(header) for row in rows]
        kind = infer_column_type(values)
        if kind is None:
            return None
        typed[header] = kind
    return typed


def infer_column_type(values: list[Any]) -> str | None:
    non_null = [value for value in values if value is not None]
    nullable = len(non_null) != len(values)
    suffix = "?" if nullable else ""

    if not non_null:
        return "s?"
    if all(isinstance(value, bool) for value in non_null):
        return "b" + suffix
    if all(isinstance(value, int) and not isinstance(value, bool) for value in non_null):
        return "i" + suffix
    if all(is_number(value) for value in non_null):
        return "n" + suffix
    if all(isinstance(value, str) for value in non_null):
        if nullable and any(value == "~" for value in non_null):
            return None
        return "s" + suffix
    return None


def is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def typed_cell(value: Any, kind: str) -> str:
    base = kind.removesuffix("?")
    if value is None:
        return "~"
    if base == "b":
        return "1" if value else "0"
    return str(value)


def cell_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def decode_candidate_value(name: str, text: str, source_kind: str) -> Any:
    if name == "raw":
        return decode_raw_value(text, source_kind)
    if name == "compact-json":
        return json.loads(text)
    if name == "column-json":
        return decode_column_json(text)
    if name == "codebook-json":
        return decode_codebook_json(text)
    if name == "csv":
        return decode_json_table(text, ",")
    if name == "tsv":
        return decode_json_table(text, "\t")
    if name == "typed-csv":
        return decode_typed_table(text, ",")
    if name == "typed-tsv":
        return decode_typed_table(text, "\t")
    raise ValueError(f"unsupported candidate {name}")


def decode_raw_value(text: str, source_kind: str) -> Any:
    if source_kind == "json":
        return json.loads(text)
    if source_kind == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if source_kind in {"csv", "tsv"}:
        delimiter = "\t" if source_kind == "tsv" else ","
        return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    raise ValueError(f"unsupported source kind {source_kind}")


def decode_json_table(text: str, delimiter: str) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        return []
    return [
        {header: json.loads(cell) for header, cell in zip(headers, row)}
        for row in reader
    ]


def decode_column_json(text: str) -> list[dict[str, Any]]:
    value = json.loads(text)
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("column-json must be [columns, rows]")
    headers, rows = value
    return decode_column_rows(headers, rows)


def decode_codebook_json(text: str) -> list[dict[str, Any]]:
    value = json.loads(text)
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("codebook-json must be [columns, dicts, rows]")
    headers, dicts, rows = value
    if not isinstance(dicts, list):
        raise ValueError("codebook-json dictionaries must be a list")

    dictionaries: dict[int, list[Any]] = {}
    for item in dicts:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("codebook-json dictionary must be [column, values]")
        column, values = item
        if isinstance(column, bool) or not isinstance(column, int):
            raise ValueError("codebook-json dictionary column must be an integer")
        if not isinstance(values, list):
            raise ValueError("codebook-json dictionary values must be a list")
        if column in dictionaries:
            raise ValueError("codebook-json duplicate dictionary column")
        dictionaries[column] = values

    decoded_rows = decode_column_rows(headers, rows, dictionaries)
    return decoded_rows


def decode_column_rows(
    headers: Any,
    rows: Any,
    dictionaries: dict[int, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(headers, list) or not all(isinstance(header, str) for header in headers):
        raise ValueError("column rows must have string columns")
    if not isinstance(rows, list):
        raise ValueError("column rows must be a list")
    dictionaries = dictionaries or {}
    if any(column < 0 or column >= len(headers) for column in dictionaries):
        raise ValueError("codebook-json dictionary column out of range")

    decoded: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(headers):
            raise ValueError("column row length mismatch")
        decoded_row: dict[str, Any] = {}
        for index, (header, cell) in enumerate(zip(headers, row)):
            values = dictionaries.get(index)
            if values is None:
                decoded_row[header] = cell
                continue
            if isinstance(cell, bool) or not isinstance(cell, int) or cell < 0 or cell >= len(values):
                raise ValueError("codebook-json cell code out of range")
            decoded_row[header] = values[cell]
        decoded.append(decoded_row)
    return decoded


def decode_typed_table(text: str, delimiter: str) -> list[dict[str, Any]]:
    first_line, _, table = text.partition("\n")
    if not first_line.startswith("t:") or not table:
        raise ValueError("missing typed table header")
    types = first_line[2:].split(",")
    reader = csv.reader(io.StringIO(table), delimiter=delimiter)
    headers = next(reader)
    if len(headers) != len(types):
        raise ValueError("header/type length mismatch")
    return [
        {
            header: decode_typed_cell(cell, kind)
            for header, cell, kind in zip(headers, row, types)
        }
        for row in reader
    ]


def decode_typed_cell(value: str, kind: str) -> Any:
    nullable = kind.endswith("?")
    base = kind.removesuffix("?")
    if nullable and value == "~":
        return None
    if base == "b":
        if value not in {"0", "1"}:
            raise ValueError("invalid boolean cell")
        return value == "1"
    if base == "i":
        return int(value)
    if base == "n":
        return float(value) if any(marker in value for marker in ".eE") else int(value)
    if base == "s":
        return value
    raise ValueError(f"unsupported typed cell kind {kind}")


def dedupe_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        if candidate.text in seen:
            continue
        seen.add(candidate.text)
        unique.append(candidate)
    return unique


def resolve_model_profile(model: str, payload: dict[str, Any], cwd: Path) -> ModelProfile:
    config = load_codex_config(cwd)
    provider = str(payload.get("model_provider") or config.get("model_provider") or "openai")

    context_window = as_int(payload.get("model_context_window")) or as_int(config.get("model_context_window"))
    auto_compact = as_int(payload.get("model_auto_compact_token_limit")) or as_int(
        config.get("model_auto_compact_token_limit")
    )
    source = "hook payload/config"

    catalog_entry = find_model_catalog_entry(model, config)
    if catalog_entry:
        context_window = context_window or as_int(catalog_entry.get("context_window"))
        auto_compact = auto_compact or as_int(catalog_entry.get("auto_compact_token_limit"))
        source = str(catalog_entry.get("_catalog_source") or "model catalog")

    tokenizer_family = infer_tokenizer_family(model, provider)
    token_counter = resolve_token_counter(tokenizer_family)
    return ModelProfile(
        slug=model,
        provider=provider,
        tokenizer_family=tokenizer_family,
        token_counter=token_counter,
        context_window=context_window,
        auto_compact_token_limit=auto_compact,
        source=source,
    )


def load_codex_config(cwd: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    config_paths = [codex_home / "config.toml", cwd / ".codex" / "config.toml"]

    for path in config_paths:
        if not path.exists():
            continue
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
            merged.update(loaded)
        except Exception:
            continue
    return merged


def find_model_catalog_entry(model: str, config: dict[str, Any]) -> dict[str, Any] | None:
    catalog_paths: list[Path] = []

    env_catalog = os.environ.get("CONTEXT_OPTIMIZER_MODEL_CATALOG_JSON")
    if env_catalog:
        catalog_paths.append(Path(env_catalog).expanduser())

    config_catalog = config.get("model_catalog_json")
    if isinstance(config_catalog, str):
        catalog_paths.append(Path(config_catalog).expanduser())

    # Static fallback from the open-source Codex catalog for common OpenAI models.
    catalog_paths.append(Path(__file__).with_name("model-catalog.snapshot.json"))

    for path in catalog_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        models = data.get("models") if isinstance(data, dict) else data
        if not isinstance(models, list):
            continue
        for entry in models:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug") or entry.get("id") or entry.get("model")
            if slug == model:
                found = dict(entry)
                found["_catalog_source"] = str(path)
                return found
    return None


def infer_tokenizer_family(model: str, provider: str) -> str:
    lowered = f"{provider}/{model}".lower()
    if "openai" in lowered or lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return "tiktoken"
    if "claude" in lowered or "anthropic" in lowered:
        return "anthropic-estimate"
    if "gemini" in lowered or "google" in lowered:
        return "gemini-estimate"
    if any(name in lowered for name in ("llama", "qwen", "mistral", "deepseek", "kimi")):
        return "hf-estimate"
    return "fallback"


def resolve_token_counter(tokenizer_family: str) -> str:
    if tokenizer_json_path() and module_available("tokenizers"):
        return "tokenizers-json"
    if tokenizer_family == "tiktoken" and module_available("tiktoken"):
        return "tiktoken"
    return "deterministic-fallback"


def tokenizer_json_path() -> Path | None:
    raw = os.environ.get("CONTEXT_OPTIMIZER_TOKENIZER_JSON")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.exists() and path.is_file() else None


@lru_cache(maxsize=None)
def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def count_tokens(text: str, model_profile: ModelProfile) -> int:
    """Count model tokens when the tokenizer is available.

    The tiktoken and Hugging Face tokenizer paths are exact for the tokenizer
    being used. The fallback intentionally counts dense punctuation and
    separators as token-ish units because compact encodings can otherwise be
    overrated by char count.
    """
    if model_profile.token_counter == "tokenizers-json":
        path = tokenizer_json_path()
        if path:
            try:
                return len(load_hf_tokenizer(str(path)).encode(text).ids)
            except Exception:
                pass

    if model_profile.token_counter == "tiktoken":
        try:
            encoding = load_tiktoken_encoding(
                model_profile.slug,
                preferred_tiktoken_encoding(model_profile.slug),
            )
            return len(encoding.encode(text))
        except Exception:
            pass
    return fallback_token_estimate(text)


def token_count_label(choice: Choice) -> str:
    if "fallback token estimate" in choice.candidate.notes:
        return "Estimated tokens"
    return "Tokens"


def preferred_tiktoken_encoding(model: str) -> str:
    configured = os.environ.get("CONTEXT_OPTIMIZER_TIKTOKEN_ENCODING")
    if configured:
        return configured

    lowered = model.lower()
    if lowered.startswith(("gpt-5", "gpt-4o", "o1", "o3", "o4")):
        return "o200k_base"
    return "cl100k_base"


@lru_cache(maxsize=16)
def load_tiktoken_encoding(model: str, fallback_encoding: str) -> Any:
    import tiktoken  # type: ignore

    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding(fallback_encoding)


@lru_cache(maxsize=4)
def load_hf_tokenizer(path: str) -> Any:
    from tokenizers import Tokenizer  # type: ignore

    return Tokenizer.from_file(path)


def fallback_token_estimate(text: str) -> int:
    if not text:
        return 0
    # OpenAI's o200k/cl100k families both tokenize common tabular delimiters
    # cheaply, but unknown models may not. Penalize dense punctuation enough
    # that raw/JSON can still win for short or irregular inputs.
    words = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", text)
    # Approximate subword splits for long atoms without needing a tokenizer.
    total = 0
    for word in words:
        if re.match(r"^[A-Za-z0-9_]+$", word):
            total += max(1, (len(word) + 3) // 4)
        else:
            total += 1
    return total


def build_additional_context(
    choices: list[Choice],
    skipped: list[str],
    model_profile: ModelProfile,
    inline_max_chars: int,
) -> str:
    lines = [
        "Context optimizer hook ran before this turn.",
        f"Target model: {model_profile.slug}",
        f"Model profile: provider={model_profile.provider}, tokenizer={model_profile.tokenizer_family}, token_counter={model_profile.token_counter}, context_window={model_profile.context_window or 'unknown'}, profile_source={model_profile.source}",
        "Use the optimized context files below instead of reading the original raw data when answering questions about those files.",
        "All emitted candidates are deterministic and lossless for the supported source types.",
    ]

    for choice in choices:
        percent = round(choice.savings_ratio * 100, 1)
        lines.extend(
            [
                "",
                f"Source: {choice.source}",
                f"Optimized: {require_output_path(choice)}",
                f"Selected format: {choice.candidate.name}",
                f"{token_count_label(choice)}: {choice.total_tokens} vs raw {choice.raw_tokens} ({percent}% savings)",
                f"Payload tokens: {choice.payload_tokens}; decoder-instruction tokens: {choice.instruction_tokens}",
                f"Notes: {', '.join(choice.candidate.notes)}",
            ]
        )
        inline_text = candidate_blob(choice.candidate)
        if len(inline_text) <= inline_max_chars:
            lines.extend(["Optimized context:", "```", inline_text, "```"])
        else:
            lines.append("Optimized context is stored in the file above; read that file if needed.")

    if skipped:
        lines.extend(["", "Skipped:"])
        lines.extend(f"- {item}" for item in skipped)

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
