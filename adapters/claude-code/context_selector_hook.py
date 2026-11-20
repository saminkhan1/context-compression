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
        output = handle(json.load(sys.stdin))
    except Exception as exc:
        print(f"context selector hook failed open: {exc}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return 0


def handle(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    configured = os.environ.get("CONTEXT_SELECTOR_REPO_ROOT")
    if configured:
        repo = Path(configured).expanduser().resolve()
    else:
        repo = Path(__file__).resolve().parents[2]
    if not (repo / "selector.py").is_file():
        return None
    cwd = payload.get("cwd") or os.environ.get("PWD") or os.getcwd()
    cwd = Path(str(cwd)).expanduser().resolve()
    model = str(payload.get("model") or os.environ.get("CONTEXT_SELECTOR_MODEL") or "unknown")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")

    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        if (
            not isinstance(file_path, str)
            or tool_input.get("offset") is not None
            or tool_input.get("limit") is not None
            or not supported(file_path)
        ):
            return None
        replacement = selected_read_paths(repo, cwd, model, [file_path], "claude-code-read-hook")
        if not replacement:
            return None
        return pretool_output({**tool_input, "file_path": replacement[0]}, "allow")

    if tool_name == "Bash":
        command = tool_input.get("command")
        paths = plain_cat_paths(command) if isinstance(command, str) else []
        if not paths or not all(supported(path) for path in paths):
            return None
        replacement = selected_read_paths(repo, cwd, model, paths, "claude-code-bash-hook")
        if not replacement:
            return None
        command = "cat -- " + " ".join(shell_quote(path) for path in replacement)
        return pretool_output({**tool_input, "command": command}, "ask")

    return None


def selected_read_paths(
    repo: Path, cwd: Path, model: str, paths: list[str], adapter: str
) -> list[str]:
    """Return verified sidecar paths for every input, or [] when any is not selected."""
    report_dir = cwd / ".codex" / "context-cache" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f"{adapter}.", suffix=".json", dir=report_dir, delete=False
    ) as handle:
        report_out = handle.name
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
                report_out,
                "--verify-report",
                *paths,
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"context selector hook no-op: {exc}", file=sys.stderr)
        return []
    results = json.loads(proc.stdout)["results"]
    if len(results) != len(paths) or not all(result["selected"] for result in results):
        return []
    return [result["read_path"] for result in results]


def supported(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def plain_cat_paths(command: str) -> list[str]:
    if re.search(r"[|;&<>`$()]", command):
        return []
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    if parts[:1] != ["cat"]:
        return []
    paths = parts[1:]
    if paths[:1] == ["--"]:
        paths = paths[1:]
    if not paths or any(path.startswith("-") for path in paths):
        return []
    return paths


def shell_quote(value: str) -> str:
    if Path(value).is_absolute() and re.fullmatch(r"[A-Za-z0-9_./:-]+", value):
        return value
    return shlex.quote(value)


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
