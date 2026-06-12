import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { promisify } from "node:util";
import { Type } from "typebox";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const execFileAsync = promisify(execFile);

export default definePluginEntry({
  id: "context-selector",
  name: "Context Selector",
  description: "Verifier-gated lossless structured-context selector.",
  register(api) {
    api.registerTool(
      {
        name: "context_selector",
        description:
          "Select a lower-token lossless representation for local JSON, JSONL, CSV, or TSV files and return a verified context-selector/v1 report.",
        parameters: Type.Object({
          repo_root: Type.String({
            description: "Absolute path to the context-compression checkout.",
          }),
          cwd: Type.String({
            description: "Working directory for resolving relative file paths.",
          }),
          model: Type.String({
            description: "Model id used for tokenizer/profile resolution.",
          }),
          paths: Type.Array(Type.String(), {
            description: "Structured data files to evaluate.",
          }),
          adapter_name: Type.Optional(
            Type.String({
              description: "Host adapter label written into the selector report.",
              default: "openclaw-plugin",
            }),
          ),
          include_candidates: Type.Optional(Type.Boolean({ default: false })),
          report_out: Type.Optional(
            Type.String({
              description: "Optional path where the selector should persist the decision report.",
            }),
          ),
        }),
        async execute(_toolCallId, params) {
          const tempDir = params.report_out ? undefined : await mkdtemp(join(tmpdir(), "context-selector-openclaw-"));
          const reportOut = params.report_out
            ? isAbsolute(params.report_out)
              ? params.report_out
              : resolve(params.cwd, params.report_out)
            : join(tempDir!, "selector-report.json");
          const selectorArgs = [
            join(params.repo_root, "selector.py"),
            "--cwd",
            params.cwd,
            "--adapter",
            params.adapter_name ?? "openclaw-plugin",
            "--model",
            params.model,
            "--report-out",
            reportOut,
            "--verify-report",
          ];
          if (params.include_candidates) selectorArgs.push("--include-candidates");
          selectorArgs.push(...params.paths);

          try {
            const { stdout } = await execFileAsync("python3", selectorArgs, {
              cwd: params.repo_root,
              encoding: "utf8",
              maxBuffer: 10 * 1024 * 1024,
            });
            return {
              content: [{ type: "text", text: stdout }],
              details: {
                schema_version: "context-selector/v1",
                verified: true,
                report_out: reportOut,
              },
            };
          } finally {
            if (tempDir) await rm(tempDir, { recursive: true, force: true });
          }
        },
      },
      { optional: true },
    );
  },
});
