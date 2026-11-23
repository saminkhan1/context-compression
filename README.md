# Context Compression for Structured Agent Data

Cut the tokens an AI agent spends on JSON, JSONL, CSV and TSV files without changing
its answers. Two tools, one rule: **inline small data losslessly, query large data.**

- **Selector** ([`selector.py`](selector.py), [`hook.py`](hook.py)): for each file, it
  generates lossless candidate encodings, verifies each round-trips to the parsed
  source, counts tokens with the active model's tokenizer, and returns the cheapest
  read path as a labelled sidecar under `.codex/context-cache/`. Source files are never
  modified.
- **Schema preview** ([`preview.py`](preview.py)): for large files, it writes a short,
  labelled description (shape, fields, types, two sample rows). The agent then answers
  with code against the real file instead of reading the rows.

## Evidence

Everything here is claude-haiku-4-5 only, measured on the official
[TOON retrieval benchmark](https://github.com/toon-format/toon/tree/main/benchmarks) (fresh seeds 9001-9003) and on
[DataBench](https://huggingface.co/datasets/cardiffnlp/databench) (70 real tables, 1,613 questions, official comparator).
The study was [pre-registered](bench/PREREGISTRATION.md) before the confirmatory runs; raw per-question results,
transcripts and `bench/final_stats.sh` are in [`bench/results2/`](bench/results2/). Intervals are 95% cluster
bootstraps over datasets.

**Give the agent a schema preview.** A ~150-token description of the file (names, types, row count) before it runs code:

| Where | Accuracy with preview | Without | Tokens per question, with vs without |
| --- | --- | --- | --- |
| Six synthetic benchmark sets (n = 1,434) | 97.6% | 96.4% (+1.1 pp [+0.2, +2.1]) | 3,150 vs 6,156: **1.9x fewer** |
| DataBench, real tables (n = 1,613) | **86.8%** | 81.0% (+5.8 pp [+4.0, +7.5]) | 7,096 vs 7,037: no saving |

So the preview saves tokens on synthetic files and raises accuracy on real tables. Names alone work as well as the
fuller preview (+0.1 pp, 12% fewer tokens).

**Don't read big files into context.** Reading cost grows with file size; an agent's does not.

| Setting | Agent (code) | Reading in context | Note |
| --- | --- | --- | --- |
| 2,000-row-class files, thinking off | 97.5% at 3,047 tok/Q | 61.9% at 51,754 | **17x fewer tokens** (interval 5-31x; depends on size) |
| ≤100-row files, thinking off (n = 717) | 97.6% | 64.6% | 1.7x fewer tokens |
| ≤100-row files, **thinking on** (n = 239) | 97.1% at 3,276 | 95.0% at 6,346 | accuracy gap not detectable (+2.1 pp [-1.8, +6.9]) |
| Small real tables ≤10k tokens (n = 166) | 88.0% | 52.4% | same token cost |

Thinking-on reading was not tested on large files, so the large-file accuracy gap is against thinking-off reading.

**Not robust: one-shot code.** A single script with no repair step matches the agent on the synthetic sets (98.0% at
1,242 tok/Q) but loses 7.5 pp on DataBench (79.3% vs 86.8%), where scripts crash on column names.

**What is not claimed.** The idea of answering table questions with code is established (PAL, DataBench entries).
Other models, wall-clock time and production savings were not tested; the synthetic questions are templated and favour
code. An earlier version of this README claimed 14.8x and 41.8x: those compared against an agent carrying Claude Code's
Bash tool schema and against thinking-off reading, and used 32-question baselines. They are withdrawn
(see [`bench/FINDINGS.md`](bench/FINDINGS.md)).

**Inline small data losslessly.** Token totals on TOON's token-efficiency datasets (o200k_base, primer and view header
included); accuracy at seed 12345 is from the earlier development study:

| Encoding | Tokens | vs TOON | Accuracy (seed 12345) |
| --- | ---: | ---: | ---: |
| JSON compact | 366,981 | +9.9% | – |
| JSON pretty | 560,684 | +68% | 60.7% |
| TOON | 334,046 | – | 66.1% |
| selector, `advanced` tier (`ctab` wins 7/8 datasets) | **246,038** | **−26.3%** | 61.9% |

`ctab` matches JSON's accuracy at half the tokens and trails TOON slightly (p = 0.04), with thinking off.

**Lessons built into the defaults:**

- Lossless is not answer-preserving: the former `codebook-json` candidate scored 51.9%, so it is experimental-only.
- Every sidecar states the file's on-disk format. Without that label, an agent shown a re-encoded view wrote code for the wrong shape.

## Quick Start

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests

.venv/bin/python selector.py --include-candidates --verify-report sample-repetitive.json
.venv/bin/python selector.py --preview-above-tokens 4000 data/big.json   # adds preview_path
```

The output is a `context-selector/v1` report
([schema](docs/schemas/context-selector-v1.schema.json)). Each result has the source,
the verified `read_path`, `selected_format`, the token counts, and source and sidecar
hashes. Adapters must trust only the verified `read_path`.

## Candidates and Selection

| Tier (`CONTEXT_OPTIMIZER_CANDIDATE_TIER`) | Candidates |
| --- | --- |
| `safe` (default) | raw, compact JSON, column JSON `[columns, rows]`, CSV/TSV with JSON cells |
| `advanced` | safe + [`ctab`](ctab.py) (sectioned tables, grouped headers, verified list-length columns) + typed CSV/TSV |
| `experimental` | advanced + codebook JSON (measured −8.8 pp accuracy vs JSON) |

```text
best = argmin tokens(view_header + decoder_instructions + candidate)
       subject to decode(candidate) == parsed_source
select only if best != raw
       and savings >= CONTEXT_OPTIMIZER_MIN_SAVINGS_RATIO   (default 0.05)
       and saved_tokens >= CONTEXT_OPTIMIZER_MIN_SAVED_TOKENS (default 128)
```

The selection is deterministic for a fixed input, tokenizer and tier.

- **Hooks stay conservative.** `jq`, `grep`, `sed`, `head`, pipes, `cat -n`, partial
  reads and unsupported files are left raw.
- **Optional latency gate.** `CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND`,
  `CONTEXT_OPTIMIZER_MIN_NET_LATENCY_SAVED_MS` and
  `CONTEXT_OPTIMIZER_MAX_HOOK_LATENCY_MS` skip rewrites whose local cost outweighs the
  provider-side saving.

**Tokenizers:**

- OpenAI slugs use `tiktoken`. Unmapped modern slugs default to `o200k_base`; override
  with `CONTEXT_OPTIMIZER_TIKTOKEN_ENCODING`.
- For other models, set `CONTEXT_OPTIMIZER_TOKENIZER_JSON=/path/to/tokenizer.json`.
- Without a tokenizer, counts are labelled estimated, and only `safe` candidates are
  used.
- Model metadata comes from the hook payload, Codex config, or
  [`model-catalog.snapshot.json`](model-catalog.snapshot.json).

## Adapters

All adapters are thin wrappers over the selector; see
[`adapters/CONTRACT.md`](adapters/CONTRACT.md).

| Runtime | Adapter | Behavior |
| --- | --- | --- |
| Codex | [`run-hook.sh`](run-hook.sh) | `PreToolUse` rewrites whole-file Bash `cat` reads to verified sidecars |
| Claude Code | [`adapters/claude-code/`](adapters/claude-code/) | Rewrites whole-file `Read` and simple `cat` reads (Bash rewrites use `ask`) |
| Pi | [`adapters/pi/`](adapters/pi/) | Whole-file read substitution plus an explicit `context_selector` tool |
| Hermes Agent | [`adapters/hermes-plugin/`](adapters/hermes-plugin/) | Overrides `read_file` for supported whole-file reads |
| MCP | [`adapters/mcp/`](adapters/mcp/) | Explicit stdio `context_selector` tool |
| OpenClaw | [`adapters/openclaw/`](adapters/openclaw/) | Explicit verified selector tool |
| Generic | [`adapters/generic/`](adapters/generic/) | Portable selector contract |

Codex setup (`~/.codex/config.toml`):

```toml
[features]
hooks = true

[[hooks.PreToolUse]]
matcher = "Bash"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/absolute/path/to/context-compression/run-hook.sh"
timeout = 30
```

When adapter glue changes, run `scripts/verify_harness_contracts.py` against installed
hosts. Details are in [`docs/harness-contract-verification.md`](docs/harness-contract-verification.md).

## Benchmarks

- [`bench/`](bench/) holds the accuracy and token benchmark on TOON's suite, behind every
  number above. Reproduce steps are in [`bench/FINDINGS.md`](bench/FINDINGS.md#6-reproduce).
- [`benchmark.py`](benchmark.py) is the token-only benchmark over downloaded public
  datasets, with external codec baselines (`--baseline-dir`, `--baseline-command`). See
  [`data/README.md`](data/README.md).

## License

MIT
