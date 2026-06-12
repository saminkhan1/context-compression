from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SUPPORTED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv"}


def register(ctx: Any) -> None:
    from tools.registry import registry

    original = registry.get_entry("read_file")
    if original is None:
        return

    def optimized_read_file(args: dict[str, Any], **kwargs: Any) -> str:
        replacement = _selected_read_path(args, kwargs.get("task_id") or "default")
        if replacement is None:
            return original.handler(args, **kwargs)
        rewritten = dict(args)
        rewritten["path"] = replacement
        return original.handler(rewritten, **kwargs)

    ctx.register_tool(
        name="read_file",
        toolset=original.toolset,
        schema=original.schema,
        handler=optimized_read_file,
        check_fn=original.check_fn,
        requires_env=original.requires_env,
        description=original.description,
        emoji=original.emoji,
        override=True,
    )


def _selected_read_path(args: dict[str, Any], task_id: str) -> str | None:
    path = args.get("path")
    if not isinstance(path, str):
        return None
    if "offset" in args or "limit" in args:
        return None
    if Path(path).suffix.lower() not in SUPPORTED_SUFFIXES:
        return None

    repo_root = os.environ.get("CONTEXT_SELECTOR_REPO_ROOT")
    if not repo_root:
        return None
    repo = Path(repo_root).expanduser().resolve()
    if not (repo / "selector.py").is_file():
        return None

    try:
        source_path = _resolve_hermes_path(path, task_id)
        selected = _run_selector(repo, source_path)
        if selected is None:
            return None
        if _line_count(selected) > 500:
            return None
        return str(selected)
    except Exception:
        return None


def _resolve_hermes_path(path: str, task_id: str) -> Path:
    try:
        from tools.file_tools import _resolve_path_for_task

        return Path(_resolve_path_for_task(path, task_id)).resolve()
    except Exception:
        raw = Path(path).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        base = Path(os.environ.get("TERMINAL_CWD") or os.getcwd())
        return (base / raw).resolve()


def _run_selector(repo: Path, source_path: Path) -> Path | None:
    with tempfile.TemporaryDirectory(prefix="context-selector-hermes-hook-") as tmp:
        report_out = Path(tmp) / "selector-report.json"
        proc = subprocess.run(
            [
                "python3",
                str(repo / "selector.py"),
                "--cwd",
                str(source_path.parent),
                "--adapter",
                "hermes-read-file-plugin",
                "--model",
                os.environ.get("CONTEXT_SELECTOR_MODEL", "unknown"),
                "--report-out",
                str(report_out),
                "--verify-report",
                str(source_path),
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(proc.stdout)
        results = report.get("results")
        if not isinstance(results, list) or len(results) != 1:
            return None
        result = results[0]
        if not isinstance(result, dict) or not result.get("selected"):
            return None
        read_path = result.get("read_path")
        if not isinstance(read_path, str):
            return None
        return Path(read_path)


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)
