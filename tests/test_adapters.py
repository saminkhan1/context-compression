from __future__ import annotations

import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "adapters" / "mcp" / "context_selector_server.py"
ADAPTER_FILES = [
    ROOT / "adapters" / "claude-code" / "context_selector_hook.py",
    ROOT / "adapters" / "pi" / "context-selector-tool.ts",
    ROOT / "adapters" / "openclaw" / "index.ts",
    ROOT / "adapters" / "hermes-plugin" / "__init__.py",
    ROOT / "adapters" / "mcp" / "context_selector_server.py",
    ROOT / "adapters" / "generic" / "context-selector-tool.md",
]


class AdapterContractTests(unittest.TestCase):
    def test_adapter_contract_is_documented(self) -> None:
        contract = (ROOT / "adapters" / "CONTRACT.md").read_text(encoding="utf-8")

        self.assertIn("selector.py", contract)
        self.assertIn("--verify-report", contract)
        self.assertIn("read_path", contract)
        self.assertIn("Never rewrite the source file", contract)

    def test_all_adapters_follow_selector_verifier_report_contract(self) -> None:
        for path in ADAPTER_FILES:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn("selector.py", source)
                self.assertIn("--verify-report", source)
                self.assertIn("report", source.lower())
                self.assertNotIn("decode_candidate_value", source)
                self.assertNotIn("choose_best", source)
                self.assertNotIn("output_path", source)

    def test_pi_adapter_uses_current_extension_tool_shape_and_verifier(self) -> None:
        source = (ROOT / "adapters" / "pi" / "context-selector-tool.ts").read_text(encoding="utf-8")

        self.assertIn("defineTool", source)
        self.assertIn("pi.registerTool(contextSelectorTool)", source)
        self.assertIn('pi.on("tool_call"', source)
        self.assertIn("input.path = replacement[0]", source)
        self.assertIn("input.command = `cat -- ${replacements.map(shellQuote).join(\" \")}`", source)
        self.assertIn("--verify-report", source)
        self.assertNotIn("pi.registerTool(\n    {", source)

    def test_claude_code_adapter_uses_pretooluse_shape_and_verifier(self) -> None:
        source = (ROOT / "adapters" / "claude-code" / "context_selector_hook.py").read_text(encoding="utf-8")

        self.assertIn('"PreToolUse"', source)
        self.assertIn('"updatedInput"', source)
        self.assertIn('"permissionDecision"', source)
        self.assertIn('"Read"', source)
        self.assertIn('"Bash"', source)
        self.assertIn("--verify-report", source)
        self.assertNotIn("decode_candidate_value", source)
        self.assertNotIn("choose_best", source)

    def test_openclaw_adapter_uses_current_plugin_shape_and_verifier(self) -> None:
        plugin_dir = ROOT / "adapters" / "openclaw"
        source = (plugin_dir / "index.ts").read_text(encoding="utf-8")
        package = json.loads((plugin_dir / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads((plugin_dir / "openclaw.plugin.json").read_text(encoding="utf-8"))

        self.assertIn('definePluginEntry', source)
        self.assertIn('api.registerTool', source)
        self.assertNotIn('"before_tool_call"', source)
        self.assertIn('name: "context_selector"', source)
        self.assertIn('--verify-report', source)
        self.assertIn('{ optional: true }', source)
        self.assertIn('isAbsolute(params.report_out)', source)
        self.assertIn('await rm(tempDir, { recursive: true, force: true })', source)
        self.assertEqual(package["openclaw"]["extensions"], ["./index.ts"])
        self.assertEqual(manifest["extensions"], ["./index.ts"])

    def test_mcp_adapter_exposes_and_verifies_context_selector_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_out = Path(tmp) / "selector-report.json"
            proc = subprocess.Popen(
                [sys.executable, str(MCP_SERVER)],
                cwd=ROOT,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            try:
                initialize = rpc(proc, 1, "initialize", {})
                tools = rpc(proc, 2, "tools/list", {})
                result = rpc(
                    proc,
                    3,
                    "tools/call",
                    {
                        "name": "context_selector",
                        "arguments": {
                            "repo_root": str(ROOT),
                            "cwd": str(ROOT),
                            "model": "gpt-5.4-mini",
                            "paths": ["sample-repetitive.json"],
                            "report_out": str(report_out),
                        },
                    },
                )
            finally:
                proc.stdin.close()
                proc.terminate()
                _, stderr = proc.communicate(timeout=5)

            report = json.loads(result["result"]["content"][0]["text"])

        self.assertEqual(stderr, "")
        self.assertEqual(initialize["result"]["serverInfo"]["name"], "context-selector")
        self.assertEqual(tools["result"]["tools"][0]["name"], "context_selector")
        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertTrue(report["results"][0]["selected"])
        self.assertEqual(report["results"][0]["read_path"], report["results"][0]["output_path"])

    def test_hermes_plugin_overrides_read_file_with_verifier(self) -> None:
        plugin = (ROOT / "adapters" / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
        manifest = (ROOT / "adapters" / "hermes-plugin" / "plugin.yaml").read_text(encoding="utf-8")

        self.assertIn('registry.get_entry("read_file")', plugin)
        self.assertIn('name="read_file"', plugin)
        self.assertIn('override=True', plugin)
        self.assertIn('if "offset" in args or "limit" in args:', plugin)
        self.assertIn('--verify-report', plugin)
        self.assertIn('read_file', manifest)


def rpc(proc: subprocess.Popen[str], request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


if __name__ == "__main__":
    unittest.main()
