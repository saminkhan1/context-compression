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
    const tempDir = params.reportOut ? undefined : await mkdtemp(join(tmpdir(), "context-selector-"));
    const reportOut = params.reportOut ?? join(tempDir!, "selector-report.json");
    const selectorArgs = [
      `${params.repoRoot}/selector.py`,
      "--cwd",
      params.cwd,
      "--adapter",
      "pi-extension",
      "--model",
      params.model,
      "--report-out",
      reportOut,
      "--verify-report",
    ];
    if (params.includeCandidates) selectorArgs.push("--include-candidates");
    selectorArgs.push(...params.paths);

    const { stdout } = await execFileAsync("python3", selectorArgs, {
      cwd: params.repoRoot,
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
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(contextSelectorTool);
  pi.on("tool_call", async (event, ctx) => {
    const repoRoot = resolveRepoRoot();
    if (!repoRoot) return undefined;

    if (event.toolName === "read") {
      const input = event.input as { path?: unknown; offset?: unknown; limit?: unknown };
      if (typeof input.path !== "string" || input.offset !== undefined || input.limit !== undefined) {
        return undefined;
      }
      if (!SUPPORTED_EXTENSION_RE.test(input.path)) return undefined;
      const replacement = await selectedReadPath(repoRoot, ctx.cwd, ctx.model?.id ?? "unknown", [input.path], "pi-read-hook");
      if (replacement.length !== 1) return undefined;
      input.path = replacement[0];
      return undefined;
    }

    if (event.toolName === "bash") {
      const input = event.input as { command?: unknown };
      if (typeof input.command !== "string") return undefined;
      const paths = plainCatPaths(input.command);
      if (!paths.length || !paths.every((path) => SUPPORTED_EXTENSION_RE.test(path))) return undefined;
      const replacements = await selectedReadPath(repoRoot, ctx.cwd, ctx.model?.id ?? "unknown", paths, "pi-bash-hook");
      if (replacements.length !== paths.length) return undefined;
      input.command = `cat -- ${replacements.map(shellQuote).join(" ")}`;
      return undefined;
    }

    return undefined;
  });
}

function resolveRepoRoot(): string | undefined {
  const configured = process.env.CONTEXT_SELECTOR_REPO_ROOT;
  if (configured) return configured;
  return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
}

async function selectedReadPath(
  repoRoot: string,
  cwd: string,
  model: string,
  paths: string[],
  adapter: string,
): Promise<string[]> {
  const tempDir = await mkdtemp(join(tmpdir(), "context-selector-pi-hook-"));
  const reportOut = join(tempDir, "selector-report.json");
  const selectorArgs = [
    join(repoRoot, "selector.py"),
    "--cwd",
    cwd,
    "--adapter",
    adapter,
    "--model",
    model,
    "--report-out",
    reportOut,
    "--verify-report",
    ...paths,
  ];
  try {
    const { stdout } = await execFileAsync("python3", selectorArgs, {
      cwd: repoRoot,
      encoding: "utf8",
      maxBuffer: 10 * 1024 * 1024,
    });
    const report = JSON.parse(stdout) as {
      results?: Array<{ selected?: boolean; read_path?: string }>;
    };
    const results = report.results ?? [];
    if (results.length !== paths.length || !results.every((result) => result.selected && result.read_path)) {
      return [];
    }
    return results.map((result) => result.read_path!);
  } catch {
    return [];
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
}

function plainCatPaths(command: string): string[] {
  const trimmed = command.trim();
  const match = trimmed.match(/^cat\s+(?:--\s+)?(.+)$/);
  if (!match) return [];
  const tail = match[1];
  if (/[|;&<>`$()]/.test(tail)) return [];
  const paths = tail.split(/\s+/).filter(Boolean);
  if (!paths.length || paths.some((path) => path.startsWith("-"))) return [];
  return paths;
}

function shellQuote(value: string): string {
  return isAbsolute(value) && /^[A-Za-z0-9_./:-]+$/.test(value)
    ? value
    : `'${value.replace(/'/g, "'\\''")}'`;
}
