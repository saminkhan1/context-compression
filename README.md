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

Full write-up, method, and raw per-question results: [`bench/FINDINGS.md`](bench/FINDINGS.md)
(Nov 2026). Everything below was measured on the official
[TOON retrieval benchmark](https://github.com/toon-format/toon/tree/main/benchmarks), with
its own datasets, questions, and deterministic grader.

**Headline (fresh, untuned seed; system-level: the model writes code against the file
instead of reading the rows).** All billed tokens are counted (input, output, all turns,
overhead):

| Regime | Code from schema preview ([`preview.py`](preview.py)) | TOON in context | Fewer billed tokens |
| --- | --- | --- | --- |
| ≤100 rows | **97.5%** at 1,230 tok/Q | 64.9% at 5,410 tok/Q | **4.4×** |
| 100–2,000 rows | **98.3%** at 1,236 tok/Q | 68.8% at 51,697 tok/Q | **41.8×** |

Against a plain tool-using agent (Bash, no preview) on the same large questions: 93.8% at
18,127 tok/Q vs **100% at 1,221 tok/Q, 14.8× fewer**.

The reduction scales with size: about 1× on a 6-record config, 2–8× under 100 records,
and 11–139× for 500–2,000 records. Below roughly 400 records, inline lossless views
(below) are the better choice. This compares a code-executing reader with in-context
formats, on templated retrieval questions with one model (claude-haiku-4-5); see
[`bench/FINDINGS.md`](bench/FINDINGS.md) for caveats, including the github-dataset
disclosure.

```sh
.venv/bin/python selector.py --preview-above-tokens 4000 data/big.json   # adds a labelled preview_path
```

**Tokens** (TOON's token-efficiency datasets, o200k_base, primer and view header included):

| Encoding | Total tokens | vs TOON |
| --- | ---: | ---: |
| JSON compact | 366,981 | +9.9% |
| TOON | 334,046 | – |
| this selector, `advanced` tier (picks `ctab` on 7/8 datasets) | **246,038** | **−26.3%** |

**Accuracy** (claude-haiku-4-5, 239 questions, official seed 12345):

| Arm | Accuracy | Data tokens / question |
| --- | ---: | ---: |
| JSON pretty | 60.7% | 6,964 |
| TOON | 66.1% | 4,154 |
| `ctab` (this repo) | 61.9% | 3,503 |
| `codebook-json` (former best candidate) | 51.9% | 5,103 |
| schema preview + Bash/python tool (2 turns; billed 10.3k input tok/Q) | 97.1% | 232 |

What this means:

- `ctab` matches JSON's accuracy (p = 0.63) at half the tokens. It is still slightly *below*
  TOON on accuracy (p = 0.04), but it is the best of any inline format per token.
- Lossless is not the same as answer-preserving. `codebook-json` was lossless and scored
  lowest, so it now lives in the `experimental` tier.
- The largest lever is the harness, not the notation. For large files or
  aggregation/filtering questions, give the model a schema preview and let it query the
  file.
- Every sidecar view is now labelled with the file's on-disk format. Without the label, an
  agent shown a re-encoded view wrote code for the wrong file shape.

The earlier 28-file corpus run (13.3% aggregate savings) is still documented in
[`reports/benchmark-report.md`](reports/benchmark-report.md) and [`EVIDENCE.md`](EVIDENCE.md).
It measured tokens only.

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

Advanced candidates (opt-in):

- `ctab`: sectioned tables with grouped headers and verified list-length columns, compact
  JSON elsewhere ([`ctab.py`](ctab.py))
- typed CSV/TSV

Experimental (`CONTEXT_OPTIMIZER_CANDIDATE_TIER=experimental`): codebook JSON as
`[columns, dictionaries, rows]`. It measured −8.8 pp vs JSON on retrieval accuracy.

Enable the larger candidate set only when you are intentionally evaluating it:

```sh
CONTEXT_OPTIMIZER_CANDIDATE_TIER=advanced
```

Keep `advanced` out of invisible runtime hooks until paired quality evals prove
answer parity for the target model family.

Selection rule:

```text
best = argmin token_count(view_header + decoder_instructions(candidate) + candidate, model)
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
