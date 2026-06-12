# Lossless Context Compression for Structured Agent Data

Reduce the token cost of structured files before they enter an AI agent's
context window. This repo provides a deterministic, lossless context-compression
selector for JSON, JSONL, CSV, and TSV data used by Codex, Claude Code, Pi,
Hermes Agent, OpenClaw, MCP tools, and generic agent runtimes.

Given a structured data file, the selector compares reversible representations
for the active model tokenizer and returns the lowest-token version that
round-trips back to the parsed source value. It is built for tokenizer-aware
compression, deterministic prompt optimization, and safe agent adapters rather
than lossy summarization.

Runtime adapters keep the source file untouched. When a whole-file read is safe
to substitute, they write a verified sidecar under `.codex/context-cache/` and
read that instead. Commands whose meaning depends on the original bytes or shell
semantics stay raw.

The current implementation targets local JSON, JSONL, CSV, and TSV files. It
does not edit the source files. It creates optimized sidecar files under
`.codex/context-cache/` and rewrites whole-file context reads to those sidecars
when an adapter is about to feed the file to the model. The user still asked
about the original file, and the model only receives the replacement content.
The same selector is also available through `selector.py`, which emits a stable
JSON decision report for other AI agents and evidence harnesses.

Supported inputs: JSON, JSONL, CSV, and TSV. The default runtime candidate tier
uses familiar lossless forms only: raw, compact JSON, columnar JSON, and
CSV/TSV with JSON cells. Advanced candidates such as codebook JSON and typed
CSV/TSV are available for offline evaluation, but they are not selected by the
hook unless explicitly enabled. Token counters include `tiktoken` and Hugging
Face tokenizer JSON files.

## Benchmark Snapshot

Latest checked-in corpus run: `gpt-5.4-mini` via `tiktoken`, 28 JSON/JSONL/CSV/TSV
files across public tabular, QA, conversation, review, code/documentation, log,
and GitHub metadata datasets.

| Raw tokens | Optimized tokens | Saved tokens | Savings |
| ---: | ---: | ---: | ---: |
| 17,496,442 | 15,162,406 | 2,334,036 | 13.3% |

Best source-family reductions in that run:

| Source family | Savings | Winning format |
| --- | ---: | --- |
| SQuAD QA rows | 72.7% | codebook-json |
| Titanic tabular rows | 50.5% | codebook-json |
| LogHub 2.0 logs | 41.5% | codebook-json |
| GitHub repository metadata | 29.1% | codebook-json |

See [`reports/benchmark-report.md`](reports/benchmark-report.md) for the full
corpus, per-file results, local processing time, and baseline comparison.

## Why Use It

- **Lower input-token cost:** verbose structured data can be rewritten to a
  smaller verified sidecar before it enters the model context.
- **Lossless by construction:** every selected candidate decodes back to the
  parsed source value.
- **Tokenizer-aware selection:** OpenAI and Hugging Face tokenizers can be used
  directly, so the selector optimizes for the model that will read the file.
- **Deterministic selection:** fixed input, model profile, tokenizer, and
  candidate set produce the same choice.
- **Safe hook boundary:** `jq`, `grep`, `sed`, pipes, paginated reads, mixed
  unsupported files, and low-savings cases stay raw.
- **Portable selector API:** Codex, Pi, Hermes Agent, OpenClaw, MCP tools, and
  generic agent adapters can consume the same `context-selector/v1` report.

## Contents

- [Runtime Contract](#runtime-contract)
- [What It Does](#what-it-does)
- [Candidate Formats](#candidate-formats)
- [Install](#install)
- [Harness Setup](#harness-setup)
- [Verify](#verify)
- [Generic Selector CLI](#generic-selector-cli)
- [Benchmark](#benchmark)
- [Model And Tokenizer Handling](#model-and-tokenizer-handling)
- [Prior Art And Research Basis](#prior-art-and-research-basis)
- [Current Boundary](#current-boundary)

## Runtime Contract

The target product flow is:

```text
user references a file -> adapter detects optimizable structured data ->
selector writes a lower-token sidecar -> adapter substitutes the sidecar ->
model receives optimized content -> user receives the normal answer
```

The optimizer should not become part of the task. By default, adapters avoid
injecting explanatory metadata into the conversation.

## What It Does

- Codex `PreToolUse`: rewrites whole-file Bash context reads like
  `cat data.json` or `cat a.json b.csv` to `cat` optimized sidecars.
- Codex `UserPromptSubmit`: no-ops by default because current Codex hooks cannot
  invisibly replace prompt text or app-injected file attachment content.
- Claude Code `PreToolUse`: rewrites whole-file `Read` calls and simple Bash
  `cat` reads through verified sidecars. Bash rewrites use `ask`, so Claude Code
  shows the changed command rather than silently auto-approving shell execution.
- Pi and Hermes Agent adapters can substitute verified sidecars for supported
  whole-file reads in their native extension or plugin surfaces. OpenClaw
  exposes the same selector as an explicit optional plugin tool.
- Semantic file operations stay raw: `jq`, `grep`, `sed`, `head`, Python
  scripts, pipes, `cat -n`, mixed unsupported files, or files that fail the
  savings gate all no-op.
- Selection is deterministic for a fixed model, tokenizer, input file, and
  candidate set.

The selector is:

```text
best = argmin token_count(decoder_instructions(candidate) + candidate, model)
       subject to round_trip(candidate) == parsed_source_value

inject only if best != raw and savings >= CONTEXT_OPTIMIZER_MIN_SAVINGS_RATIO
          and saved_tokens >= CONTEXT_OPTIMIZER_MIN_SAVED_TOKENS
```

For OpenAI models with `tiktoken` and non-OpenAI models with
`CONTEXT_OPTIMIZER_TOKENIZER_JSON`, `token_count` is exact for that tokenizer.
Only the deterministic fallback path reports `Estimated tokens`; on that path,
the selector compares only standard raw/JSON/CSV/TSV candidates.

The default absolute floor is `128` saved tokens. This keeps tiny token wins out
of the invisible runtime rewrite path, where local preprocessing overhead can
cost more than the provider-side token savings.

The default candidate tier is `safe`. To evaluate the larger candidate set
offline, run `selector.py --candidate-tier advanced ...` or set:

```sh
CONTEXT_OPTIMIZER_CANDIDATE_TIER=advanced
```

Keep `advanced` out of invisible runtime hooks until paired quality evals prove
answer parity for the target model family.

Latency economics are opt-in for the runtime hook. If you know the downstream
model's approximate input-processing speed, set:

```sh
CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND=1500
```

With that value set, `PreToolUse` rewrites only when projected provider-side
input latency saved by fewer tokens is greater than local preprocessing time.
Use `CONTEXT_OPTIMIZER_MIN_NET_LATENCY_SAVED_MS` to require an additional net
latency margin. If no provider throughput is configured, adapters keep the
default token-savings policy only.

`PreToolUse` also has a hard local processing ceiling:

```sh
CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS=500
```

If the hook is already over that ceiling before starting another file, it no-ops
and leaves the original command untouched. If a first valid rewrite has already
been computed and verified, the hook returns it instead of discarding the paid
cold-start work.

## Candidate Formats

The selector compares these candidates:

- raw/no conversion
- compact JSON
- columnar JSON as `[columns, rows]`
- CSV and TSV with JSON cells
- codebook JSON as `[columns, dictionaries, rows]` (`advanced`)
- typed CSV/TSV (`advanced`)

The columnar and codebook JSON candidates use only standard JSON while removing
repeated object keys and repeated categorical values from uniform row data. The
default hook tier uses columnar JSON for repetitive flat records until
model-quality evidence justifies dictionary or typed candidates in production.
On the included `sample-repetitive.json`, the default safe tier still produces a
large verified reduction; use the selector CLI with `--include-candidates` to
inspect exact local token counts for your tokenizer.

Every generated candidate is decoded and compared with the parsed source value
before it can win. Non-uniform object arrays do not use tabular candidates unless
the representation can preserve missing keys, nulls, and nested values exactly.

## Install

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x run-hook.sh
```

## Harness Setup

### Codex

Add the Bash hook to `~/.codex/config.toml`:

```toml
[features]
hooks = true

[[hooks.PreToolUse]]
matcher = "Bash"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/absolute/path/to/context-compression/run-hook.sh"
timeout = 30
statusMessage = "Optimizing data file reads"
```

Disable Codex by removing that `[[hooks.PreToolUse]]` block or setting
`hooks = false`. Uninstall by removing the repo checkout and any generated
`.codex/context-cache/` directories in the workspaces where you used it.

### Claude Code

Add the hook to `~/.claude/settings.json` or a project-level
`.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "CONTEXT_SELECTOR_REPO_ROOT=/absolute/path/to/context-compression /absolute/path/to/context-compression/adapters/claude-code/context_selector_hook.py"
          }
        ]
      }
    ]
  }
}
```

The hook only rewrites JSON, JSONL, CSV, and TSV whole-file `Read` calls and
simple `cat file.json` commands. Paginated reads, pipes, shell metacharacters,
unsupported formats, or failed selector verification no-op. Disable Claude Code
by removing that `PreToolUse` hook block.

### Pi

Register [`adapters/pi/context-selector-tool.ts`](adapters/pi/context-selector-tool.ts)
as a Pi extension and point it at this checkout:

```sh
export CONTEXT_SELECTOR_REPO_ROOT=/absolute/path/to/context-compression
pi -e /absolute/path/to/context-compression/adapters/pi/context-selector-tool.ts
```

The extension registers two surfaces. The invisible path listens for Pi
`tool_call` events and rewrites whole-file `read` calls and simple `bash` `cat`
calls to verified sidecars. The explicit `context_selector` tool remains
available for manual evidence collection; callers should read only the verified
`read_path` from the returned `context-selector/v1` report.

Disable Pi by unregistering that tool from the extension manifest or startup
path, or by unsetting `CONTEXT_SELECTOR_REPO_ROOT` for the transparent hook.
Uninstall by removing the extension registration and this repo checkout.

### OpenClaw

Install [`adapters/openclaw/`](adapters/openclaw/) as an optional OpenClaw
plugin and point it at this checkout:

```sh
export CONTEXT_SELECTOR_REPO_ROOT=/absolute/path/to/context-compression
```

Current OpenClaw plugin code exposes optional tools through `api.registerTool`.
It does not expose a supported transparent tool-call rewrite surface for this
adapter. The plugin therefore registers only `context_selector`: an explicit
tool that runs `selector.py --verify-report` and returns the verified selector
output. OpenClaw callers should read only the verified `read_path` values from
that report.

Disable OpenClaw by removing the plugin from the active plugin list. Uninstall
by deleting the plugin registration and this repo checkout.

### Hermes Agent

Install [`adapters/hermes-plugin/`](adapters/hermes-plugin/) as a Hermes Agent
plugin and point it at this checkout:

```sh
export CONTEXT_SELECTOR_REPO_ROOT=/absolute/path/to/context-compression
```

The plugin overrides Hermes Agent's built-in `read_file` tool, runs the selector
for supported structured whole-file reads, verifies the sidecar report, and then
delegates to the original `read_file` handler with the verified sidecar path.
Paginated reads, unsupported formats, unsafe selector results, or sidecars that
would still exceed Hermes' default read pagination safely fall back to the
original source path.

For manual evidence collection, you can also run
[`adapters/mcp/context_selector_server.py`](adapters/mcp/context_selector_server.py)
as a stdio MCP server and register its `context_selector` tool with Hermes
Agent. Hermes should trust only the verified `read_path` in the returned report.

Disable Hermes Agent by removing the plugin from `plugins.enabled` or removing
the MCP server entry from the Hermes tool configuration. Uninstall by removing
those registrations and this repo checkout.

## Verify

```sh
.venv/bin/python -m unittest discover -s tests
```

The regression suite includes a real Hugging Face tabular fixture derived from
[`julien-c/titanic-survival`](https://hf.co/datasets/julien-c/titanic-survival).
The checked-in JSON fixture has 887 records and verifies a concrete optimizer
result in the default safe tier: `column-json`, `21460` tokenizer tokens versus
raw `71983` on `gpt-5.4-mini`, a `70.2%` reduction.

Manual smoke test:

```sh
printf '%s\n' '{"hook_event_name":"PreToolUse","cwd":"'"$PWD"'","model":"gpt-5.4-mini","tool_name":"Bash","tool_input":{"command":"cat sample-repetitive.json"}}' \
  | ./run-hook.sh
```

Expected result: `hookSpecificOutput.updatedInput.command` points at an
optimized sidecar file. The default output does not include `additionalContext`;
the model receives the rewritten tool output without optimizer narration.

Run all harness smokes:

```sh
.venv/bin/python scripts/run_harness_smokes.py
```

The harness smokes are not the whole compatibility gate. They prove the local
selector/verifier path and adapter-shaped call paths. When changing adapter
glue or install instructions, also verify the actual upstream harness source
contracts:

```sh
.venv/bin/python scripts/verify_harness_contracts.py
```

See
[`docs/harness-contract-verification.md`](docs/harness-contract-verification.md)
for the exact upstream repositories and source affordances checked.

Or run them individually:

```sh
.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_codex_pretooluse_smoke_rewrites_to_verified_sidecar

.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_claude_code_read_smoke_rewrites_to_verified_sidecar

.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_claude_code_bash_smoke_rewrites_cat_to_verified_sidecar

.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_pi_smoke_returns_verified_report_with_selected_read_path

.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_openclaw_smoke_returns_verified_report_with_selected_read_path

.venv/bin/python -m unittest \
  tests.test_harness_smokes.HarnessSmokeTests.test_hermes_agent_mcp_smoke_returns_verified_report_with_selected_read_path
```

Run the local evidence gate before using benchmark or product claims:

```sh
.venv/bin/python scripts/verify_evidence.py --full-tests
```

This checks the unit suite, selector report verification, an actual Codex
`PreToolUse` rewrite smoke, deterministic eval-dataset verification, and a
benchmark smoke with a generated external baseline. It is intentionally lean:
it proves the evidence pipeline is wired correctly without rerunning the full
downloaded-corpus benchmark.

Set up the no-API quality-eval skeleton without calling a paid model:

```sh
.venv/bin/python evals/build_context_quality_dataset.py \
  --corpus data/benchmark-corpus \
  --out evals/context-quality.generated.jsonl \
  --model gpt-5.4-mini

.venv/bin/python evals/verify_context_quality_dataset.py \
  evals/context-quality.generated.jsonl
```

This verifies local raw/optimized pair integrity only. Do not promote those
results to answer-parity evidence until an Inspect run has been executed
against real models and summarized.

Verify the install path from an isolated temporary checkout:

```sh
python3 scripts/verify_clean_install.py
```

This copies the current checkout without generated caches, creates a fresh
`.venv`, installs `requirements.txt`, makes `run-hook.sh` executable, runs the
unit suite, runs the harness smokes, and runs the lean evidence gate.

## Generic Selector CLI

Use `selector.py` when an agent or benchmark wants the selector decision without
using a runtime adapter:

```sh
.venv/bin/python selector.py \
  --cwd "$PWD" \
  --model gpt-5.4-mini \
  --adapter codex-manual \
  --report-out reports/selector-report.json \
  --include-candidates \
  sample-repetitive.json
```

The CLI returns `context-selector/v1` JSON with per-file decisions, source
fingerprints, token counts, selected sidecar paths, and no-op reasons. Other
agents can safely read each result's `read_path`; it points at the optimized
sidecar only when `selected: true`, otherwise it points back at the original
source.

Adapters that substitute `read_path` should first validate the report:

```sh
.venv/bin/python verify_selector_report.py --check-files reports/selector-report.json
```

The verifier checks the substitution invariants that JSON Schema cannot express:
selected results must read `output_path`, no-op results must read the source,
summary token math must match per-file rows, and file checks must prove the
source hash, sidecar hash, sidecar path, and selected sidecar round-trip are
still valid.

See [docs/selector-evidence-layer.md](docs/selector-evidence-layer.md) for the
general selector contract and the Codex, Claude Code, Pi, Hermes, and OpenClaw
adapter shape. Adapter starting points live under [`adapters/`](adapters/).
Claude Code can use the command hook in
[`adapters/claude-code/`](adapters/claude-code/). The Pi adapter is a current
TypeScript extension with a transparent `tool_call` hook plus an explicit
evidence tool. Hermes can use the native `read_file` override plugin in
[`adapters/hermes-plugin/`](adapters/hermes-plugin/) and the stdio MCP evidence
adapter in
[`adapters/mcp/context_selector_server.py`](adapters/mcp/context_selector_server.py).
OpenClaw can use the optional plugin in
[`adapters/openclaw/`](adapters/openclaw/) as an explicit verified selector
tool.

## Tester Feedback

Keep tester notes local. Start from
[`feedback/TEMPLATE.md`](feedback/TEMPLATE.md) and write filled reports under
`feedback/local/` so they stay untracked. Capture the harness, workflow, file
type, correctness, usefulness, confusion, and failure mode together with the
report path or smoke command you used.

## Benchmark

The evidence path is intentionally larger than the checked-in fixture. Build a
local, git-ignored benchmark corpus:

```sh
.venv/bin/python benchmark.py all \
  --rows 1000 \
  --out data/benchmark-corpus \
  --corpus data/benchmark-corpus \
  --candidate-tier advanced \
  --input-price-per-1m 5 \
  --monthly-calls 100000 \
  --provider-input-tokens-per-second 1500 \
  --require-publication-corpus
```

The build step reuses existing downloaded files by default. Use
`--force-download` only when intentionally refreshing the local corpus.
For publication-facing claims, keep `--require-publication-corpus` on. It
requires the full Hugging Face corpus specification, thousands of HF rows, and
all JSON/JSONL/CSV/TSV materializations; tiny toy corpora are only acceptable
for plumbing tests.

You can check the corpus without running token counts:

```sh
.venv/bin/python benchmark.py verify-corpus --corpus data/benchmark-corpus
```

To compare against external codecs without adding them as runtime dependencies,
drop pre-encoded baseline files into per-baseline directories and point the
benchmark at them:

```sh
.venv/bin/python benchmark.py run \
  --corpus data/benchmark-corpus \
  --baseline-dir reports/baselines/toon \
  --baseline-dir reports/baselines/onto
```

Each baseline directory should contain files named like
`hf-titanic-tabular.json.txt` or `hf-titanic-tabular.json`. The benchmark will
count those texts with the same tokenizer, include coverage and win/tie/loss
counts against the selector, and fingerprint the benchmark corpus in the report
for reproducibility.

For reproducible external baselines, generate those files during the benchmark
run instead of preparing them by hand:

```sh
.venv/bin/python benchmark.py run \
  --corpus data/benchmark-corpus \
  --baseline-command 'toon=/opt/homebrew/bin/npm exec --yes --package @toon-format/toon@2.3.0 -- node scripts/toon_baseline.mjs --fallback-raw-on-fail {input} {output}' \
  --baseline-command 'onto=onto encode --input {input} --output {output}'
```

`--baseline-command` is benchmark-only. It runs one command per corpus file,
requires the command to write `{output}`, and then counts the generated baseline
with the same tokenizer as the selector. This keeps TOON, ONTO, LLMLingua, or
other comparator tooling out of the conservative hook runtime while making
baseline coverage reproducible. Generated baseline directories include
`baseline-provenance.json` with the command template, rendered per-file
commands, and source/output SHA-256 hashes; the benchmark report links that
manifest under `baseline_provenance`.

The TOON helper is intentionally outside runtime:
[`scripts/toon_baseline.mjs`](scripts/toon_baseline.mjs) imports
`@toon-format/toon`, parses JSON/JSONL/CSV/TSV into the same data model used by
the benchmark, encodes TOON, decodes it, and refuses to write output if the
parsed value changes. Use `--fallback-raw-on-fail` for full-corpus comparator
runs; unsupported TOON cases are then counted as raw fallback rather than
silently corrupted or dropped.

The current local downloaded corpus spans Hugging Face tabular, review,
conversation, QA, code/documentation, and LogHub 2.0 log datasets plus GitHub
repository metadata, materialized as JSON, JSONL, CSV, and TSV.
The corpus gate also enforces shape coverage for flat/tabular, nested, text,
conversation, QA, code, long-text, and repetitive-log data so the benchmark is
not a toy single-shape result.
See [docs/paper-dataset-alignment.md](docs/paper-dataset-alignment.md) for how
these public slices map to recent ONTO, dictionary-encoding, TOON, and
LLMLingua-style baselines.

Benchmark requirement: the evaluation suite should include matched external
baselines and should aim to beat them on the axes they cover. That means
semantic prompt compressors such as LLMLingua and LongLLMLingua for
natural-language prompt reduction, and structured-data compressors such as TOON,
ONTO, LoPace, and dictionary-encoding methods for lossless or near-lossless
structured inputs. Where the task overlaps with long-context retrieval or
context-efficiency methods, include ILRe-style baselines as well.

Latest local run numbers live in [`EVIDENCE.md`](EVIDENCE.md) and
[`reports/benchmark-report.md`](reports/benchmark-report.md). Rerun the command
above after changing the corpus, tokenizer, candidate set, candidate tier, or
price scenario before using token or dollar claims externally.

See [EVIDENCE.md](EVIDENCE.md) for the corpus, commands, and limits of the
claim.

For model-quality checks, use the optional Inspect AI path in
[evals/README.md](evals/README.md). It can generate paired raw/optimized exact
answer tasks from the benchmark corpus, including nested-value, null, repeated
value, and delimiter-adversary slices, while keeping eval execution in an
existing framework instead of adding a custom runner to the hook.

## Model And Tokenizer Handling

Adapters pass the active model slug as `model` when the host exposes it. The
selector resolves model metadata from:

- the adapter or hook payload, if present
- `~/.codex/config.toml`
- project `.codex/config.toml`
- `model_catalog_json` from Codex config
- `CONTEXT_OPTIMIZER_MODEL_CATALOG_JSON`
- bundled `model-catalog.snapshot.json`

OpenAI model slugs use `tiktoken` when available. Modern GPT/Codex slugs not
yet mapped by `tiktoken` default to `o200k_base`; override with
`CONTEXT_OPTIMIZER_TIKTOKEN_ENCODING` when needed.

For non-OpenAI models, set:

```sh
CONTEXT_OPTIMIZER_TOKENIZER_JSON=/path/to/tokenizer.json
```

When this is set and `tokenizers` is installed, the selector uses that exact
Hugging Face tokenizer instead of the fallback estimate.

## Prior Art And Research Basis

This project is intentionally a deterministic format selector, not a universal
new notation or a reimplementation of every prior codec.

- Prompt compression work such as
  [LLMLingua](https://hf.co/papers/2310.05736),
  [LongLLMLingua](https://hf.co/papers/2310.06839), and
  [An Empirical Study on Prompt Compression](https://hf.co/papers/2505.00019)
  shows that shorter prompts can reduce cost and latency, but compression rate
  must be balanced against quality.
- Table and structured-data studies such as
  [Table Meets LLM](https://hf.co/papers/2305.13062),
  [StructEval](https://hf.co/papers/2505.20139), and
  [StrucText-Eval](https://hf.co/papers/2406.10621) show that format choices
  affect structural understanding, not just token count.
- Tokenizer research such as
  [An Information-Theoretic Perspective on LLM Tokenizers](https://hf.co/papers/2601.09039)
  and
  [Exact Byte-Level Probabilities from Tokenized Language Models](https://hf.co/papers/2410.09303)
  supports model/tokenizer-specific counting instead of character-count
  heuristics.
- [TOON](https://github.com/toon-format/toon) is useful for uniform arrays and
  mixed structured data, but its own documentation notes cases where compact
  JSON or CSV can be better.
- [ONTO](https://arxiv.org/abs/2604.17512) shows that columnar notation can
  cut JSON token count materially on operational records, but its current
  evidence is synthetic and task-scoped.
- The recent paper
  [Lossless Prompt Compression via Dictionary-Encoding and In-Context Learning](https://arxiv.org/abs/2604.13066)
  is the closest direct match to this repo's codebook direction.

Implementation consequence: TOON, ONTO, and similar codecs should be treated as
external baselines in the benchmark harness rather than absorbed into the hook
runtime until they beat the current selector on token savings and answer parity
for repo-shaped data.

Benchmarking implication: the repo should not treat these as background
references only. The benchmark contract should compare against the best matched
baseline for each input family and require the local selector to win on the
targeted axis, whether that is token savings, exact round-trip fidelity,
deterministic behavior, latency break-even, or utility at a fixed budget.

See [RESEARCH.md](RESEARCH.md) for the full research notes and how each finding
maps to the implementation.

## Current Boundary

This is complete for the current hook-based workaround: file-as-context reads
are optimized without changing the original files.

Current Codex `UserPromptSubmit` hooks cannot replace pasted prompt text or
app-injected attachment content. True invisible replacement for those paths
requires a Codex core change that adds an `updatedInput`/`updatedPrompt` field
to `UserPromptSubmit` and applies it before pending input is recorded.

## License

MIT
