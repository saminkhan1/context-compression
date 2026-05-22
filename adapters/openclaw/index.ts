import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Type } from "@sinclair/typebox";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const execFileAsync = promisify(execFile);
const SUPPORTED_EXTENSION_RE = /\.(jsonl?|csv|tsv)$/i;

export default definePluginEntry({
  id: "context-selector",
  name: "Context Selector",
  description: "Verifier-gated lossless structured-context selector.",
  register(api) {
    api.on(
      "before_tool_call",
      async (event, ctx) => {
        const repoRoot = resolveRepoRoot();
        if (!repoRoot) return undefined;
        const modelId = (ctx as { modelId?: unknown }).modelId;
        const model = typeof modelId === "string" ? modelId : "unknown";

        if (event.toolName === "read_file") {
          const path = event.params.path;
          if (typeof path !== "string") return undefined;
          if (event.params.offset !== undefined || event.params.limit !== undefined) return undefined;
          if (!SUPPORTED_EXTENSION_RE.test(path)) return undefined;
          const resolvedPath = api.resolvePath(path);
          const replacement = await selectedReadPath(repoRoot, dirname(resolvedPath), model, [resolvedPath], "openclaw-read-hook");
          if (replacement.length !== 1) return undefined;
          return { params: { ...event.params, path: replacement[0] } };
        }

        if (event.toolName === "terminal") {
          const command = event.params.command;
          if (typeof command !== "string") return undefined;
          const paths = plainCatPaths(command);
          if (!paths.length || !paths.every((path) => SUPPORTED_EXTENSION_RE.test(path))) return undefined;
          const resolvedPaths = paths.map((path) => api.resolvePath(path));
          const replacements = await selectedReadPath(repoRoot, dirname(resolvedPaths[0]), model, resolvedPaths, "openclaw-terminal-hook");
          if (replacements.length !== paths.length) return undefined;
          return { params: { ...event.params, command: `cat -- ${replacements.map(shellQuote).join(" ")}` } };
        }

        return undefined;
      },
      { priority: 50 },
    );

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
        async execute(_id, params) {
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
          ];
          if (params.include_candidates) selectorArgs.push("--include-candidates");
          selectorArgs.push(...params.paths);

          const { stdout } = await execFileAsync("python3", selectorArgs, {
            cwd: params.repo_root,
            encoding: "utf8",
            maxBuffer: 10 * 1024 * 1024,
          });
          await execFileAsync("python3", [join(params.repo_root, "verify_selector_report.py"), "--check-files", reportOut], {
            cwd: params.repo_root,
            encoding: "utf8",
            maxBuffer: 1024 * 1024,
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
      },
      { optional: true },
    );
  },
});

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
  const tempDir = await mkdtemp(join(tmpdir(), "context-selector-openclaw-hook-"));
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
    ...paths,
  ];
  try {
    const { stdout } = await execFileAsync("python3", selectorArgs, {
      cwd: repoRoot,
      encoding: "utf8",
      maxBuffer: 10 * 1024 * 1024,
    });
    await execFileAsync("python3", [join(repoRoot, "verify_selector_report.py"), "--check-files", reportOut], {
      cwd: repoRoot,
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
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
