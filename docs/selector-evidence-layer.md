# Selector And Evidence Layer

This repo is a selector and evidence layer for multiple AI-agent runtimes:
Codex, Claude Code, Pi, Hermes Agent, OpenClaw, MCP, and generic shell adapters.

## Product Contract

The core product is not a new notation. It is a deterministic decision system
that decides whether a structured artifact should enter an agent context as
raw text or as a lower-token reversible representation.

The selector contract is:

- parse supported source formats with standard parsers
- generate a fixed candidate set for the source and model profile
- reject every candidate that does not round-trip to the parsed source value
- count candidate plus decoder instructions with the active tokenizer when
  available
- select deterministically under a fixed input, model profile, tokenizer, and
  candidate set
- refuse invisible substitution unless both the relative savings threshold and
  absolute saved-token floor pass
- emit an evidence report with the source fingerprint, selected format, token
  counts, savings, output sidecar hash, and no-op reason when it refuses

The product surface is:

- **Selector core**: `selector.py` emits `context-selector/v1` JSON for agents
  and harnesses.
- **Runtime adapters**: Codex, Claude Code, Pi, Hermes Agent, and OpenClaw
  wrappers stay thin and host-specific.
- **Benchmark and eval evidence**: `benchmark.py`, `reports/`, `EVIDENCE.md`,
  and `evals/` prove token, latency, fidelity, and answer-parity behavior.

## Generic CLI Contract

All non-Codex integrations should follow
[`adapters/CONTRACT.md`](../adapters/CONTRACT.md). They are thin selector and
verifier wrappers, not independent compressors.

Other agents should call:

```sh
.venv/bin/python selector.py \
  --cwd "$PWD" \
  --model gpt-5.4-mini \
  --adapter pi-extension \
  --report-out .codex/context-cache/selector-report.json \
  --include-candidates \
  path/to/data.json
```

The command writes optimized sidecars and returns JSON:

```json
{
  "schema_version": "context-selector/v1",
  "adapter": "generic-cli",
  "model_profile": {},
  "policy": {},
  "summary": {},
  "results": [
    {
      "source": "/abs/path/data.json",
      "decision": "selected",
      "selected": true,
      "read_path": "/abs/path/.codex/context-cache/data.hash.codebook-json.txt",
      "selected_format": "codebook-json",
      "raw_tokens": 71983,
      "selected_tokens": 20791,
      "output_path": "/abs/path/.codex/context-cache/data.hash.codebook-json.txt",
      "output_sha256": "..."
    }
  ]
}
```

Agents should only substitute the sidecar when `selected` is `true`. Every other
decision is a no-op. The simplest adapter rule is: read `read_path`, and only
announce or persist an optimization when `selected` is `true`.

Before substituting, adapters should ask the selector to verify the emitted
report:

```sh
python3 selector.py --verify-report --report-out .codex/context-cache/selector-report.json ...
```

That verifier mode enforces the trust boundary for hosts that do not share
Codex's hook runtime: selected rows must read the generated sidecar, no-op rows
must read the original source, summary token math must match the row data, and
file checks must prove the source hash, sidecar hash, read path, and selected
sidecar round-trip are still valid.

- `unsupported_format`
- `too_large`
- `raw_best`
- `below_threshold`
- `error`

The machine-readable schema lives at
[`docs/schemas/context-selector-v1.schema.json`](schemas/context-selector-v1.schema.json).

Use `--report-out` whenever an adapter makes a claim about token savings or
when a session should retain evidence of why a sidecar was read. Use `--adapter`
to label the host integration, for example `codex-hook`, `pi-extension`,
`hermes-tool`, or `openclaw-tool`.

## Codex Adapter

Codex has the strongest current fit because hooks can run before Bash tool use
and can substitute the file that a plain context read returns.

Current adapter behavior:

- `PreToolUse` rewrites plain whole-file `cat` reads to optimized sidecars,
  without adding optimizer narration to the model context by default.
- `UserPromptSubmit` no-ops by default because current Codex hooks cannot
  invisibly replace prompt text or app-injected file attachment content. Visible
  prompt injection is available only as an explicit debugging opt-in.
- semantic shell operations remain raw.

The Codex adapter should stay conservative. It should not rewrite commands
whose purpose depends on exact bytes, formatting, line numbers, delimiters, or
shell semantics.

## Claude Code Adapter

Claude Code uses [`adapters/claude-code/context_selector_hook.py`](../adapters/claude-code/context_selector_hook.py)
as a `PreToolUse` command hook with a `Read|Bash` matcher. It rewrites only
whole-file `Read` calls and simple Bash `cat` reads after
`selector.py --verify-report` returns selected `read_path` values. Bash rewrites
return `permissionDecision: "ask"` so Claude Code shows the modified command to
the user.

## Pi Adapter Shape

Pi exposes TypeScript extensions that can intercept input, tool calls, context,
and provider requests. Its README also documents JSON/RPC and SDK modes.

Current first integration:

- a Pi extension tool that shells out to `selector.py --verify-report` and
  persists a verified selector report
- a `tool_call` hook that rewrites only whole-file `read` calls and simple
  `bash cat` calls when the selector report says every requested file is
  `selected: true`
- no visible model-context injection by default

Do not depend on Pi internals for the first version. The stable interface should
remain the `context-selector/v1` JSON report plus verifier.

A minimal Pi tool example lives at
[`adapters/pi/context-selector-tool.ts`](../adapters/pi/context-selector-tool.ts).

## Hermes And OpenClaw Adapter Shape

Hermes and OpenClaw both expose richer assistant surfaces than Codex: gateways,
tools, skills, sessions, messaging channels, and remote execution. That makes
them better second targets after the selector contract is stable.

Best first integration:

- expose `selector.py` through a read-only tool that takes file paths, model id,
  and optional policy limits
- let the host agent decide whether to read the returned sidecar
- store selector reports as session evidence so cost and context claims are
  auditable later
- keep remote/channel inputs untrusted; never optimize arbitrary inbound text as
  if it were a local trusted file

For Hermes, use the native plugin in
[`adapters/hermes-plugin/`](../adapters/hermes-plugin/). Hermes Agent's
`pre_tool_call` hook is block-only, so the transparent adapter uses the
documented plugin tool route with `ctx.register_tool(..., override=True)` to
replace `read_file`. The wrapper runs `selector.py`, verifies the report, and
then delegates to Hermes' original `read_file` handler with the verified
sidecar path. It no-ops for explicit pagination, unsupported formats, failed
verification, and sidecars that would still exceed Hermes' default read
pagination.

For manual Hermes evidence collection, use the stdio MCP adapter:

```yaml
mcp_servers:
  context_selector:
    command: "python3"
    args:
      - "/absolute/path/to/context-compression/adapters/mcp/context_selector_server.py"
    tools:
      include: [context_selector]
      resources: false
      prompts: false
```

Hermes registers the MCP tool with its `mcp_<server>_<tool>` prefix, so this
becomes `mcp_context_selector_context_selector` in the Hermes tool namespace.

For OpenClaw, use the optional plugin in
[`adapters/openclaw/`](../adapters/openclaw/). Current OpenClaw source exposes
`definePluginEntry` and `api.registerTool`, but not a supported transparent
tool-call rewrite surface for plugins. The adapter therefore registers only an
explicit optional `context_selector` tool. Callers should use the verified
`read_path` values in the returned report when they want optimized content.

The generic shell-backed tool shape lives at
[`adapters/generic/context-selector-tool.md`](../adapters/generic/context-selector-tool.md).
The stdio MCP implementation lives at
[`adapters/mcp/context_selector_server.py`](../adapters/mcp/context_selector_server.py).

## Publication Evidence Roadmap

The publishable claim needs four evidence tracks:

- **Fidelity**: every selected candidate round-trips to the parsed source value.
- **Token economics**: exact tokenizer counts or clearly labeled estimates.
- **Latency economics**: local preprocessing compared with provider-side input
  token savings.
- **Matched baselines**: TOON, ONTO, LLMLingua, or other comparator outputs
  generated with `benchmark.py --baseline-command` or supplied with
  `--baseline-dir`.
- **Answer parity**: raw versus optimized context across lookup, aggregation,
  nested-value recovery, null handling, delimiter adversaries, and schema
  reasoning, summarized with `evals/summarize_context_quality.py` using
  explicit quality-gate thresholds.

Token savings alone are not enough for the product or the paper.

Use the local evidence gate as the minimum pre-claim check:

```sh
python3 scripts/verify_evidence.py --full-tests
```

It verifies the selector, Codex hook rewrite, eval dataset construction, and
benchmark/baseline plumbing. Full publication claims still require matched
external comparator runs and real model answer-parity evals with zero
raw-correct/optimized-wrong regressions, complete raw/optimized pairing, and a
declared optimized-accuracy floor on the accepted claim slice.
