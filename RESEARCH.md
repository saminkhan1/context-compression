# Research Notes

This project is based on a conservative reading of prompt compression and
structured-data research: lower token count helps cost and latency, but the
format must remain recognizable and structurally faithful enough for the target
model.

## Findings

### Prompt compression is useful, but not free

- [LLMLingua](https://hf.co/papers/2310.05736) reports up to 20x prompt
  compression with little performance loss by using a budget controller,
  token-level compression, and distribution alignment.
- [LongLLMLingua](https://hf.co/papers/2310.06839) reports lower cost and
  latency in long-context settings, and in some QA settings even improved
  performance because compression concentrates key information.
- [An Empirical Study on Prompt Compression](https://hf.co/papers/2505.00019)
  evaluates multiple compression methods and finds the impact is task- and
  context-length-dependent. Moderate compression can help long-context tasks,
  but aggressive compression can hurt quality.

Implementation consequence: this hook does not blindly minify everything. It
requires a savings threshold and keeps raw data available.

### Structured format choice changes model behavior

- [Table Meets LLM](https://hf.co/papers/2305.13062) finds that table input
  format, content order, role prompting, and partition marks affect tabular
  reasoning performance.
- [StructEval](https://hf.co/papers/2505.20139) evaluates JSON, YAML, CSV, and
  other structured formats and shows that format adherence and structural
  correctness remain nontrivial even for strong models.
- [StrucText-Eval](https://hf.co/papers/2406.10621) shows that reasoning over
  structure-rich text gets much harder as nesting and structural width grow.
- [POML](https://hf.co/papers/2508.13948) frames prompt construction as a
  presentation-sensitive problem and argues for separating logical content from
  presentation.

Implementation consequence: candidates include compact JSON, tabular formats,
and dictionary-coded variants. The selector is per file and per tokenizer rather
than hardcoding one format.

### Tokenization is part of the physical limit

- [An Information-Theoretic Perspective on LLM Tokenizers](https://hf.co/papers/2601.09039)
  treats tokenizers as structured compressors and shows trade-offs among
  compression efficiency, induced statistical structure, and robustness under
  domain shift.
- [Exact Byte-Level Probabilities from Tokenized Language Models](https://hf.co/papers/2410.09303)
  highlights tokenization bias and shows that token boundaries can affect model
  behavior.

Implementation consequence: when `tiktoken` or a Hugging Face `tokenizer.json`
is available, this hook counts exact tokenizer tokens. It only reports
`Estimated tokens` when forced onto the deterministic fallback counter.

### TOON is one candidate, not the answer

[TOON](https://github.com/toon-format/toon) is a good human-readable
representation for many object arrays, especially when repeated object keys
dominate. Its own README notes that compact JSON can win for small or deeply
nested data and CSV can be better for uniform flat tables.

Implementation consequence: TOON is treated as an external baseline instead of
being reimplemented here. The local selector stays smaller and focuses on
stdlib-backed compact JSON, columnar/codebook JSON, CSV/TSV, and typed CSV/TSV
for offline evaluation. In this repo:

- `sample-repetitive.json`: default safe-tier `column-json`, `1419` exact
  tokenizer tokens vs raw `4102` on `gpt-5.5`.
- Hugging Face `julien-c/titanic-survival` JSON fixture: `codebook-json`, `20791`
  exact tokenizer tokens vs raw `71983` on `gpt-5.5`.

### Adjacent projects already exist

This repo should not present the idea as novel in isolation.

- [microsoft/LLMLingua](https://github.com/microsoft/LLMLingua) is the mature
  semantic prompt-compression baseline. It is not lossless, but it is the right
  comparison point for natural-language prompt compression.
- [toon-format/toon](https://github.com/toon-format/toon) is the mature
  structured-data format baseline. Its public benchmarks report roughly 40%
  token reduction on mixed structured retrieval tasks, and the README explicitly
  calls out cases where compact JSON or CSV can win.
- [ONTO](https://github.com/harsh-aranga/onto) is a newer columnar notation for
  LLM input optimization, reporting 46-51% token reduction versus JSON on
  operational datasets.
- [TONL](https://github.com/tonl-dev/tonl) is another structured token-efficient
  notation/runtime aiming at practical JSON replacement workflows.
- `leanctx`, `llmlingua-2-js`, and other prompt-compression libraries show that
  cost-saving wrappers are already appearing around production LLM pipelines.

Implementation consequence: the defensible product surface is not "new compact
notation." It is a general-purpose deterministic selector for AI agents
that measures the active tokenizer, validates round-trip safety, and no-ops
when the source format is already best.

### Evidence must be corpus-scale

The single Titanic fixture is useful as a regression test, not as proof of
mainstream adoption value. The benchmark harness now builds a local corpus from
several public data sources and materializes JSON, JSONL, CSV, and TSV variants.

Current local run:

- 24 downloaded files
- 17.3M raw `gpt-5.5` tokenizer tokens
- 2.27M input tokens saved
- JSON savings: 20.8%
- JSONL savings: 15.3%
- CSV/TSV savings: 7.3-7.4%, mostly when JSON-native column/codebook
  candidates beat tabular source files with heavily quoted nested cells

Implementation consequence: claims should be format-conditional. The hook is
highest-value for verbose structured JSON and JSONL; it should still no-op for
already-optimal tabular files. Benchmark reports should also include a latency
break-even view instead of assuming every saved token becomes a real-world
speed-up.

## Open Research Risk

The hook optimizes for exact tokenizer-token count inside a lossless candidate
set. That is necessary for cost and context pressure, but it is not a full
accuracy guarantee. A production-grade version should add task-level evals:

- lookup questions
- row retrieval
- aggregation
- schema reasoning
- nested JSON reconstruction
- adversarial strings containing delimiters and null markers

Until then, the implementation stays conservative: whole-file context reads can
be redirected, while semantic commands keep the raw file.
