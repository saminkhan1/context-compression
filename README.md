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

Measured on the official
[TOON retrieval benchmark](https://github.com/toon-format/toon/tree/main/benchmarks)
(its datasets, questions and deterministic grader), using claude-haiku-4-5. The method,
caveats and raw per-question results are in [`bench/FINDINGS.md`](bench/FINDINGS.md).

**Query large data.** This is system-level: the model writes code against the file
instead of reading the rows. Results are on a fresh, untuned seed, counting all billed
tokens (input, output, all turns, overhead):

| Regime | Code from schema preview | Baseline | Fewer billed tokens |
| --- | --- | --- | --- |
| 100–2,000 rows | **100%** at 1,221 tok/Q | plain Bash agent, no preview: 93.8% at 18,127 | **14.8×** |
| 100–2,000 rows | **98.3%** at 1,236 tok/Q | TOON in context: 68.8% at 51,697 | **41.8×** |
| ≤100 rows | **97.5%** at 1,230 tok/Q | TOON in context: 64.9% at 5,410 | 4.4× |

The gain scales with size: about 1× on a 6-record config, 2–8× under 100 records, and
11–139× from 500 to 2,000 records. The questions are templated lookups and aggregations,
and only one model was tested.

**Inline small data losslessly.** Token totals on TOON's token-efficiency datasets
(o200k_base, primer and view header included):

| Encoding | Tokens | vs TOON | Accuracy (seed 12345) |
| --- | ---: | ---: | ---: |
| JSON compact | 366,981 | +9.9% | – |
| JSON pretty | 560,684 | +68% | 60.7% |
| TOON | 334,046 | – | 66.1% |
| selector, `advanced` tier (`ctab` wins 7/8 datasets) | **246,038** | **−26.3%** | 61.9% |

`ctab` matches JSON's accuracy at half the tokens. It trails TOON slightly on accuracy
(p = 0.04).

**Lessons built into the defaults:**

- Lossless is not answer-preserving: the former `codebook-json` candidate scored 51.9%,
  so it is experimental-only.
- Every sidecar now states the file's on-disk format. Without that label, an agent shown
  a re-encoded view wrote code for the wrong file shape.

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
