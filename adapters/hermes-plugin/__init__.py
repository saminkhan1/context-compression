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
        try:
            replacement = _selected_read_path(args, kwargs.get("task_id") or "default")
        except Exception:
            replacement = None
        if replacement is None:
            return original.handler(args, **kwargs)
        return original.handler({**args, "path": replacement}, **kwargs)

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
    """Return a verified sidecar path for a whole-file structured read, else None."""
    path = args.get("path")
    repo_root = os.environ.get("CONTEXT_SELECTOR_REPO_ROOT")
    if not isinstance(path, str) or not repo_root:
        return None
    if "offset" in args or "limit" in args:
        return None
    if Path(path).suffix.lower() not in SUPPORTED_SUFFIXES:
        return None
    repo = Path(repo_root).expanduser().resolve()
    if not (repo / "selector.py").is_file():
        return None

    from tools.file_tools import _resolve_path_for_task

    source_path = Path(_resolve_path_for_task(path, task_id)).resolve()
    with tempfile.TemporaryDirectory(prefix="context-selector-hermes-hook-") as tmp:
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
                str(Path(tmp) / "selector-report.json"),
                "--verify-report",
                str(source_path),
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
    (result,) = json.loads(proc.stdout)["results"]
    if not result["selected"]:
        return None
    with open(result["read_path"], encoding="utf-8", errors="replace") as handle:
        if sum(1 for _ in handle) > 500:
            return None
    return result["read_path"]
