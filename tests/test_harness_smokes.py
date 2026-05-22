from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import importlib.util
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "adapters" / "mcp" / "context_selector_server.py"


class HarnessSmokeTests(unittest.TestCase):
    def test_codex_pretooluse_smoke_rewrites_to_verified_sidecar(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(ROOT),
            "model": "gpt-5.5",
            "tool_name": "Bash",
            "tool_input": {"command": "cat sample-repetitive.json"},
        }
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            cwd=ROOT,
        )
        output = json.loads(proc.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertTrue(output["hookSpecificOutput"]["updatedInput"]["command"].startswith("cat -- "))

    def test_pi_smoke_returns_verified_report_with_selected_read_path(self) -> None:
        result = run_node_harness_smoke(
            "pi",
            """
            process.env.CONTEXT_SELECTOR_REPO_ROOT = repoRoot;
            const registration = { tool: null, handlers: new Map() };
            mod({
              registerTool(tool) { registration.tool = tool; },
              on(event, handler) { registration.handlers.set(event, handler); },
            });
            if (!registration.tool) throw new Error("pi adapter did not register a tool");
            const response = await registration.tool.execute(
              "call-1",
              {
                repoRoot,
                cwd: repoRoot,
                model: "gpt-5.5",
                paths: ["sample-repetitive.json"],
                reportOut,
              },
              undefined,
              undefined,
              undefined,
            );
            const hook = registration.handlers.get("tool_call");
            if (!hook) throw new Error("pi adapter did not register tool_call hook");
            const readEvent = {
              type: "tool_call",
              toolName: "read",
              toolCallId: "read-1",
              input: { path: "sample-repetitive.json" },
            };
            await hook(readEvent, { cwd: repoRoot, model: { id: "gpt-5.5" } });
            console.log(JSON.stringify({
              details: response.details,
              report: JSON.parse(response.content[0].text),
              rewrittenReadPath: readEvent.input.path,
            }));
            """,
        )
        report = result["report"]
        self.assertTrue(result["details"]["verified"])
        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertEqual(report["results"][0]["read_path"], report["results"][0]["output_path"])
        self.assertEqual(report["results"][0]["decision"], "selected")
        self.assertEqual(result["rewrittenReadPath"], report["results"][0]["read_path"])

    def test_openclaw_smoke_returns_verified_report_with_selected_read_path(self) -> None:
        result = run_node_harness_smoke(
            "openclaw",
            """
            process.env.CONTEXT_SELECTOR_REPO_ROOT = repoRoot;
            const registration = { tool: null, options: null, hooks: new Map() };
            mod.register({
              on(hookName, handler) {
                registration.hooks.set(hookName, handler);
              },
              resolvePath(path) {
                return path.startsWith("/") ? path : `${repoRoot}/${path}`;
              },
              registerTool(tool, options) {
                registration.tool = tool;
                registration.options = options;
              },
            });
            if (!registration.tool) throw new Error("openclaw adapter did not register a tool");
            const response = await registration.tool.execute("call-1", {
              repo_root: repoRoot,
              cwd: repoRoot,
              model: "gpt-5.5",
              paths: ["sample-repetitive.json"],
              report_out: reportOut,
            });
            const hook = registration.hooks.get("before_tool_call");
            if (!hook) throw new Error("openclaw adapter did not register before_tool_call hook");
            const hookResult = await hook(
              { toolName: "read_file", params: { path: "sample-repetitive.json" } },
              { toolName: "read_file", modelId: "gpt-5.5" },
            );
            console.log(JSON.stringify({
              details: response.details,
              report: JSON.parse(response.content[0].text),
              optional: registration.options?.optional === true,
              rewrittenReadPath: hookResult?.params?.path,
            }));
            """,
        )
        report = result["report"]
        self.assertTrue(result["details"]["verified"])
        self.assertTrue(result["optional"])
        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertEqual(report["results"][0]["read_path"], report["results"][0]["output_path"])
        self.assertEqual(report["results"][0]["decision"], "selected")
        self.assertEqual(result["rewrittenReadPath"], report["results"][0]["read_path"])

    def test_hermes_agent_mcp_smoke_returns_verified_report_with_selected_read_path(self) -> None:
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
                rpc(proc, 1, "initialize", {})
                rpc(proc, 2, "tools/list", {})
                result = rpc(
                    proc,
                    3,
                    "tools/call",
                    {
                        "name": "context_selector",
                        "arguments": {
                            "repo_root": str(ROOT),
                            "cwd": str(ROOT),
                            "model": "gpt-5.5",
                            "paths": ["sample-repetitive.json"],
                            "report_out": str(report_out),
                        },
                    },
                )
            finally:
                proc.stdin.close()
                proc.terminate()
                proc.communicate(timeout=5)

        report = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(report["schema_version"], "context-selector/v1")
        self.assertEqual(report["results"][0]["read_path"], report["results"][0]["output_path"])
        self.assertEqual(report["results"][0]["decision"], "selected")

    def test_hermes_agent_plugin_smoke_overrides_read_file_to_verified_sidecar(self) -> None:
        plugin_path = ROOT / "adapters" / "hermes-plugin" / "__init__.py"
        previous_repo_root = os.environ.get("CONTEXT_SELECTOR_REPO_ROOT")
        os.environ["CONTEXT_SELECTOR_REPO_ROOT"] = str(ROOT)
        try:
            with hermes_stub_modules():
                spec = importlib.util.spec_from_file_location("context_selector_hermes_plugin", plugin_path)
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                registry = sys.modules["tools.registry"].registry
                ctx = HermesStubContext(registry)
                module.register(ctx)
                response = registry.registered_handler({"path": "sample-repetitive.json"}, task_id="default")
        finally:
            if previous_repo_root is None:
                os.environ.pop("CONTEXT_SELECTOR_REPO_ROOT", None)
            else:
                os.environ["CONTEXT_SELECTOR_REPO_ROOT"] = previous_repo_root

        result = json.loads(response)
        self.assertIn(".codex/context-cache/sample-repetitive.", result["path"])
        self.assertTrue(result["path"].endswith(".column-json.txt"))
        sidecar_text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("JSON [columns,rows].", sidecar_text)


def run_node_harness_smoke(adapter: str, harness_logic: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        node_modules = tmp_path / "node_modules"
        if adapter == "pi":
            adapter_source = ROOT / "adapters" / "pi" / "context-selector-tool.ts"
            write_package(
                node_modules / "@earendil-works" / "pi-ai",
                """
                export const Type = {
                  String: (options = {}) => ({ type: "string", ...options }),
                  Boolean: (options = {}) => ({ type: "boolean", ...options }),
                  Array: (items, options = {}) => ({ type: "array", items, ...options }),
                  Optional: (schema) => ({ ...schema, optional: true }),
                  Object: (properties) => ({ type: "object", properties }),
                };
                """,
            )
            write_package(
                node_modules / "@earendil-works" / "pi-coding-agent",
                """
                export function defineTool(tool) { return tool; }
                """,
            )
        elif adapter == "openclaw":
            adapter_source = ROOT / "adapters" / "openclaw" / "index.ts"
            write_package(
                node_modules / "@sinclair" / "typebox",
                """
                export const Type = {
                  String: (options = {}) => ({ type: "string", ...options }),
                  Boolean: (options = {}) => ({ type: "boolean", ...options }),
                  Array: (items, options = {}) => ({ type: "array", items, ...options }),
                  Optional: (schema) => ({ ...schema, optional: true }),
                  Object: (properties) => ({ type: "object", properties }),
                };
                """,
            )
            openclaw_root = node_modules / "openclaw"
            (openclaw_root / "plugin-sdk").mkdir(parents=True, exist_ok=True)
            (openclaw_root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "openclaw",
                        "type": "module",
                        "exports": {
                            "./plugin-sdk/plugin-entry": "./plugin-sdk/plugin-entry.js",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (openclaw_root / "plugin-sdk" / "plugin-entry.js").write_text(
                "export function definePluginEntry(entry) { return entry; }\n",
                encoding="utf-8",
            )
        else:
            raise AssertionError(adapter)

        adapter_copy = tmp_path / adapter_source.name
        shutil.copyfile(adapter_source, adapter_copy)
        script_path = tmp_path / "runner.mjs"
        report_out = tmp_path / "selector-report.json"
        script_path.write_text(
            textwrap.dedent(
                f"""
                import mod from {json.dumps(adapter_copy.as_uri())};

                const repoRoot = {json.dumps(str(ROOT))};
                const reportOut = {json.dumps(str(report_out))};
                {textwrap.dedent(harness_logic)}
                """
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["node", str(script_path)],
            cwd=tmp_path,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"node smoke failed for {adapter}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return json.loads(proc.stdout)


class HermesStubEntry:
    toolset = "file"
    schema = {"name": "read_file", "parameters": {"type": "object"}}
    check_fn = None
    requires_env: list[str] = []
    description = "Read a text file."
    emoji = ""

    @staticmethod
    def handler(args: dict[str, object], **_kwargs: object) -> str:
        return json.dumps({"path": args["path"]})


class HermesStubRegistry:
    def __init__(self) -> None:
        self.original = HermesStubEntry()
        self.registered_handler = None

    def get_entry(self, name: str) -> HermesStubEntry | None:
        if name == "read_file":
            return self.original
        return None

    def register(self, **kwargs: object) -> None:
        self.registered_handler = kwargs["handler"]


class HermesStubContext:
    def __init__(self, registry: HermesStubRegistry) -> None:
        self.registry = registry

    def register_tool(self, **kwargs: object) -> None:
        self.registry.register(**kwargs)


class hermes_stub_modules:
    def __enter__(self) -> None:
        self.previous = {name: sys.modules.get(name) for name in ["tools", "tools.registry", "tools.file_tools"]}
        tools = types.ModuleType("tools")
        registry_module = types.ModuleType("tools.registry")
        file_tools_module = types.ModuleType("tools.file_tools")
        registry_module.registry = HermesStubRegistry()

        def resolve_path(path: str, _task_id: str = "default") -> Path:
            raw = Path(path).expanduser()
            return raw if raw.is_absolute() else ROOT / raw

        file_tools_module._resolve_path_for_task = resolve_path
        sys.modules["tools"] = tools
        sys.modules["tools.registry"] = registry_module
        sys.modules["tools.file_tools"] = file_tools_module
        return None

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        for name, module in self.previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def write_package(directory: Path, source: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(
        json.dumps({"name": directory.name, "type": "module", "exports": "./index.js"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / "index.js").write_text(textwrap.dedent(source), encoding="utf-8")


def rpc(proc: subprocess.Popen[str], request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


if __name__ == "__main__":
    unittest.main()
