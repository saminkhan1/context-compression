#!/usr/bin/env python3
"""Claude Code PreToolUse hook for verified structured context sidecars."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SUPPORTED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv"}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        output = handle(payload)
    except Exception as exc:
        print(f"context selector hook failed open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def handle(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None

    cwd = Path(str(payload.get("cwd") or os.environ.get("PWD") or os.getcwd())).expanduser().resolve()
    model = str(payload.get("model") or os.environ.get("CONTEXT_SELECTOR_MODEL") or "unknown")
    repo = resolve_repo_root()
    if repo is None:
        return None

    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or tool_input.get("offset") is not None or tool_input.get("limit") is not None:
            return None
        if not supported(file_path):
            return None
        replacement = selected_read_paths(repo, cwd, model, [file_path], "claude-code-read-hook")
        if len(replacement) != 1:
            return None
        updated = dict(tool_input)
        updated["file_path"] = replacement[0]
        return pretool_output(updated, "allow")

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        paths = plain_cat_paths(command)
        if not paths or not all(supported(path) for path in paths):
            return None
        replacement = selected_read_paths(repo, cwd, model, paths, "claude-code-bash-hook")
        if len(replacement) != len(paths):
            return None
        updated = dict(tool_input)
        updated["command"] = "cat -- " + " ".join(shell_quote(path) for path in replacement)
        return pretool_output(updated, "ask")

    return None


def resolve_repo_root() -> Path | None:
    configured = os.environ.get("CONTEXT_SELECTOR_REPO_ROOT")
    repo = Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[2]
    return repo if (repo / "selector.py").is_file() else None


def selected_read_paths(repo: Path, cwd: Path, model: str, paths: list[str], adapter: str) -> list[str]:
    report_dir = cwd / ".codex" / "context-cache" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f"{adapter}.", suffix=".json", dir=report_dir, delete=False) as handle:
        report_out = Path(handle.name)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(repo / "selector.py"),
                "--cwd",
                str(cwd),
                "--adapter",
                adapter,
                "--model",
                model,
                "--report-out",
                str(report_out),
                "--verify-report",
                *paths,
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(proc.stdout)
        results = report.get("results")
        if not isinstance(results, list) or len(results) != len(paths):
            return []
        if not all(isinstance(result, dict) and result.get("selected") and result.get("read_path") for result in results):
            return []
        return [str(result["read_path"]) for result in results]
    except Exception as exc:
        print(f"context selector hook no-op: {exc}", file=sys.stderr)
        return []


def supported(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def plain_cat_paths(command: str) -> list[str]:
    if re.search(r"[|;&<>`$()]", command):
        return []
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    if not parts or parts[0] != "cat":
        return []
    paths = parts[1:]
    if paths[:1] == ["--"]:
        paths = paths[1:]
    if not paths or any(path.startswith("-") for path in paths):
        return []
    return paths


def shell_quote(value: str) -> str:
    return value if Path(value).is_absolute() and re.fullmatch(r"[A-Za-z0-9_./:-]+", value) else shlex.quote(value)


def pretool_output(updated_input: dict[str, Any], decision: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": "Use verified lower-token structured context sidecar.",
            "updatedInput": updated_input,
        }
    }


if __name__ == "__main__":
    raise SystemExit(main())
