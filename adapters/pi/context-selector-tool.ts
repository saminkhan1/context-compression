import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const execFileAsync = promisify(execFile);
const SUPPORTED_EXTENSION_RE = /\.(jsonl?|csv|tsv)$/i;

const contextSelectorTool = defineTool({
  name: "context_selector",
  label: "Context Selector",
  description:
    "Select a lower-token lossless representation for local JSON, JSONL, CSV, or TSV files.",
  parameters: Type.Object({
    repoRoot: Type.String({
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
    includeCandidates: Type.Optional(Type.Boolean()),
    reportOut: Type.Optional(
      Type.String({
        description: "Optional path where the selector should persist the decision report.",
      }),
    ),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const tempDir = params.reportOut
      ? undefined
      : await mkdtemp(join(tmpdir(), "context-selector-"));
    const reportOut = params.reportOut ?? join(tempDir!, "selector-report.json");
    const stdout = await runSelector(params.repoRoot, [
      "--cwd",
      params.cwd,
      "--adapter",
      "pi-extension",
      "--model",
      params.model,
      "--report-out",
      reportOut,
      ...(params.includeCandidates ? ["--include-candidates"] : []),
      ...params.paths,
    ]);
    return {
      content: [{ type: "text", text: stdout }],
      details: {
        schema_version: "context-selector/v1",
        verified: true,
        report_out: reportOut,
      },
    };
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(contextSelectorTool);
  pi.on("tool_call", async (event, ctx) => {
    const repoRoot =
      process.env.CONTEXT_SELECTOR_REPO_ROOT ||
      resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
    const model = ctx.model?.id ?? "unknown";

    if (event.toolName === "read") {
      const input = event.input as { path?: unknown; offset?: unknown; limit?: unknown };
      if (
        typeof input.path !== "string" ||
        input.offset !== undefined ||
        input.limit !== undefined ||
        !SUPPORTED_EXTENSION_RE.test(input.path)
      ) {
        return undefined;
      }
      const replacement = await selectedReadPaths(
        repoRoot, ctx.cwd, model, [input.path], "pi-read-hook",
      );
      if (replacement.length) {
        input.path = replacement[0];
      }
    } else if (event.toolName === "bash") {
      const input = event.input as { command?: unknown };
      const paths = typeof input.command === "string" ? plainCatPaths(input.command) : [];
      if (!paths.length || !paths.every((path) => SUPPORTED_EXTENSION_RE.test(path))) {
        return undefined;
      }
      const replacements = await selectedReadPaths(repoRoot, ctx.cwd, model, paths, "pi-bash-hook");
      if (replacements.length) {
        input.command = `cat -- ${replacements.map(shellQuote).join(" ")}`;
      }
    }
    return undefined;
  });
}

async function runSelector(repoRoot: string, args: string[]): Promise<string> {
  const { stdout } = await execFileAsync(
    "python3",
    [join(repoRoot, "selector.py"), "--verify-report", ...args],
    { cwd: repoRoot, encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  );
  return stdout;
}

// Verified sidecar paths for every input, or [] (leave the call untouched) on any failure.
async function selectedReadPaths(
  repoRoot: string,
  cwd: string,
  model: string,
  paths: string[],
  adapter: string,
): Promise<string[]> {
  const tempDir = await mkdtemp(join(tmpdir(), "context-selector-pi-hook-"));
  try {
    const stdout = await runSelector(repoRoot, [
      "--cwd",
      cwd,
      "--adapter",
      adapter,
      "--model",
      model,
      "--report-out",
      join(tempDir, "selector-report.json"),
      ...paths,
    ]);
    const results: { selected: boolean; read_path: string }[] = JSON.parse(stdout).results;
    if (results.length !== paths.length || !results.every((result) => result.selected)) {
      return [];
    }
    return results.map((result) => result.read_path);
  } catch {
    return [];
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
}

function plainCatPaths(command: string): string[] {
  const match = command.trim().match(/^cat\s+(?:--\s+)?(.+)$/);
  if (!match || /[|;&<>`$()]/.test(match[1])) {
    return [];
  }
  const paths = match[1].split(/\s+/).filter(Boolean);
  return paths.some((path) => path.startsWith("-")) ? [] : paths;
}

function shellQuote(value: string): string {
  return isAbsolute(value) && /^[A-Za-z0-9_./:-]+$/.test(value)
    ? value
    : `'${value.replace(/'/g, "'\\''")}'`;
}
