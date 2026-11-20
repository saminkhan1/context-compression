#!/usr/bin/env python3
"""Stdio MCP server exposing the context selector as a verifier-gated tool."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2024-11-05"


TOOL_SCHEMA = {
    "type": "object",
    "required": ["repo_root", "cwd", "model", "paths"],
    "properties": {
        "repo_root": {
            "type": "string",
            "description": "Absolute path to the context-compression checkout.",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory for resolving relative file paths.",
        },
        "model": {
            "type": "string",
            "description": "Model id used for tokenizer/profile resolution.",
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Structured data files to evaluate.",
        },
        "adapter_name": {
            "type": "string",
            "default": "mcp-context-selector",
            "description": "Host adapter label written into the selector report.",
        },
        "include_candidates": {"type": "boolean", "default": False},
        "report_out": {
            "type": "string",
            "description": "Optional path where the selector should persist the decision report.",
        },
    },
}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = handle_message(json.loads(line))
        except Exception as exc:
            response = error_response(None, -32603, str(exc))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if request_id is None:
        return None
    if method == "initialize":
        return result_response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "context-selector", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "tools/list":
        tool = {
            "name": "context_selector",
            "description": (
                "Select a lower-token lossless representation for local JSON, JSONL, "
                "CSV, or TSV files and return a verified context-selector/v1 report."
            ),
            "inputSchema": TOOL_SCHEMA,
        }
        return result_response(request_id, {"tools": [tool]})
    if method != "tools/call":
        return error_response(request_id, -32601, f"unknown method {method!r}")
    name = params.get("name")
    if name != "context_selector":
        return error_response(request_id, -32602, f"unknown tool {name!r}")
    try:
        report_text = run_context_selector(params.get("arguments") or {})
    except Exception as exc:
        return error_response(request_id, -32603, str(exc))
    content = [{"type": "text", "text": report_text}]
    return result_response(request_id, {"content": content, "isError": False})


def run_context_selector(arguments: dict[str, Any]) -> str:
    for key in ("repo_root", "cwd", "model"):
        if not isinstance(arguments.get(key), str) or not arguments[key]:
            raise ValueError(f"{key} must be a non-empty string")
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of strings")
    repo_root = Path(arguments["repo_root"]).expanduser().resolve()
    cwd = arguments["cwd"]
    report_out = arguments.get("report_out")

    with tempfile.TemporaryDirectory(prefix="context-selector-mcp-") as tmp:
        if report_out is None:
            report_path = Path(tmp) / "selector-report.json"
        else:
            report_path = Path(cwd).expanduser().resolve() / Path(str(report_out)).expanduser()
        selector_args = [
            sys.executable,
            str(repo_root / "selector.py"),
            "--cwd",
            cwd,
            "--adapter",
            str(arguments.get("adapter_name") or "mcp-context-selector"),
            "--model",
            arguments["model"],
            "--report-out",
            str(report_path),
            "--verify-report",
        ]
        if arguments.get("include_candidates"):
            selector_args.append("--include-candidates")
        return subprocess.run(
            [*selector_args, *paths],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout


def result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    raise SystemExit(main())
