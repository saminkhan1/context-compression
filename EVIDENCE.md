# Evidence And Benchmarking

This project should be evaluated as a deterministic structured-context format
selector, not as a semantic prompt compressor.

## Current Evidence Bar

The implementation now requires two gates before it selects an optimized
representation:

- the candidate must round-trip back to the parsed source value
- the candidate plus its decoder instruction must use fewer exact model tokens
  than the source under the active tokenizer

This matters because token savings alone are not enough. A tabular encoding that
collapses missing fields into nulls, or loses nested values, is not acceptable
for agent context.

The benchmark contract should include matched external baselines and should
show the local selector winning on the relevant axis for each input family. For
natural-language prompt reduction, that means LLMLingua/LongLLMLingua-style
comparisons; for structured inputs, that means TOON, ONTO, LoPace, TONL, and
dictionary-encoding style baselines; and for long-context efficiency overlap,
ILRe-style comparisons where the setting matches.

## Downloaded Corpus

The benchmark corpus is downloaded locally under `data/benchmark-corpus/`. That
directory is intentionally git-ignored because it is large benchmark data, not
source code.

Current local corpus:

| Source | Rows | Shapes |
| --- | ---: | --- |
| [`julien-c/titanic-survival`](https://hf.co/datasets/julien-c/titanic-survival) | 887 | flat tabular passenger data |
| [`SetFit/amazon_reviews_multi_en`](https://hf.co/datasets/SetFit/amazon_reviews_multi_en) | 1,000 | review text, labels, ids |
| [`OpenAssistant/oasst1`](https://hf.co/datasets/OpenAssistant/oasst1) | 1,000 | conversation rows with nested metadata |
| [`rajpurkar/squad`](https://hf.co/datasets/rajpurkar/squad) | 1,000 | QA rows with nested answer spans |
| [`codeparrot/github-jupyter-code-to-text`](https://hf.co/datasets/codeparrot/github-jupyter-code-to-text) | 1,000 | code and documentation text |
| [`bolu61/loghub_2`](https://hf.co/datasets/bolu61/loghub_2) | 1,000 | LogHub 2.0 repetitive system log lines |
| [GitHub top repositories search](https://api.github.com/search/repositories?q=stars:%3E50000&sort=stars&order=desc) | 425 | repository metadata with nested owner/license data |

Each source is materialized as JSON, JSONL, CSV, and TSV, so the benchmark does
not depend on one file format or one data shape.

The Hugging Face sources are intentionally mixed rather than toy-sized:

- Titanic covers flat tabular data with repeated categorical and numeric fields.
- Amazon reviews covers long text records with repeated schema and labels.
- OpenAssistant covers agent-like conversations with nested metadata.
- SQuAD covers question-answering rows with nested answer arrays.
- GitHub notebook code-to-text covers code/documentation rows with long cells.
- LogHub 2.0 covers the repetitive-log benchmark family used by recent
  dictionary-compression work.

`benchmark.py verify-corpus` enforces this source list, row scale, four-format
materialization, and required shape coverage before a run can be used as
publication-facing evidence. See
[`docs/paper-dataset-alignment.md`](docs/paper-dataset-alignment.md) for how
these public Hugging Face slices map to recent paper datasets and baselines.

## Reproduce

Install dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the lean evidence gate:

```sh
.venv/bin/python scripts/verify_evidence.py --full-tests
```

This is the default pre-claim check. It runs the unit suite, verifies a selector
report with file round-trip checks, exercises the Codex `PreToolUse` rewrite,
builds and verifies a paired raw/optimized eval dataset from checked-in
fixtures, and runs a benchmark smoke with a generated external baseline. It
does not replace the full
downloaded-corpus benchmark or real model-quality evals; it prevents broken
evidence plumbing from being mistaken for a valid result.

Run the harness source-contract gate when adapter support changes:

```sh
.venv/bin/python scripts/verify_harness_contracts.py \
  --upstream-root /tmp/context-compression-upstream
```

This checks official upstream source checkouts for the Codex `PreToolUse`
rewrite contract, Pi extension tool registration and mutable `tool_call`
events, OpenClaw `before_tool_call` parameter rewrite and plugin tool
registration, Hermes Agent plugin tool override support for `read_file`, and
Hermes Agent MCP stdio configuration surface. It is a separate gate from local
smokes; if it fails, local smokes are not sufficient evidence of installable
harness support.

Run the clean-install gate before tagging an MVP candidate:

```sh
python3 scripts/verify_clean_install.py
```

This proves setup from a temporary clean copy: fresh virtualenv, runtime
dependency install, hook runner permissions, unit tests, four harness smokes,
and the lean evidence gate.

Build or refresh the corpus:

```sh
.venv/bin/python benchmark.py all \
  --rows 1000 \
  --out data/benchmark-corpus \
  --corpus data/benchmark-corpus \
  --input-price-per-1m 5 \
  --monthly-calls 100000 \
  --provider-input-tokens-per-second 1500 \
  --require-publication-corpus
```

`--input-price-per-1m` is explicit because API pricing changes. The current
local report uses `$5.00 / 1M input tokens` as a scenario value; rerun with the
current vendor price before using the dollar projection in external material.

Build mode reuses existing downloaded files by default, which keeps local
iteration fast and avoids unnecessary repeated traffic to public dataset APIs.
Use `--force-download` when you intentionally want a fresh pull, and
`--allow-partial` only when you are explicitly accepting an incomplete corpus.
For claims intended for a paper, deck, or customer-facing material, the corpus
must pass:

```sh
.venv/bin/python benchmark.py verify-corpus --corpus data/benchmark-corpus
```

This gate requires the configured Hugging Face Dataset Viewer sources, thousands
of HF rows, and all four supported materializations. Toy corpora remain useful
for unit tests and smoke tests, but they are not evidence for token or dollar
savings claims.

To compare matched external baselines in the same report without adding them to
the hook runtime, place each baseline's encoded outputs in its own directory
and pass one or more `--baseline-dir` flags to `benchmark.py run` or
`benchmark.py all`. Each directory should contain one file per source, named
`<source-file>` or `<source-file>.txt`. The report now records:

- corpus manifest and per-file SHA-256 fingerprints
- per-baseline file coverage
- token and dollar savings under the same tokenizer
- win/tie/loss counts against the selector on files where the baseline is present
- baseline provenance for generated comparator outputs

That keeps baseline comparisons verifiable while leaving TOON/ONTO/other codec
tooling outside the conservative hook dependency set.

Prefer `--baseline-command NAME=COMMAND` when a comparator has a CLI, because
manual baseline folders are easy to stale. The command runs once per corpus
file, with `{input}` and `{output}` placeholders, and the generated files are
then treated exactly like `--baseline-dir` inputs. Example:

```sh
.venv/bin/python benchmark.py run \
  --corpus data/benchmark-corpus \
  --baseline-command 'toon=/opt/homebrew/bin/npm exec --yes --package @toon-format/toon@2.3.0 -- node scripts/toon_baseline.mjs --fallback-raw-on-fail {input} {output}' \
  --baseline-command 'onto=onto encode --input {input} --output {output}'
```

This is intentionally benchmark-only. A baseline should not enter the runtime
hook until it wins on token count, round-trip safety, deterministic behavior,
latency break-even, and model answer parity for the data family it targets.
Generated baseline directories include `baseline-provenance.json`, recording the
command template, rendered command per file, source/output hashes, and output
sizes. The benchmark report exposes the manifest path under
`baseline_provenance` so external baseline claims are auditable instead of
hand-waved.

For TOON specifically, [`scripts/toon_baseline.mjs`](scripts/toon_baseline.mjs)
is the benchmark-only bridge to the official `@toon-format/toon` package. It
does not enter the hook runtime. It parses the same supported source formats,
encodes with TOON, decodes the result, and refuses output if the parsed value
does not round-trip. Use `--fallback-raw-on-fail` for full-corpus coverage so a
TOON decoder failure is counted as raw fallback instead of disappearing from the
comparison.

The report also includes a candidate ablation table. This aggregates every
internal candidate across the corpus, recording coverage, win count, token
savings, average rank, and rank range. It is the selector-optimality evidence:
if a new candidate rarely wins or ranks poorly, it should not stay in the
runtime path without a clear safety or coverage reason.

## Current Local Result

The latest local downloaded-corpus report is
[`reports/benchmark-report.md`](reports/benchmark-report.md).

| Files | Raw tokens | Optimized tokens | Saved tokens | Savings | Saved / call | Saved / 100k calls |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 28 | 17,496,442 | 15,171,483 | 2,324,959 | 13.3% | $11.624795 | $1,162,479.50 |

Local processing time for that run:

| Load ms | Candidate ms | Token-count ms | Total ms | Saved tokens / ms |
| ---: | ---: | ---: | ---: | ---: |
| 333.8 | 5,760.1 | 41,342.4 | 47,436.9 | 49.0 |

By format:

| Format | Files | Raw tokens | Optimized tokens | Saved tokens | Savings |
| --- | ---: | ---: | ---: | ---: | ---: |
| CSV | 7 | 4,110,345 | 3,800,680 | 309,665 | 7.5% |
| JSON | 7 | 4,793,580 | 3,785,039 | 1,008,541 | 21.0% |
| JSONL | 7 | 4,478,352 | 3,785,039 | 693,313 | 15.5% |
| TSV | 7 | 4,114,165 | 3,800,725 | 313,440 | 7.6% |

By source dataset:

| Source | Files | Saved tokens | Savings | Winning formats |
| --- | ---: | ---: | ---: | --- |
| GitHub top repos | 4 | 239,352 | 29.1% | codebook-json x4 |
| Amazon reviews | 4 | 35,615 | 15.4% | raw x1, typed-csv x3 |
| GitHub notebook code/doc | 4 | 670,063 | 5.2% | raw x1, typed-csv x3 |
| LogHub 2.0 | 4 | 67,545 | 41.5% | codebook-json x4 |
| OpenAssistant | 4 | 494,177 | 22.0% | codebook-json x4 |
| SQuAD | 4 | 731,871 | 72.7% | codebook-json x4 |
| Titanic | 4 | 86,336 | 50.5% | codebook-json x4 |

Interpretation: pretty JSON and JSONL carry the largest avoidable overhead.
CSV/TSV should still no-op when they are already near-optimal, but JSON-native
codebooks can beat them when the tabular files contain heavily quoted repeated
nested cells.
The source rollup is intentionally included because the aggregate hides weak
slices: code/documentation records save only 5.2%, while QA/tabular and LogHub
2.0 sources save much more. Product claims should report this distribution
rather than only the overall 13.3%.

The latest refreshed report did not include an external baseline command. Use
the `--baseline-command` flow above before making TOON, ONTO, or other external
codec comparison claims.

The report now also records a latency break-even ceiling: the maximum
downstream input-token throughput where local preprocessing still pays off in
end-to-end latency. If you know the provider's approximate input throughput,
pass `--provider-input-tokens-per-second` to project whether token savings
outweigh local rewrite overhead on your stack.

The Codex `PreToolUse` runtime has the same economics guard as an opt-in
policy: set `CONTEXT_OPTIMIZER_PROVIDER_INPUT_TOKENS_PER_SECOND` to make
invisible rewrites no-op when local preprocessing is projected to cost more
latency than the saved input tokens recover. The default remains token-gated
only because provider throughput is deployment-specific.

## Remaining Publication Gap

The repository now has reproducible token and dollar-savings evidence plus
round-trip correctness tests. It also has a minimal optional Inspect AI smoke
eval in [`evals/`](evals/) so quality checks can use an existing eval framework
rather than a custom runner.

Before a model-quality run, generated eval datasets should pass the deterministic
dataset verifier:

```sh
.venv/bin/python evals/verify_context_quality_dataset.py \
  evals/context-quality.generated.jsonl
```

This gate recomputes exact targets from the source files, checks raw/optimized
pairing, and decodes the actual `Data:` block in each record so corrupted
optimized context cannot pass as evidence. It is weaker than model answer
parity, but it prevents target drift, missing paired variants, or broken
sidecar-context payloads from being mistaken for evidence.

It still needs a full task-level model-quality run before making a
top-paper-strength claim about accuracy preservation:

- lookup questions
- row retrieval
- aggregation
- schema reasoning
- nested-value recovery
- adversarial delimiters, nulls, missing keys, duplicates, and long text cells

Those evals should compare raw JSON, compact JSON, JSONL, CSV/TSV, TOON, and
this selector across several current model families. Summarize each Inspect log
with:

```sh
.venv/bin/python evals/summarize_context_quality.py \
  logs/path-to-run.eval \
  --json-out reports/context-quality-summary.json \
  --markdown-out reports/context-quality-summary.md \
  --fail-on-optimized-regression \
  --fail-on-missing-pairs \
  --min-optimized-accuracy 0.99 \
  --min-pairs 100
```

The important publication gate is paired parity: the accepted claim slice should
have no raw-correct/optimized-wrong regressions, and per-slice optimized
accuracy should be reported rather than hidden in one aggregate score. With the
flags above, `reports/context-quality-summary.json` contains a structured
`quality_gate` object and the command exits non-zero if the configured claim
bar is not met. The tracked Inspect smoke dataset proves the framework path; it
is not itself enough evidence for publication-grade accuracy preservation.
