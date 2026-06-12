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


TOOL_SCHEMA: dict[str, Any] = {
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
        "include_candidates": {
            "type": "boolean",
            "default": False,
        },
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
            message = json.loads(line)
            response = handle_message(message)
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
                "serverInfo": {
                    "name": "context-selector",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "tools": {},
                },
            },
        )
    if method == "tools/list":
        return result_response(
            request_id,
            {
                "tools": [
                    {
                        "name": "context_selector",
                        "description": (
                            "Select a lower-token lossless representation for local JSON, JSONL, "
                            "CSV, or TSV files and return a verified context-selector/v1 report."
                        ),
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        )
    if method == "tools/call":
        name = params.get("name")
        if name != "context_selector":
            return error_response(request_id, -32602, f"unknown tool {name!r}")
        try:
            report_text = run_context_selector(params.get("arguments") or {})
        except Exception as exc:
            return error_response(request_id, -32603, str(exc))
        return result_response(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": report_text,
                    }
                ],
                "isError": False,
            },
        )

    return error_response(request_id, -32601, f"unknown method {method!r}")


def run_context_selector(arguments: dict[str, Any]) -> str:
    repo_root = Path(required_str(arguments, "repo_root")).expanduser().resolve()
    cwd = required_str(arguments, "cwd")
    model = required_str(arguments, "model")
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("paths must be a list of strings")

    report_out = arguments.get("report_out")
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if report_out is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="context-selector-mcp-")
        report_path = Path(temp_dir.name) / "selector-report.json"
    else:
        report_path = Path(str(report_out)).expanduser()
        if not report_path.is_absolute():
            report_path = Path(cwd).expanduser().resolve() / report_path

    try:
        selector_args = [
            sys.executable,
            str(repo_root / "selector.py"),
            "--cwd",
            cwd,
            "--adapter",
            str(arguments.get("adapter_name") or "mcp-context-selector"),
            "--model",
            model,
            "--report-out",
            str(report_path),
            "--verify-report",
        ]
        if bool(arguments.get("include_candidates")):
            selector_args.append("--include-candidates")
        selector_args.extend(paths)
        selector = subprocess.run(
            selector_args,
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        return selector.stdout
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
