# Lossless Context Compression for Structured Agent Data

A deterministic selector that reduces the token cost of structured files before
they enter an AI agent's context window.

The selector supports local JSON, JSONL, CSV, and TSV files. For each file, it
generates reversible candidate representations, counts tokens for the active
model tokenizer, verifies that the winning candidate decodes back to the parsed
source value, and returns the lowest-token safe read path.

It is built for agent runtimes such as Codex, Claude Code, Pi, Hermes Agent,
OpenClaw, MCP tools, and generic local agents that often read large structured
files as context.

## What This Is

- **Lossless structured-context selection:** not semantic summarization, not
  fuzzy prompt compression.
- **Source-file preserving:** adapters write optimized sidecars under
  `.codex/context-cache/` and never rewrite the referenced data files.
- **Tokenizer-aware:** token counts use `tiktoken` for OpenAI models or a
  configured Hugging Face `tokenizer.json`; fallback counts are labelled as
  estimated.
- **Deterministic:** fixed input, model profile, tokenizer, and candidate tier
  produce the same selection.
- **Conservative at runtime:** semantic shell operations such as `jq`, `grep`,
  `sed`, `head`, pipes, `cat -n`, unsupported files, and low-savings cases
  stay raw.

The core flow is:

```text
agent reads a structured file
-> adapter asks selector.py for a verified decision
-> selector writes a lower-token sidecar when it is safe
-> adapter substitutes the verified read_path
-> model sees optimized content, source file remains unchanged
```

## Current Evidence

Latest checked-in downloaded-corpus run:

- Model: `gpt-5.4-mini` via `tiktoken`
- Corpus: 28 JSON/JSONL/CSV/TSV files from public tabular, QA, conversation,
  review, code/documentation, log, and GitHub metadata datasets
- Candidate tier: benchmark path includes advanced candidates; runtime hooks
  default to the safer tier

| Raw tokens | Optimized tokens | Saved tokens | Savings |
| ---: | ---: | ---: | ---: |
| 17,496,442 | 15,171,483 | 2,324,959 | 13.3% |

Best source-family reductions in that run:

| Source family | Savings | Winning format |
| --- | ---: | --- |
| SQuAD QA rows | 72.7% | codebook-json |
| Titanic tabular rows | 50.5% | codebook-json |
| LogHub 2.0 logs | 41.5% | codebook-json |
| GitHub repository metadata | 29.1% | codebook-json |

See [`reports/benchmark-report.md`](reports/benchmark-report.md) and
[`EVIDENCE.md`](EVIDENCE.md) for the full corpus, commands, per-file results,
baseline handling, and limits of the claim.

Important boundary: the repo currently proves deterministic token savings and
round-trip safety. Full answer-parity evidence across model families is still
an optional eval path, not a completed production claim.

## Quick Start

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x run-hook.sh
```

Run the unit suite:

```sh
.venv/bin/python -m unittest discover -s tests
```

Try the selector directly:

```sh
.venv/bin/python selector.py \
  --cwd "$PWD" \
  --model gpt-5.4-mini \
  --adapter manual \
  --include-candidates \
  --verify-report \
  sample-repetitive.json
```

The output is a `context-selector/v1` JSON report. Each result contains:

- `source`: original file path
- `read_path`: the verified path an adapter may read
- `selected`: whether a sidecar won
- `selected_format`, `raw_tokens`, `selected_tokens`, `saved_tokens`
- source and sidecar hashes for auditability

Adapters should trust only the verified `read_path`, not a raw `output_path`.

## Codex Hook

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

Manual smoke test:

```sh
printf '%s\n' '{"hook_event_name":"PreToolUse","cwd":"'"$PWD"'","model":"gpt-5.4-mini","tool_name":"Bash","tool_input":{"command":"cat sample-repetitive.json"}}' \
  | ./run-hook.sh
```

Expected result: `hookSpecificOutput.updatedInput.command` points at a sidecar
under `.codex/context-cache/`. The hook does not add optimizer narration to the
conversation.

Current Codex boundary: `PreToolUse` can rewrite whole-file Bash reads.
`UserPromptSubmit` no-ops by default because current Codex hooks cannot
invisibly replace pasted prompt text or app-injected file attachments.

## Other Adapters

All adapters are thin wrappers over the same selector/verifier contract. They
must not implement independent compression logic.

| Runtime | Adapter | Behavior |
| --- | --- | --- |
| Claude Code | [`adapters/claude-code/`](adapters/claude-code/) | Rewrites whole-file `Read` calls and simple Bash `cat` reads to verified sidecars. Bash rewrites use `ask`. |
| Pi | [`adapters/pi/context-selector-tool.ts`](adapters/pi/context-selector-tool.ts) | Provides transparent whole-file read substitution plus an explicit `context_selector` evidence tool. |
| Hermes Agent | [`adapters/hermes-plugin/`](adapters/hermes-plugin/) | Overrides `read_file` for supported whole-file reads and falls back to the original path when unsafe. |
| MCP | [`adapters/mcp/context_selector_server.py`](adapters/mcp/context_selector_server.py) | Exposes an explicit stdio `context_selector` tool. |
| OpenClaw | [`adapters/openclaw/`](adapters/openclaw/) | Registers an explicit verified selector tool; no transparent rewrite surface is assumed. |
| Generic agents | [`adapters/generic/context-selector-tool.md`](adapters/generic/context-selector-tool.md) | Documents the portable selector contract. |

See [`adapters/CONTRACT.md`](adapters/CONTRACT.md) for the required adapter
invariants.

## Runtime Contract

Supported source formats are JSON, JSONL, CSV, and TSV.

The default runtime candidate tier is `safe`:

- raw/no conversion
- compact JSON
- columnar JSON as `[columns, rows]`
- CSV/TSV with JSON cells

Advanced candidates are available for offline evaluation:

- codebook JSON as `[columns, dictionaries, rows]`
- typed CSV/TSV

Enable the larger candidate set only when you are intentionally evaluating it:

```sh
CONTEXT_OPTIMIZER_CANDIDATE_TIER=advanced
```

Keep `advanced` out of invisible runtime hooks until paired quality evals prove
answer parity for the target model family.

Selection rule:

```text
best = argmin token_count(decoder_instructions(candidate) + candidate, model)
       subject to round_trip(candidate) == parsed_source_value

select only if best != raw
       and savings >= CONTEXT_OPTIMIZER_MIN_SAVINGS_RATIO
       and saved_tokens >= CONTEXT_OPTIMIZER_MIN_SAVED_TOKENS
```

The default absolute floor is `128` saved tokens. This keeps tiny token wins out
of invisible runtime rewrites, where local preprocessing may cost more than the
provider-side savings.

Optional latency gate:

```sh
CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND=1500
CONTEXT_OPTIMIZER_MIN_NET_LATENCY_SAVED_MS=0
CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS=500
```

When provider throughput is configured, `PreToolUse` rewrites only when the
projected provider-side input latency saved by fewer tokens is greater than
local preprocessing time plus any configured margin.

## Verification

Use the unit suite after changing hook behavior, candidate encoding/decoding,
model-profile resolution, selector reports, or Bash rewrite logic:

```sh
.venv/bin/python -m unittest discover -s tests
```

Run local harness smokes:

```sh
.venv/bin/python scripts/run_harness_smokes.py
```

When changing adapter glue or install instructions, verify host contract
assumptions too:

```sh
.venv/bin/python scripts/verify_harness_contracts.py
```

Run the lean evidence gate before using benchmark or product claims:

```sh
.venv/bin/python scripts/verify_evidence.py --full-tests
```

Verify a clean install from an isolated temporary checkout:

```sh
python3 scripts/verify_clean_install.py
```

## Benchmarking

Build or refresh the local, git-ignored benchmark corpus:

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

`--input-price-per-1m` is a scenario value. API pricing changes, so rerun with
current pricing before using dollar projections externally.

Check the corpus without running token counts:

```sh
.venv/bin/python benchmark.py verify-corpus --corpus data/benchmark-corpus
```

To compare external codecs without adding runtime dependencies, use
`--baseline-dir` or reproducible `--baseline-command` inputs:

```sh
.venv/bin/python benchmark.py run \
  --corpus data/benchmark-corpus \
  --baseline-command 'toon=/opt/homebrew/bin/npm exec --yes --package @toon-format/toon@2.3.0 -- node scripts/toon_baseline.mjs --fallback-raw-on-fail {input} {output}' \
  --baseline-command 'onto=onto encode --input {input} --output {output}'
```

Generated baseline outputs include provenance and hashes so baseline claims are
auditable. Baseline tooling stays benchmark-only until it wins on token count,
round-trip safety, deterministic behavior, latency break-even, and answer
parity for the relevant data family.

## Model And Tokenizer Handling

Adapters pass the active model slug as `model` when the host exposes it. The
selector resolves model metadata from:

- the adapter or hook payload
- `~/.codex/config.toml`
- project `.codex/config.toml`
- `model_catalog_json` from Codex config
- `CONTEXT_OPTIMIZER_MODEL_CATALOG_JSON`
- bundled [`model-catalog.snapshot.json`](model-catalog.snapshot.json)

OpenAI model slugs use `tiktoken` when available. Modern GPT/Codex slugs not
yet mapped by `tiktoken` default to `o200k_base`; override with
`CONTEXT_OPTIMIZER_TIKTOKEN_ENCODING` when needed.

For non-OpenAI models, set:

```sh
CONTEXT_OPTIMIZER_TOKENIZER_JSON=/path/to/tokenizer.json
```

When this is set and `tokenizers` is installed, the selector uses that exact
Hugging Face tokenizer instead of the fallback estimate.

## Quality Evals

The optional Inspect AI path builds paired raw/optimized tasks from benchmark
data:

```sh
.venv/bin/python evals/build_context_quality_dataset.py \
  --corpus data/benchmark-corpus \
  --out evals/context-quality.generated.jsonl \
  --model gpt-5.4-mini

.venv/bin/python evals/verify_context_quality_dataset.py \
  evals/context-quality.generated.jsonl
```

That verifies local pair integrity only. Do not promote it to answer-parity
evidence until an Inspect run has been executed against real models and
summarized. See [`evals/README.md`](evals/README.md).

## Research Notes

This project is a deterministic format selector, not a universal new notation.
Related work includes LLMLingua/LongLLMLingua-style prompt compression, TOON,
ONTO, table-structure evals, tokenizer studies, and dictionary-encoding
approaches.

Those systems should be treated as matched benchmark baselines rather than
background references only. The selector should win on the target axis for the
input family being claimed: token savings, exact round-trip fidelity,
determinism, latency break-even, or model utility at a fixed budget.

See [`RESEARCH.md`](RESEARCH.md) and
[`PAPER_SWEEP_2025_2026.md`](PAPER_SWEEP_2025_2026.md) for the full notes.

## Repository Map

- [`selector.py`](selector.py): reusable selector CLI and
  `context-selector/v1` report surface
- [`hook.py`](hook.py): conservative Codex hook runtime
- [`verify_selector_report.py`](verify_selector_report.py): report verifier
- [`benchmark.py`](benchmark.py): corpus build, token benchmark, and baseline
  comparison path
- [`adapters/`](adapters/): Codex-adjacent and agent-runtime integrations
- [`evals/`](evals/): optional Inspect AI quality-check path
- [`reports/`](reports/), [`EVIDENCE.md`](EVIDENCE.md): checked-in evidence
  surfaces

Generated local artifacts under `.codex/context-cache/`,
`data/benchmark-corpus/`, `logs/`, `.venv/`, and Python cache directories should
stay untracked.

## License

MIT
