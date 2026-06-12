# Quality Evals

Token and dollar savings are not enough for publication-grade evidence. Use
Inspect AI for model-quality checks instead of carrying a custom eval runner in
this repo.

Generate a paired raw/optimized dataset from the local benchmark corpus. This
does not call a model or require an API key:

```sh
.venv/bin/python evals/build_context_quality_dataset.py \
  --corpus data/benchmark-corpus \
  --out evals/context-quality.generated.jsonl \
  --model gpt-5.4-mini
```

Verify the generated pairs locally before spending model tokens:

```sh
.venv/bin/python evals/verify_context_quality_dataset.py \
  evals/context-quality.generated.jsonl
```

This verifier recomputes deterministic targets from the source files, checks
that every `(source_file, question_type)` has matching raw and optimized
variants, and decodes the actual `Data:` context embedded in each record before
checking that the answer is still derivable from that context. It does not prove
model answer parity, but it prevents target drift, missing pairs, or corrupted
optimized contexts from becoming evidence.

Install optional eval dependencies only when you have a free local model server
or API credentials for an actual model-quality run:

```sh
.venv/bin/python -m pip install -r requirements-eval.txt
```

Run the checked-in smoke dataset with a configured Inspect model:

```sh
.venv/bin/inspect eval evals/context_quality.py \
  --model openai/gpt-5.4-mini \
  --limit 2
```

Run the generated dataset by pointing `CONTEXT_QUALITY_DATASET` at the generated
file:

```sh
CONTEXT_QUALITY_DATASET=context-quality.generated.jsonl \
  .venv/bin/inspect eval evals/context_quality.py \
  --model openai/gpt-5.4-mini
```

Summarize the resulting Inspect log before treating it as evidence:

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

The summary reports total accuracy, raw versus optimized accuracy, accuracy by
question slice, and paired regressions where the raw context was answered
correctly but the optimized context was not. That paired regression count is the
main quality gate for the invisible replacement contract. When the gate flags
above are provided, the JSON summary includes a `quality_gate` object with
criteria, failures, and pass/fail status, and the command exits non-zero if the
claim slice does not meet the configured bar.

The tracked smoke dataset checks the same lookup question against raw JSON and
the optimized representation. It is deliberately small. The generated dataset
adds deterministic count, lookup, integer aggregation, repeated-value count,
null recovery, nested-value recovery, delimiter-adversary string recovery, and
missing-key existence, and first-row reconstruction tasks for supported
row-shaped corpus files. A paper-grade run should extend those slices further
and compare several model families.
