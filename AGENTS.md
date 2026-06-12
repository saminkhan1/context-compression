# AGENTS.md

## Repository Expectations

- This repository is a deterministic, lossless structured-context selector with adapters for Codex, Pi, Hermes Agent, OpenClaw, MCP, and generic agent runtimes.
- Treat `selector.py` as the reusable selector surface and `hook.py` as the Codex runtime hook surface. `hook.py` handles `UserPromptSubmit` and `PreToolUse` payloads and must remain conservative.
- Treat `benchmark.py`, `reports/`, `EVIDENCE.md`, and `PAPER_SWEEP_2025_2026.md` as evidence and research surfaces. Do not change benchmark claims without updating the matching evidence.
- Treat `evals/` as the optional Inspect AI quality-check path, not as hook runtime code.
- Do not commit generated local artifacts from `.codex/context-cache/`, `data/benchmark-corpus/`, `logs/`, `.venv/`, or Python cache directories.

## Runtime Contract

- Preserve source files. The selector and adapters may create optimized sidecar files, but they must not rewrite the referenced data files.
- Keep compression lossless. Every non-raw candidate must decode back to the parsed source value before it can be selected.
- Keep selection deterministic for a fixed input file, model profile, tokenizer, and candidate set.
- Keep semantic file operations raw. Commands such as `jq`, `grep`, `sed`, `head`, pipes, `cat -n`, mixed unsupported files, and failed savings gates should no-op.
- Supported source formats are JSON, JSONL, CSV, and TSV unless the README and tests are updated with a new supported format.
- For exact token claims, use `tiktoken` for OpenAI models or a configured Hugging Face `tokenizer.json`. If the deterministic fallback counter is used, label counts as estimated.

## Setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x run-hook.sh
```

Install optional eval dependencies only when running quality evals:

```sh
.venv/bin/python -m pip install -r requirements-eval.txt
```

## Verification

- Run the unit suite after changing hook behavior, candidate encoding/decoding, model-profile resolution, or Bash rewrite logic:

```sh
.venv/bin/python -m unittest discover -s tests
```

- Run the manual hook smoke when changing `PreToolUse` command rewriting:

```sh
printf '%s\n' '{"hook_event_name":"PreToolUse","cwd":"'"$PWD"'","model":"gpt-5.4-mini","tool_name":"Bash","tool_input":{"command":"cat sample-repetitive.json"}}' \
  | ./run-hook.sh
```

- Run the benchmark path only when changing benchmark code or updating evidence:

```sh
.venv/bin/python benchmark.py all \
  --rows 1000 \
  --out data/benchmark-corpus \
  --corpus data/benchmark-corpus \
  --input-price-per-1m 5 \
  --monthly-calls 100000
```

- Run the optional Inspect AI smoke only when touching model-quality evals or quality claims:

```sh
.venv/bin/inspect eval evals/context_quality.py \
  --model openai/gpt-5.4-mini \
  --limit 2
```

## Change Guidelines

- Prefer Python standard-library parsing and serialization over ad hoc text manipulation for data formats.
- Keep new dependencies out of runtime unless they are clearly justified and added to `requirements.txt`.
- Keep tests focused on round-trip safety, deterministic selection, exact no-op boundaries, and concrete token-saving regressions.
- When adding a candidate representation, add tests that prove both successful round-trip behavior and refusal of lossy/non-uniform cases.
- When changing documented token or cost numbers, regenerate the relevant report or state clearly that the number is not refreshed.
- If verification cannot run because optional credentials, network access, or local dependencies are missing, report that explicitly.
