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
import math
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

import ctab


SUPPORTED_EXTENSIONS = {".json", ".jsonl", ".csv", ".tsv"}
SAFE_CANDIDATES = {"raw", "compact-json", "column-json", "csv", "tsv"}
# Tiers follow measured answer accuracy, not just token counts (bench/FINDINGS.md):
# codebook-json scored 51.9% vs 60.7% for JSON on claude-haiku-4-5 (field lookups 84%
# vs 100%), so dictionary indirection is experimental-only.
CANDIDATE_TIERS = {
    "safe": SAFE_CANDIDATES,
    "advanced": SAFE_CANDIDATES | {"ctab", "typed-csv", "typed-tsv"},
    "experimental": SAFE_CANDIDATES | {"ctab", "typed-csv", "typed-tsv", "codebook-json"},
}
DEFAULT_CANDIDATE_TIER = "safe"
SCHEMA_VERSION = "context-selector/v1"
CACHE_DIR = Path(".codex", "context-cache")
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError as exc:
        print(f"Context optimizer hook error: Invalid hook JSON: {exc}", file=sys.stderr)
        return emit_noop()

    event_name = payload.get("hook_event_name") or payload.get("hookEventName")
    if event_name == "UserPromptSubmit":
        return handle_user_prompt_submit(payload)
    if event_name == "PreToolUse":
        return handle_pre_tool_use(payload)
    return emit_noop()


def handle_user_prompt_submit(payload: dict[str, Any]) -> int:
    prompt = str(payload.get("prompt") or "")
    if os.environ.get("CONTEXT_OPTIMIZER_VISIBLE_PROMPT_INJECTION") != "1":
        return emit_noop()
    cwd, model_profile = payload_context(payload)
    max_bytes = env_value("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES)
    inline_max_chars = env_value("CONTEXT_OPTIMIZER_INLINE_MAX_CHARS", DEFAULT_INLINE_MAX_CHARS)
    policy = savings_policy_from_env()
    candidate_tier = candidate_tier_from_env()
    report_skips = os.environ.get("CONTEXT_OPTIMIZER_REPORT_SKIPS") == "1"
    if RAW_INTENT_RE.search(prompt):
        return emit_noop()

    cache_dir = cwd / CACHE_DIR
    choices: list[Choice] = []
    skipped: list[str] = []
    for path in discover_paths(prompt, cwd):
        try:
            if path.stat().st_size > max_bytes:
                skipped.append(f"{path}: skipped; over {max_bytes} bytes")
                continue
            source = load_source(path)
            choice = choose_best(
                source, model_profile, cache_dir, candidate_tier, persist_sidecar=False
            )
            if should_inject(choice, policy):
                choices.append(
                    choice_with_sidecar(choice, source, cache_dir, model_profile, candidate_tier)
                )
            elif report_skips:
                skipped.append(
                    f"{path}: no injection; best={choice.candidate.name}, "
                    f"savings={choice.savings_ratio:.1%}"
                )
        except Exception as exc:
            if report_skips:
                skipped.append(f"{path}: skipped; {exc}")
    if not choices and not skipped:
        return emit_noop()

    lines = [
        "Context optimizer hook ran before this turn.",
        f"Target model: {model_profile.slug}",
        f"Model profile: provider={model_profile.provider}, "
        f"tokenizer={model_profile.tokenizer_family}, "
        f"token_counter={model_profile.token_counter}, "
        f"context_window={model_profile.context_window or 'unknown'}, "
        f"profile_source={model_profile.source}",
        "Use the optimized context files below instead of reading the original raw data "
        "when answering questions about those files.",
        "All emitted candidates are deterministic and lossless for the supported source types.",
    ]
    for choice in choices:
        lines += [
            "",
            f"Source: {choice.source}",
            f"Optimized: {require_output_path(choice)}",
            f"Selected format: {choice.candidate.name}",
            f"{token_count_label(choice)}: {choice.total_tokens} vs raw {choice.raw_tokens} "
            f"({round(choice.savings_ratio * 100, 1)}% savings)",
            f"Payload tokens: {choice.payload_tokens}; "
            f"decoder-instruction tokens: {choice.instruction_tokens}",
            f"Notes: {', '.join(choice.candidate.notes)}",
        ]
        inline_text = candidate_blob(choice.candidate)
        if len(inline_text) <= inline_max_chars:
            lines += ["Optimized context:", "```", inline_text, "```"]
        else:
            lines.append("Optimized context is stored in the file above; read that file if needed.")
    if skipped:
        lines += ["", "Skipped:", *(f"- {item}" for item in skipped)]

    output = {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n".join(lines),
    }
    print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))
    return 0


def handle_pre_tool_use(payload: dict[str, Any]) -> int:
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return emit_noop()

    # Fail closed: any problem below leaves the agent's original command untouched.
    started_at = time.perf_counter()
    try:
        cwd, model_profile = payload_context(payload)
        paths = plain_cat_paths(str(tool_input.get("command") or ""), cwd)
        choices = rewrite_choices(paths, model_profile, cwd, started_at) if paths else None
        if not choices:
            return emit_noop()
        report_path = write_hook_report(choices, cwd, model_profile)
    except Exception:
        return emit_noop()
    if not verify_hook_report(report_path):
        return emit_noop()

    quoted = " ".join(shlex.quote(str(require_output_path(choice))) for choice in choices)
    hook_output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": "cat -- " + quoted},
    }
    if os.environ.get("CONTEXT_OPTIMIZER_EXPLAIN_REWRITES") == "1":
        summary = "; ".join(
            f"Source: {choice.source}. Optimized: {require_output_path(choice)}. "
            f"Selected format: {choice.candidate.name}. "
            f"{token_count_label(choice)}: {choice.total_tokens} vs raw {choice.raw_tokens} "
            f"({round(choice.savings_ratio * 100, 1)}% savings)."
            for choice in choices
        )
        hook_output["additionalContext"] = (
            "Context optimizer rewrote a whole-file context read to optimized sidecar file(s). "
            f"{summary}"
        )
    print(json.dumps({"hookSpecificOutput": hook_output}, ensure_ascii=False))
    return 0


def emit_noop() -> int:
    print("{}")
    return 0


def payload_context(payload: dict[str, Any]) -> tuple[Path, ModelProfile]:
    cwd = Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()
    model = str(payload.get("model") or payload.get("model_id") or "unknown")
    return cwd, resolve_model_profile(model, payload, cwd)


def discover_paths(prompt: str, cwd: Path) -> list[Path]:
    """Find plausible local data paths mentioned in the prompt."""
    quoted = re.findall(r"""['"`]([^'"`\n]+\.(?:jsonl?|csv|tsv))['"`]""", prompt, flags=re.I)
    bare = re.findall(
        r"(?<![\w:/.-])((?:\./|\.\./|/|[A-Za-z0-9_.-]+/)?[A-Za-z0-9_./-]+\.(?:jsonl?|csv|tsv))"
        r"(?![\w.-])",
        prompt,
        flags=re.I,
    )
    found = {resolve_supported_file(raw.strip(), cwd) for raw in [*quoted, *bare]}
    found.discard(None)
    return sorted(found)


def plain_cat_paths(command: str, cwd: Path) -> list[Path]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    if parts[:1] != ["cat"]:
        return []
    raw_paths = parts[2:] if parts[1:2] == ["--"] else parts[1:]
    if any(raw_path.startswith("-") for raw_path in raw_paths):
        return []
    paths = [resolve_supported_file(raw_path, cwd) for raw_path in raw_paths]
    return [] if None in paths else paths


def resolve_supported_file(raw_path: str, cwd: Path) -> Path | None:
    resolved = (cwd / Path(raw_path).expanduser()).resolve()
    if resolved.is_file() and resolved.suffix.lower() in SUPPORTED_EXTENSIONS:
        return resolved
    return None


def rewrite_choices(
    paths: list[Path], model_profile: ModelProfile, cwd: Path, started_at: float
) -> tuple[Choice, ...] | None:
    """Choose a sidecar for every path, or None if any path or the latency gate says no."""
    max_bytes = env_value("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES)
    max_hook_ms = env_value("CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS", DEFAULT_MAX_HOOK_LATENCY_MS)
    policy = savings_policy_from_env()
    candidate_tier = candidate_tier_from_env()
    cache_dir = cwd / CACHE_DIR

    pending: list[tuple[SourceData, Choice]] = []
    for path in paths:
        if pending and elapsed_milliseconds(started_at) > max_hook_ms:
            return None
        if path.stat().st_size > max_bytes:
            return None
        source = load_source(path)
        choice = choose_best(
            source, model_profile, cache_dir, candidate_tier, persist_sidecar=False
        )
        if not should_inject(choice, policy):
            return None
        pending.append((source, choice))

    local_milliseconds = elapsed_milliseconds(started_at)
    latency = latency_policy_from_env()
    provider_tps = latency["provider_input_tokens_per_second"]
    if provider_tps > 0:
        saved_tokens = sum(choice.raw_tokens - choice.total_tokens for _, choice in pending)
        net_saved_ms = saved_tokens * 1000.0 / provider_tps - local_milliseconds
        if net_saved_ms < latency["min_net_latency_saved_milliseconds"]:
            return None
    return tuple(
        choice_with_sidecar(choice, source, cache_dir, model_profile, candidate_tier)
        for source, choice in pending
    )


def write_hook_report(
    choices: tuple[Choice, ...], cwd: Path, model_profile: ModelProfile
) -> Path:
    adapter = "codex-pre-tool-use"
    results = []
    for choice in choices:
        output_path = require_output_path(choice)
        results.append({
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
            "token_counter_label": (
                "estimated"
                if model_profile.token_counter == "deterministic-fallback"
                else "exact"
            ),
            "output_path": str(output_path),
            "output_sha256": sha256_file(output_path),
            "notes": list(choice.candidate.notes),
        })
    raw_tokens = sum(choice.raw_tokens for choice in choices)
    selected_tokens = sum(choice.total_tokens for choice in choices)
    max_hook_ms = env_value("CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS", DEFAULT_MAX_HOOK_LATENCY_MS)
    report = {
        "schema_version": SCHEMA_VERSION,
        "adapter": adapter,
        "cwd": str(cwd),
        "out_dir": str(cwd / CACHE_DIR),
        "model_profile": asdict(model_profile),
        "policy": {
            "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            "max_bytes": env_value("CONTEXT_OPTIMIZER_MAX_BYTES", DEFAULT_MAX_BYTES),
            "max_hook_latency_milliseconds": max_hook_ms,
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
            "savings_ratio": savings_ratio(raw_tokens, selected_tokens),
        },
        "results": results,
    }
    digest_input = "\n".join(
        f"{choice.source}\0{choice.output_path}\0{choice.total_tokens}" for choice in choices
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    report_dir = cwd / CACHE_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{adapter}.{digest}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report_path


def verify_hook_report(report_path: Path) -> bool:
    try:
        from verify_selector_report import validate_report

        report = json.loads(report_path.read_text(encoding="utf-8"))
        return not validate_report(report, check_files=True)
    except Exception:
        return False


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def parse_text(text: str, kind: str) -> Any:
    if kind == "json":
        return json.loads(text)
    if kind == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if kind in ("csv", "tsv"):
        delimiter = "\t" if kind == "tsv" else ","
        return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    raise ValueError(f"unsupported source kind {kind}")


def load_source(path: Path) -> SourceData:
    text = path.read_text(encoding="utf-8-sig")
    kind = path.suffix.lower().removeprefix(".")
    return SourceData(path=path, kind=kind, value=parse_text(text, kind), raw_text=text)


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
    tier = normalize_candidate_tier(candidate_tier)
    ranked = sorted(
        (
            (candidate_token_metrics(candidate, model_profile), candidate)
            for candidate in generated_candidates_for_profile(source, model_profile, tier)
        ),
        key=lambda item: candidate_rank_key(item[1], item[0]),
    )
    # Verify lazily in rank order: only decode until the first lossless candidate.
    for (total_tokens, payload_tokens, instruction_tokens), candidate in ranked:
        if candidate_matches_source(source, candidate):
            break
    else:
        raise ValueError("no reversible candidates generated")
    best = replace(candidate, notes=(*candidate.notes, "roundtrip verified"))
    output_path = None
    if persist_sidecar:
        output_path = write_candidate_sidecar(source, best, cache_dir, model_profile, tier)
    return Choice(
        source=source.path,
        candidate=best,
        raw_tokens=raw_tokens,
        payload_tokens=payload_tokens,
        instruction_tokens=instruction_tokens,
        total_tokens=total_tokens,
        savings_ratio=savings_ratio(raw_tokens, total_tokens),
        output_path=output_path,
    )


def choice_with_sidecar(
    choice: Choice,
    source: SourceData,
    cache_dir: Path,
    model_profile: ModelProfile,
    candidate_tier: str,
) -> Choice:
    output_path = write_candidate_sidecar(
        source, choice.candidate, cache_dir, model_profile, candidate_tier
    )
    return replace(choice, output_path=output_path)


def require_output_path(choice: Choice) -> Path:
    if choice.output_path is None:
        raise ValueError("selected choice is missing optimized sidecar path")
    return choice.output_path


def savings_ratio(raw_tokens: int, total_tokens: int) -> float:
    return 0.0 if raw_tokens == 0 else 1.0 - (total_tokens / raw_tokens)


def candidate_token_metrics(
    candidate: Candidate, model_profile: ModelProfile
) -> tuple[int, int, int]:
    return (
        count_tokens(candidate_blob(candidate), model_profile),
        count_tokens(candidate.text, model_profile),
        count_tokens(candidate.instructions, model_profile),
    )


def candidate_rank_key(
    candidate: Candidate, metrics: tuple[int, int, int]
) -> tuple[int, int, int, str]:
    total_tokens, payload_tokens, _ = metrics
    return (total_tokens, payload_tokens, len(candidate.text), candidate.name)


def env_value(name: str, default: int | float) -> int | float:
    """Read a numeric env setting, parsed with the type of its default."""
    return type(default)(os.environ.get(name, default))


def savings_policy_from_env() -> dict[str, float | int]:
    return {
        "min_savings_ratio": env_value(
            "CONTEXT_OPTIMIZER_MIN_SAVINGS_RATIO", DEFAULT_MIN_SAVINGS_RATIO
        ),
        "min_saved_tokens": env_value(
            "CONTEXT_OPTIMIZER_MIN_SAVED_TOKENS", DEFAULT_MIN_SAVED_TOKENS
        ),
    }


def latency_policy_from_env() -> dict[str, float | bool]:
    provider_tps = env_value(
        "CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND",
        DEFAULT_PROVIDER_INPUT_TOKENS_PER_SECOND,
    )
    return {
        "latency_gate_enabled": provider_tps > 0,
        "provider_input_tokens_per_second": provider_tps,
        "min_net_latency_saved_milliseconds": env_value(
            "CONTEXT_OPTIMIZER_MIN_NET_LATENCY_SAVED_MS", DEFAULT_MIN_NET_LATENCY_SAVED_MS
        ),
    }


def candidate_tier_from_env() -> str:
    return normalize_candidate_tier(os.environ.get("CONTEXT_OPTIMIZER_CANDIDATE_TIER"))


def normalize_candidate_tier(raw_tier: str | None) -> str:
    tier = (raw_tier or DEFAULT_CANDIDATE_TIER).strip().lower()
    return tier if tier in CANDIDATE_TIERS else DEFAULT_CANDIDATE_TIER


def should_inject(choice: Choice, policy: dict[str, float | int]) -> bool:
    return (
        choice.candidate.name != "raw"
        and choice.savings_ratio >= policy["min_savings_ratio"]
        and choice.raw_tokens - choice.total_tokens >= policy["min_saved_tokens"]
    )


def elapsed_milliseconds(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def candidates_for_profile(
    source: SourceData,
    model_profile: ModelProfile,
    candidate_tier: str | None = None,
) -> list[Candidate]:
    return verify_candidates(
        source, generated_candidates_for_profile(source, model_profile, candidate_tier)
    )


def generated_candidates_for_profile(
    source: SourceData,
    model_profile: ModelProfile,
    candidate_tier: str | None = None,
) -> list[Candidate]:
    # Fallback counts are estimates, so only the standard candidates compete.
    fallback = model_profile.token_counter == "deterministic-fallback"
    if fallback:
        allowed, extra_notes = SAFE_CANDIDATES, ("fallback token estimate",)
    else:
        allowed, extra_notes = CANDIDATE_TIERS[normalize_candidate_tier(candidate_tier)], ()
    # Label re-encoded views so agents never mistake them for the file's real format.
    # Without this, a model that reads a column-json view of a JSON array writes code that
    # indexes data[0]/data[1] and crashes on the real file (bench/FINDINGS.md, probe).
    header = (
        f"[Context view of {source.path.name}: on disk it is {describe_source(source)}. "
        "Below is the same data re-encoded losslessly to save tokens; code or edits must "
        "target the on-disk format.] "
    )
    candidates = []
    for candidate in generate_candidates(source, allowed):
        if candidate.name != "raw":
            candidate = replace(candidate, instructions=header + candidate.instructions)
        candidates.append(replace(candidate, notes=candidate.notes + extra_notes))
    return candidates


def write_candidate_sidecar(
    source: SourceData,
    candidate: Candidate,
    out_dir: Path,
    model_profile: ModelProfile,
    candidate_tier: str,
) -> Path:
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
        "candidate_instructions_sha256": hashlib.sha256(
            candidate.instructions.encode("utf-8")
        ).hexdigest(),
    }
    digest_json = json.dumps(digest_input, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(digest_json).hexdigest()[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{source.path.stem}.{digest}.{candidate.name}.txt"
    if not output_path.exists():
        output_path.write_text(candidate_blob(candidate), encoding="utf-8")
    return output_path


def generate_candidates(
    source: SourceData, allowed_names: set[str] | None = None
) -> list[Candidate]:
    allowed = allowed_names or set().union(*CANDIDATE_TIERS.values())
    value = source.value
    candidates = [
        Candidate(
            "raw", source.raw_text, True, "", ("original bytes normalized as UTF-8 text",)
        ),
        Candidate(
            "compact-json", compact_json(value), True, "Minified JSON.", ("lossless parsed data",)
        ),
    ]
    if "ctab" in allowed:
        candidates.append(
            Candidate("ctab", ctab.encode(value), True, ctab.PRIMER, ("lossless sectioned tables",))
        )

    rows = value
    if isinstance(value, dict):
        keys = ("rows", "data", "items", "records", "results")
        rows = next((value[key] for key in keys if is_record_list(value.get(key))), None)
    rows = rows if is_record_list(rows) else []
    headers = stable_headers(rows)
    if headers and rows_are_uniform(rows, headers):
        if "column-json" in allowed:
            candidates.append(Candidate(
                "column-json",
                compact_json([headers, [[row.get(header) for header in headers] for row in rows]]),
                True,
                "JSON [columns,rows].",
                ("lossless columnar JSON",),
            ))
        if "codebook-json" in allowed and (codebook := codebook_json_text(rows, headers)):
            candidates.append(Candidate(
                "codebook-json",
                codebook,
                True,
                "JSON [cols,dicts,rows]; dicts=[col,values]; codes=indexes.",
                ("lossless columnar JSON with categorical dictionaries",),
            ))
        types = None
        if {"typed-csv", "typed-tsv"} & allowed:
            types = {header: column_type([row.get(header) for row in rows]) for header in headers}
        for name, delimiter in (("typed-csv", ","), ("typed-tsv", "\t")):
            if name in allowed and types and None not in types.values():
                candidates.append(Candidate(
                    name,
                    table_text(rows, headers, delimiter, types),
                    True,
                    "Types: i=int n=num b=bool s=str ?=nullable ~=null.",
                    ("lossless typed table",),
                ))
        for name, delimiter in (("csv", ","), ("tsv", "\t")):
            if name in allowed:
                candidates.append(Candidate(
                    name,
                    table_text(rows, headers, delimiter),
                    True,
                    f"Cells=JSON {name.upper()}.",
                    ("lossless parsed table",),
                ))
    # Dedupe on text, first candidate wins.
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.text, candidate)
    return list(unique.values())


def validated_candidates(
    source: SourceData, allowed_names: set[str] | None = None
) -> list[Candidate]:
    return verify_candidates(source, generate_candidates(source, allowed_names))


def describe_source(source: SourceData) -> str:
    value = source.value
    if source.kind in {"csv", "tsv"}:
        return f"a {source.kind.upper()} file with {len(value)} rows"
    if source.kind == "jsonl":
        return f"a JSONL file with {len(value)} lines"
    if isinstance(value, list):
        return f"a JSON array of {len(value)} items"
    if isinstance(value, dict):
        keys = ", ".join(list(value)[:8]) + (", ..." if len(value) > 8 else "")
        return f"a JSON object with keys {keys}"
    return "a JSON value"


def verify_candidates(source: SourceData, candidates: Iterable[Candidate]) -> list[Candidate]:
    return [
        replace(candidate, notes=(*candidate.notes, "roundtrip verified"))
        for candidate in candidates
        if candidate.reversible and candidate_matches_source(source, candidate)
    ]


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
    _, separator, text = blob.partition("\n")
    if not separator:
        raise ValueError("candidate blob is missing decoder instruction line")
    return text


def is_record_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def stable_headers(rows: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(key) for row in rows for key in row))


def rows_are_uniform(rows: list[dict[str, Any]], headers: list[str]) -> bool:
    expected = set(headers)
    return all(set(map(str, row)) == expected for row in rows)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def table_text(
    rows: list[dict[str, Any]],
    headers: list[str],
    delimiter: str,
    types: dict[str, str] | None = None,
) -> str:
    """CSV/TSV with JSON cells, or typed cells under a "t:" type line when types are given."""
    output = io.StringIO()
    if types:
        output.write("t:" + ",".join(types[header] for header in headers) + "\n")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        if types:
            writer.writerow([typed_cell(row.get(header), types[header]) for header in headers])
        else:
            writer.writerow([compact_json(row.get(header)) for header in headers])
    return output.getvalue().rstrip("\n")


def codebook_json_text(rows: list[dict[str, Any]], headers: list[str]) -> str | None:
    cell_rows = [[compact_json(row.get(header)) for header in headers] for row in rows]
    # column index -> (cell JSON -> code, decoded dictionary values in code order)
    codebooks: dict[int, tuple[dict[str, int], list[Any]]] = {}
    for index in range(len(headers)):
        values = [cells[index] for cells in cell_rows]
        unique = list(dict.fromkeys(values))
        if len(unique) == len(values):
            continue
        mapping = {value: code for code, value in enumerate(unique)}
        dict_values = [json.loads(value) for value in unique]
        encoded_len = sum(len(str(mapping[value])) for value in values)
        dict_entry_len = len(compact_json([index, dict_values]))
        if sum(map(len, values)) > encoded_len + dict_entry_len + 1:
            codebooks[index] = (mapping, dict_values)
    if not codebooks:
        return None

    dicts = [[index, dict_values] for index, (_, dict_values) in codebooks.items()]
    encoded_rows = [
        [
            codebooks[index][0][cells[index]] if index in codebooks else row.get(header)
            for index, header in enumerate(headers)
        ]
        for row, cells in zip(rows, cell_rows)
    ]
    return compact_json([headers, dicts, encoded_rows])


def column_type(values: list[Any]) -> str | None:
    """Typed-table column code (i/n/b/s plus "?" when nullable), or None if untypeable."""
    non_null = [value for value in values if value is not None]
    nullable = len(non_null) != len(values)
    suffix = "?" if nullable else ""
    if not non_null:
        return "s?"
    if all(type(value) is bool for value in non_null):
        return "b" + suffix
    if all(type(value) is int for value in non_null):
        return "i" + suffix
    if all(
        type(value) in (int, float) and value == value and abs(value) != math.inf
        for value in non_null
    ):
        return "n" + suffix
    # "~" marks null, so nullable string columns must not contain a literal "~".
    if all(isinstance(value, str) for value in non_null) and not (nullable and "~" in non_null):
        return "s" + suffix
    return None


def typed_cell(value: Any, kind: str) -> str:
    if value is None:
        return "~"
    if kind.startswith("b"):
        return "1" if value else "0"
    return str(value)


def decode_candidate_value(name: str, text: str, source_kind: str) -> Any:
    delimiter = "\t" if name.endswith("tsv") else ","
    match name:
        case "raw":
            return parse_text(text, source_kind)
        case "compact-json":
            return json.loads(text)
        case "column-json":
            headers, rows = json.loads(text)
            return decode_column_rows(headers, rows, [])
        case "codebook-json":
            headers, dicts, rows = json.loads(text)
            return decode_column_rows(headers, rows, dicts)
        case "ctab":
            return ctab.decode(text)
        case "csv" | "tsv":
            headers, *rows = list(csv.reader(io.StringIO(text), delimiter=delimiter)) or [[]]
            return [
                {header: json.loads(cell) for header, cell in zip(headers, row)} for row in rows
            ]
        case "typed-csv" | "typed-tsv":
            type_line, _, table = text.partition("\n")
            if not type_line.startswith("t:") or not table:
                raise ValueError("missing typed table header")
            types = type_line[2:].split(",")
            headers, *rows = csv.reader(io.StringIO(table), delimiter=delimiter)
            if len(headers) != len(types):
                raise ValueError("header/type length mismatch")
            return [
                {
                    header: decode_typed_cell(cell, kind)
                    for header, cell, kind in zip(headers, row, types)
                }
                for row in rows
            ]
    raise ValueError(f"unsupported candidate {name}")


def decode_column_rows(headers: Any, rows: Any, dicts: Any) -> list[dict[str, Any]]:
    """Decode column-json rows, mapping dictionary codes back for codebook columns.

    Sidecars are read back from disk, so malformed structure must raise rather than
    decode to something that might coincidentally compare equal.
    """
    if not (
        isinstance(headers, list)
        and all(isinstance(header, str) for header in headers)
        and isinstance(rows, list)
        and isinstance(dicts, list)
    ):
        raise ValueError("column-json must be [string columns, (dicts,) row list]")
    dictionaries: dict[int, list[Any]] = {}
    for column, values in dicts:
        if (
            type(column) is not int
            or not 0 <= column < len(headers)
            or not isinstance(values, list)
            or column in dictionaries
        ):
            raise ValueError("codebook-json dictionary must be [column index, values]")
        dictionaries[column] = values

    decoded = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(headers):
            raise ValueError("column row length mismatch")
        decoded_row = dict(zip(headers, row))
        for column, values in dictionaries.items():
            code = row[column]
            if type(code) is not int or not 0 <= code < len(values):
                raise ValueError("codebook-json cell code out of range")
            decoded_row[headers[column]] = values[code]
        decoded.append(decoded_row)
    return decoded


def decode_typed_cell(value: str, kind: str) -> Any:
    if kind.endswith("?") and value == "~":
        return None
    match kind.removesuffix("?"):
        case "b" if value in {"0", "1"}:
            return value == "1"
        case "i":
            return int(value)
        case "n":
            return float(value) if any(marker in value for marker in ".eE") else int(value)
        case "s":
            return value
    raise ValueError(f"invalid typed cell {value!r} for kind {kind}")


def resolve_model_profile(model: str, payload: dict[str, Any], cwd: Path) -> ModelProfile:
    config = load_codex_config(cwd)
    provider = str(payload.get("model_provider") or config.get("model_provider") or "openai")
    entry = find_model_catalog_entry(model, config)
    limits = {}
    for key in ("context_window", "auto_compact_token_limit"):
        limits[key] = as_int(payload.get(f"model_{key}")) or as_int(config.get(f"model_{key}"))
        if entry:
            limits[key] = limits[key] or as_int(entry.get(key))
    source = "hook payload/config"
    if entry:
        source = str(entry.get("_catalog_source") or "model catalog")

    tokenizer_family = infer_tokenizer_family(model, provider)
    if tokenizer_json_path() and module_available("tokenizers"):
        token_counter = "tokenizers-json"
    elif tokenizer_family == "tiktoken" and module_available("tiktoken"):
        token_counter = "tiktoken"
    else:
        token_counter = "deterministic-fallback"
    return ModelProfile(
        slug=model,
        provider=provider,
        tokenizer_family=tokenizer_family,
        token_counter=token_counter,
        context_window=limits["context_window"],
        auto_compact_token_limit=limits["auto_compact_token_limit"],
        source=source,
    )


def load_codex_config(cwd: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    for path in (codex_home / "config.toml", cwd / ".codex" / "config.toml"):
        # A missing or broken config must not break the hook.
        try:
            with path.open("rb") as handle:
                merged.update(tomllib.load(handle))
        except (OSError, ValueError):
            continue
    return merged


def find_model_catalog_entry(model: str, config: dict[str, Any]) -> dict[str, Any] | None:
    configured = [
        os.environ.get("CONTEXT_OPTIMIZER_MODEL_CATALOG_JSON"),
        config.get("model_catalog_json"),
    ]
    catalog_paths = [Path(raw).expanduser() for raw in configured if isinstance(raw, str) and raw]
    # Static fallback from the open-source Codex catalog for common OpenAI models.
    catalog_paths.append(Path(__file__).with_name("model-catalog.snapshot.json"))

    for path in catalog_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models = data.get("models") if isinstance(data, dict) else data
        if not isinstance(models, list):
            continue
        for entry in models:
            if not isinstance(entry, dict):
                continue
            if (entry.get("slug") or entry.get("id") or entry.get("model")) == model:
                return {**entry, "_catalog_source": str(path)}
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


def tokenizer_json_path() -> Path | None:
    raw = os.environ.get("CONTEXT_OPTIMIZER_TOKENIZER_JSON")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_file() else None


@lru_cache(maxsize=None)
def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def count_tokens(text: str, model_profile: ModelProfile) -> int:
    """Count model tokens when the tokenizer is available.

    The tiktoken and Hugging Face tokenizer paths are exact for the tokenizer
    being used. If a tokenizer cannot load (e.g. tiktoken offline), counting
    falls back to an estimate that treats dense punctuation and separators as
    token-ish units, because compact encodings can otherwise be overrated by
    char count.
    """
    if model_profile.token_counter == "tokenizers-json" and (path := tokenizer_json_path()):
        try:
            return len(load_hf_tokenizer(str(path)).encode(text).ids)
        except Exception:
            pass
    if model_profile.token_counter == "tiktoken":
        slug = model_profile.slug
        encoding_name = os.environ.get("CONTEXT_OPTIMIZER_TIKTOKEN_ENCODING") or (
            "o200k_base"
            if slug.lower().startswith(("gpt-5", "gpt-4o", "o1", "o3", "o4"))
            else "cl100k_base"
        )
        try:
            return len(load_tiktoken_encoding(slug, encoding_name).encode(text))
        except Exception:
            pass
    # Each ASCII word costs ~1 token per 4 chars; every other non-space char costs 1.
    # Unknown models may not tokenize tabular delimiters cheaply, so this lets raw/JSON
    # still win for short or irregular inputs.
    words = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", text)
    return sum((len(word) + 3) // 4 for word in words)


def token_count_label(choice: Choice) -> str:
    if "fallback token estimate" in choice.candidate.notes:
        return "Estimated tokens"
    return "Tokens"


@lru_cache(maxsize=16)
def load_tiktoken_encoding(model: str, fallback_encoding: str) -> Any:
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding(fallback_encoding)


@lru_cache(maxsize=4)
def load_hf_tokenizer(path: str) -> Any:
    from tokenizers import Tokenizer

    return Tokenizer.from_file(path)


if __name__ == "__main__":
    raise SystemExit(main())
