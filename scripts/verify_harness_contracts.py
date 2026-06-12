#!/usr/bin/env python3
"""Verify adapter assumptions against real host source or installed code.

This is source-contract validation, not a selector smoke. Local smokes prove
our code path; this script checks that the host surfaces we document still
exist in the actual projects users run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-root", type=Path, default=detect_codex_root())
    parser.add_argument("--hermes-root", type=Path, default=Path.home() / ".hermes" / "hermes-agent")
    parser.add_argument("--openclaw-root", type=Path, default=Path("/tmp/context-compression-upstream-openclaw"))
    parser.add_argument("--claude-bin", type=Path, default=detect_executable("claude"))
    args = parser.parse_args()

    errors: list[str] = []
    checks = [
        ("codex", check_codex(args.codex_root)),
        ("claude-code", check_claude_code(args.claude_bin)),
        ("hermes-agent", check_hermes(args.hermes_root)),
        ("openclaw", check_openclaw(args.openclaw_root)),
    ]
    for harness, harness_errors in checks:
        errors.extend(f"{harness}: {error}" for error in harness_errors)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("harness source contracts ok")
    return 0


def detect_codex_root() -> Path | None:
    candidates = [
        Path.home()
        / "Documents"
        / "Codex"
        / "2026-05-20"
        / "i-have-an-idea-so-when"
        / "codex-src",
    ]
    for candidate in candidates:
        if (candidate / "codex-rs").is_dir():
            return candidate
    return None


def detect_executable(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found).resolve() if found else None


def check_codex(root: Path | None) -> list[str]:
    if root is None or not root.is_dir():
        return ["missing checkout; pass --codex-root /path/to/openai/codex"]
    return require_files(
        root,
        {
            "codex-rs/hooks/src/engine/output_parser.rs": [
                "pub(crate) fn parse_pre_tool_use",
                "permission_decision",
                "updated_input",
                "PreToolUsePermissionDecisionWire::Allow",
            ],
            "codex-rs/core/src/tools/registry.rs": [
                "run_pre_tool_use_hooks",
                "updated_input: Some(updated_input)",
                "with_updated_hook_input(invocation.clone(), updated_input)",
            ],
            "codex-rs/core/src/tools/handlers/shell/shell_command.rs": [
                'tool_input: serde_json::json!({ "command": command })',
                "fn with_updated_hook_input",
                "updated_hook_command(&updated_input)?",
            ],
        },
    )


def check_claude_code(binary: Path | None) -> list[str]:
    if binary is None or not binary.exists():
        return ["missing installed claude binary; pass --claude-bin /path/to/claude"]
    target = binary.resolve()
    try:
        output = subprocess.run(
            ["strings", str(target)],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except Exception as exc:
        return [f"could not inspect {target}: {exc}"]
    return require_text(
        output,
        [
            "com.anthropic.claude-code",
            "PreToolUse",
            "hook_event_name",
            "tool_input",
            "permissionDecision",
            "updatedInput",
            "settings.json",
        ],
    )


def check_hermes(root: Path | None) -> list[str]:
    if root is None or not root.is_dir():
        return ["missing checkout; pass --hermes-root /path/to/hermes-agent"]
    return require_files(
        root,
        {
            "hermes_cli/plugins.py": [
                "class PluginContext",
                "def register_tool(",
                "override: bool = False",
            ],
            "tools/registry.py": [
                "def get_entry(self, name: str)",
                "override=True",
                "registry.register(",
            ],
            "tools/file_tools.py": [
                "def _resolve_path_for_task",
                'registry.register(name="read_file"',
                '"limit": {"type": "integer"',
            ],
            "tools/mcp_tool.py": [
                "Configuration is read from ~/.hermes/config.yaml under the ``mcp_servers`` key",
                "from mcp.client.stdio import stdio_client",
            ],
        },
    )


def check_openclaw(root: Path | None) -> list[str]:
    if root is None or not root.is_dir():
        return ["missing checkout; pass --openclaw-root /path/to/openclaw"]
    errors = require_files(
        root,
        {
            "src/plugin-sdk/plugin-entry.ts": [
                "export type OpenClawPluginApi",
                "export function definePluginEntry",
            ],
            "src/plugins/types.ts": [
                "export type OpenClawPluginApi",
                "registerTool:",
            ],
            "src/plugin-sdk/tool-plugin.ts": [
                "api.registerTool(",
                "execute: async (toolCallId, params, signal, onUpdate)",
            ],
        },
    )
    adapter = (ROOT / "adapters" / "openclaw" / "index.ts").read_text(encoding="utf-8")
    errors.extend(
        require_text(
            adapter,
            [
                "api.registerTool(",
                'name: "context_selector"',
                "--verify-report",
            ],
        )
    )
    if '"before_tool_call"' in adapter:
        errors.append("adapter still uses unsupported transparent before_tool_call hook")
    return errors


def require_files(root: Path, checks: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    for relative, needles in checks.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"missing {relative}")
            continue
        errors.extend(f"{relative} missing {error}" for error in require_text(path.read_text(encoding="utf-8", errors="replace"), needles))
    return errors


def require_text(text: str, needles: list[str]) -> list[str]:
    return [repr(needle) for needle in needles if needle not in text]


if __name__ == "__main__":
    raise SystemExit(main())
