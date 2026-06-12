# 2025 to May 2026 Paper Sweep: Product Decisions for Token-Saving Context Compression

Date: 2026-05-21

Scope: papers from 2025-01-01 through 2026-05-21 that affect this repo's goal:
save input tokens for structured context without changing downstream answers.
I screened Hugging Face Papers, arXiv, Hugging Face docs, and the local
implementation. I included papers that are directly about prompt/context
compression, token-efficient serialization, tokenizer efficiency, structured
outputs, or table/structured-data reasoning. I excluded model-weight
compression, KV-cache-only work, visual-token-only work, and general serving
papers unless they changed a product decision here.

Current read: the product should not chase a universal compressed notation yet.
The best path is to keep the runtime hook deterministic and lossless, prove
answer parity by model family, and invest first in codebook/dictionary cases
where the newest papers and the local benchmark both show plausible upside.

## Deeper Recent Sweep: 2026-02-21 to 2026-05-21

The last three months sharpen the recommendation rather than reversing it. The
new papers most relevant to this repo are about deterministic dictionary-style
encodings, latency break-even measurement, columnar notation for structured
records, and exact value accuracy for structured outputs. Model-internal latent
compression continues to look promising academically, but it does not fit a
transparent Codex hook whose default contract is no material output change.

### Recent papers that should change product work

- [Lossless Prompt Compression via Dictionary-Encoding and In-Context Learning](https://arxiv.org/abs/2604.13066) (2026-03-19)
  - Replaces repeated subsequences with compact meta-tokens plus an in-context
    dictionary, explicitly optimizing for token savings after dictionary
    overhead. The paper reports high decompression fidelity on repetitive log
    data, but uses decompression as a proxy for downstream analytical parity.
  - Product implication: this is the closest match to the repo's codebook JSON
    direction. Keep dictionary/codebook candidates, but gate them by exact
    reconstruction and task-answer parity. Add log-like/repetitive datasets to
    the benchmark, because this paper's upside depends heavily on repetition.

- [Prompt Compression in the Wild](https://arxiv.org/abs/2604.02985) (2026-04-03)
  - Measures compression overhead, decoding latency, quality, memory, hardware,
    and rate adherence across many runs. Compression helps only inside a
    matched operating window; preprocessing can erase the win.
  - Product implication: add latency timing and break-even reporting before
    adding more encodings. A candidate that saves tokens but costs too much
    local preprocessing should not be selected by default.

- [ONTO: A Token-Efficient Columnar Notation for LLM Input Optimization](https://arxiv.org/abs/2604.17512) (2026-04-19)
  - Declares field names once and stores values in pipe-delimited rows,
    reporting large token reductions versus JSON on synthetic operational
    records with no material degradation on lookup/count/extraction/aggregation
    tasks.
  - Product implication: benchmark an ONTO-style external baseline against
    current typed/codebook candidates. Do not make it default until it beats
    compact JSON and current codebook output on both token savings and answer
    parity across real repo-shaped data.

- [The Structured Output Benchmark](https://arxiv.org/abs/2604.25359) (2026-04-28)
  - Shows schema compliance and exact value accuracy can diverge sharply,
    especially as context gets longer or source modality gets messier.
  - Product implication: score exact leaf-value accuracy in evals. A model
    producing valid JSON from optimized context is insufficient if values drift.

- [When Correct Isn't Usable](https://arxiv.org/abs/2605.02363) (2026-05-04)
  - Finds that constrained decoding can enforce syntax but add large latency
    overhead and sometimes reduce task performance.
  - Product implication: do not compensate for obscure custom encodings by
    layering heavy constrained decoding into the product. Prefer familiar
    encodings and compact decoder instructions that pass quality gates.

- [Compute Optimal Tokenization](https://arxiv.org/abs/2605.01188) (2026-05-02)
  - Treats compression rate as a first-class scaling variable and shows
    byte-level data size matters alongside token count.
  - Product implication: exact tokenizer counts remain required for cost and
    context-window decisions, but benchmark reports should keep byte size,
    tokenizer/model, latency, and quality together. Token savings alone are not
    enough evidence.

### Recent papers to monitor, not adopt by default

- [Large Language Model as Token Compressor and Decompressor](https://arxiv.org/abs/2603.25340) (2026-03-26, revised 2026-05-13)
  - Compresses long texts into learned latent Z-tokens with model adaptation.
  - Product implication: promising for future model-side infrastructure, but
    not safe for a local hook that sends plain text to black-box models.

- [Density-aware Soft Context Compression with Semi-Dynamic Compression Ratio](https://arxiv.org/abs/2603.25926) (2026-03-26)
  - Selects compression ratios based on information density for soft latent
    context compression.
  - Product implication: borrow the idea of data-dependent risk tiers and
    compression thresholds, not the soft-token mechanism.

- [Nacrith](https://arxiv.org/abs/2602.19626) (2026-02-23)
  - Neural lossless compression for storage/binary compression using a language
    model plus arithmetic coding.
  - Product implication: not useful as prompt text unless the downstream LLM can
    decode the binary representation. It should stay screened out of runtime
    hook candidates.

- [A Family of LLMs Liberated from Static Vocabularies](https://arxiv.org/abs/2603.15953) (2026-03-16)
  - Byte/word-level architecture work that reduces reliance on fixed
    tokenizers.
  - Product implication: reinforces model-specific measurement. It does not
    remove the need to count tokens for today's target model.

- [Learning is Forgetting](https://arxiv.org/abs/2604.07569) (2026-04-08)
  - Theoretical framing of training as lossy compression.
  - Product implication: useful background only. It should not change default
    implementation choices.

### Near-miss outside the strict window

- [Token-Oriented Object Notation vs JSON](https://arxiv.org/abs/2603.03306) (2026-02-08)
  - It falls just before the 2026-02-21 cutoff but remains directly relevant
    because it compares a token-oriented object notation against JSON.
  - Product implication: keep TOON in the external-baseline list, but treat it
    like ONTO: benchmark first, then adopt only where answer parity is proven.

### Recent-window decision changes

1. Prioritize a quality harness over new runtime encoders. The recent papers
   repeatedly show that equivalent information can produce different answers.
2. Add a dictionary/codebook benchmark slice for repetitive logs and telemetry.
   This is the recent paper cluster with the clearest potential upside for this
   repo.
3. Add latency break-even fields to every benchmark result before choosing
   heavier encodings.
4. Split candidate defaults by token-counter confidence: standard formats can
   use fallback estimates; custom codebook/ONTO-like formats require exact
   tokenizer counts and model-family quality proof.
5. Keep latent/soft-token compression out of the default product. It requires
   model adaptation or hidden representations and breaks the transparent file
   rewrite contract.

## Product Conclusion

The repo should stay a deterministic, lossless, tokenizer-measured format
selector. The current direction is stronger than adopting a lossy prompt
compressor by default, because the user-visible contract is "same answer, fewer
tokens", not "usually similar answer".

The largest product gap is proof of answer parity, not more encodings. Current
round-trip validation proves that a candidate can reconstruct the parsed source
value. It does not prove that the target LLM answers the same questions from
the alternate presentation. The 2025-2026 literature repeatedly shows that
format, table structure, prompt framing, and task type change LLM behavior even
when the data is equivalent.

Highest-value next moves:

1. Add a real quality gate before expanding default candidate formats.
   Generate Inspect AI tasks from the benchmark corpus for lookup, row
   retrieval, count, aggregation, schema reasoning, nested-value recovery,
   missing/null handling, duplicate entities, delimiter adversaries, and raw
   text preservation. Score exact leaf-value accuracy, reconstruction, and
   final answer parity. Compare raw, compact JSON, JSONL, CSV/TSV, current
   selector, dictionary/codebook candidates, TOON, and ONTO-style baselines.

2. Add a dictionary/codebook benchmark slice for repetitive operational data.
   `Lossless Prompt Compression via Dictionary-Encoding and In-Context
   Learning` is the newest directly actionable paper for this repo. Test
   repetitive logs, telemetry, repeated object keys, repeated enum strings, and
   delimiter-heavy records. Measure token savings after decoder/dictionary
   overhead, not candidate body size alone.

3. Add a latency break-even report to the benchmark. `Prompt Compression in the
   Wild` shows that compression only helps latency when preprocessing cost is
   outweighed by shorter model processing. This repo's hook is deterministic
   and local, so it has a good chance of staying below that break-even point,
   but the report should measure hook milliseconds, file size, selected format,
   raw tokens, optimized tokens, and estimated API-side savings.

4. Keep exact tokenizer counting as a hard product boundary. The tokenizer
   papers argue that tokenization is a structured compression layer with domain
   tradeoffs, so model-specific token counts are not optional. For non-OpenAI
   models, improve the `CONTEXT_OPTIMIZER_TOKENIZER_JSON` path with a small
   documented download helper. Do not trust the fallback estimator for risky
   custom formats.

5. Treat custom representations as risk-tiered. Compact JSON, JSONL, CSV, and
   TSV are familiar. Typed/codebook formats save real tokens in this corpus,
   but they are custom prompt languages. They should either pass the quality
   gate by model family or be behind an explicit experimental flag for contexts
   where answer parity matters.

6. Add raw-intent skip detection. If a prompt asks about exact bytes, line
   numbers, whitespace, syntax, quoting, delimiters, formatting, or "show the
   file as-is", the hook should no-op. `PreToolUse` already avoids semantic
   shell operations; `UserPromptSubmit` should get the same raw-intent guard.

7. Benchmark external baselines without absorbing their complexity into the
   hook. TOON and ONTO are important comparison points. The product should
   measure them and maybe call external encoders in the benchmark harness, but
   should not reimplement a broad notation unless it beats the current selector
   on both tokens and task accuracy.

## Direct Paper Set

### Prompt and context compression

- [Better Prompt Compression Without Multi-Layer Perceptrons](https://arxiv.org/abs/2501.06730) (2025-01-12)
  - Learned compression-token encoders can be much smaller than the original
    LLM architecture.
  - Product implication: useful research direction, but not deployable here
    without model adaptation. It is not a transparent hook-level transform.

- [DAST: Context-Aware Compression in LLMs via Dynamic Allocation of Soft Tokens](https://arxiv.org/abs/2502.11493) (2025-02-17)
  - Allocates soft tokens unevenly based on local/global information density.
  - Product implication: supports an "information density" lens for eval
    sampling, but it requires model internals and soft tokens, so it is not a
    default fit for a Codex hook.

- [CODEPROMPTZIP](https://arxiv.org/abs/2502.14925) (2025-02-19, revised 2026-04-09)
  - Code-specific prompt compression for RAG, using token-type priorities and a
    copy mechanism.
  - Product implication: do not apply natural-language compression to code or
    exact artifacts. If this repo expands beyond structured data, code needs a
    separate loss-aware strategy.

- [EFPC: Towards Efficient and Flexible Prompt Compression](https://arxiv.org/abs/2503.07956) (2025-03-11)
  - Combines task-aware and task-agnostic prompt compression with selective
    instruction prepending.
  - Product implication: task awareness matters. This repo should prefer
    task-level eval gates over one global savings threshold.

- [An Empirical Study on Prompt Compression for Large Language Models](https://arxiv.org/abs/2505.00019) (2025-04-24)
  - Compares six compression methods across 13 datasets and finds that long
    contexts are more affected than short contexts; moderate compression can
    help in some long-context tasks.
  - Product implication: add compression-ratio bands to quality reports. Do not
    infer quality from token savings alone.

- [Lossless Token Sequence Compression via Meta-Tokens](https://arxiv.org/abs/2506.00307) (2025-05-30)
  - LZ-style lossless token sequence compression reduces input length, but still
    leaves a performance gap versus uncompressed input in evaluated tasks.
  - Product implication: "lossless representation" is not the same as "same
    LLM behavior." This remains one of the strongest arguments for the quality
    gate.

- [SCOPE: A Generative Approach for LLM Prompt Compression](https://arxiv.org/abs/2508.15813) (2025-08-16)
  - Uses semantic chunking and concise rewriting instead of token deletion.
  - Product implication: relevant only as an optional lossy mode for natural
    language. It violates the current no-output-effect default contract.

- [DSPC: Dual-Stage Progressive Compression Framework for Efficient Long-Context Reasoning](https://arxiv.org/abs/2509.13723) (2025-09-17)
  - Training-free sentence filtering plus token pruning for long contexts.
  - Product implication: useful baseline for future natural-language context,
    but too lossy for structured data default behavior.

- [Sentence-Anchored Gist Compression for Long-Context LLMs](https://arxiv.org/abs/2511.08128) (2025-11-11)
  - Fine-tunes LLMs to compress context into learned gist tokens.
  - Product implication: model-level path, not a hook-level path.

- [Lossless Prompt Compression via Dictionary-Encoding and In-Context Learning](https://arxiv.org/abs/2604.13066) (2026-03-19)
  - Dictionary-encodes repeated subsequences and provides the dictionary in
    context so API-based LLMs can analyze compact representations without
    fine-tuning.
  - Product implication: directly supports keeping and evaluating codebook
    candidates, especially for logs and repeated operational records. Do not
    treat decompression fidelity alone as enough; add answer-parity evals.

- [Large Language Model as Token Compressor and Decompressor](https://arxiv.org/abs/2603.25340) (2026-03-26, revised 2026-05-13)
  - Uses learned latent codes to compress and decompress long texts.
  - Product implication: future research, but not safe for a transparent file
    rewrite hook.

- [Prompt Compression in the Wild](https://arxiv.org/abs/2604.02985) (2026-04-03)
  - Large-scale latency and quality study. Compression helps only in the right
    operating window; preprocessing can cancel gains.
  - Product implication: benchmark wall-clock overhead and break-even points,
    not just token deltas.

### Token-efficient structured serialization

- [Token-Oriented Object Notation vs JSON](https://arxiv.org/abs/2603.03306) (2026-02-08)
  - TOON can improve accuracy/token ratio in some generation tasks, but prompt
    overhead and task size matter. Plain JSON can still win on accuracy.
  - Product implication: TOON belongs in the benchmark as an external baseline,
    not as an assumed universal replacement.

- [ONTO: A Token-Efficient Columnar Notation for LLM Input Optimization](https://arxiv.org/abs/2604.17512) (2026-04-19)
  - Declares fields once and arranges values in rows, reporting 46-51% token
    reduction versus JSON on synthetic operational datasets with no material
    degradation in lookup/count/extraction/aggregation tasks.
  - Product implication: closest paper to the repo's typed/columnar direction.
    Benchmark an ONTO-style candidate, especially for nested operational
    records, but require answer-parity proof before default use.

- [Accept or Deny? Table-to-Text Serialization Approaches](https://arxiv.org/abs/2508.21512) (2025-08-29)
  - Serialization format changes both performance and fairness in loan approval
    tasks.
  - Product implication: format choice is a behavioral intervention. For high
    stakes data, no optimization should run without task-specific evals.

### Structured output and format reliability

- [JSONSchemaBench](https://arxiv.org/abs/2501.10868) (2025-01-18, revised 2025-02-27)
  - Evaluates constrained decoding over 10K real-world JSON schemas across
    compliance, coverage, and quality.
  - Product implication: if the hook ever asks the model to emit structured
    answers from optimized context, schema compliance alone is not enough; value
    correctness must be measured.

- [StructEval](https://arxiv.org/abs/2505.20139) (2025-05-26, revised 2026-04-02)
  - Benchmarks generation and conversion across 18 formats including JSON,
    YAML, CSV, HTML, React, and SVG.
  - Product implication: add conversion/reconstruction tasks to evals. A model
    should be able to recover the original JSON value from the selected format.

- [SO-Bench](https://arxiv.org/abs/2511.21750) (2025-11-23, revised 2026-03-17)
  - Shows schema-grounded extraction remains hard across visual domains.
  - Product implication: lower direct relevance for text-only hooks, but it
    reinforces that schema compliance and correct values diverge.

- [The Structured Output Benchmark](https://arxiv.org/abs/2604.25359) (2026-04-28)
  - Multi-source benchmark where models often satisfy schemas but miss exact
    values, especially as context gets longer.
  - Product implication: the hook's quality suite should score exact leaf-value
    accuracy, not only final free-form answer quality.

- [When Correct Isn't Usable](https://arxiv.org/abs/2605.02363) (2026-05-04)
  - Shows a gap between task correctness and JSON-format compliance; constrained
    decoding can add large latency overhead and sometimes hurt task performance.
  - Product implication: avoid adding heavy constrained decoding to compensate
    for obscure encodings. Prefer familiar encodings plus eval-proven decoder
    instructions.

### Table and structured-data reasoning

- [HiBench](https://arxiv.org/abs/2503.00912) (2025-03-02)
  - Evaluates hierarchical structure reasoning over 30 tasks.
  - Product implication: add hierarchical/nested JSON tasks before enabling
    aggressive nested encodings.

- [How well do LLMs reason over tabular data, really?](https://arxiv.org/abs/2505.07453) (2025-05-12, revised 2025-11-04)
  - Shows tabular reasoning is sensitive to missing values, duplicates, and
    structural variations.
  - Product implication: exact evals need missing/null distinction, duplicate
    rows/entities, and structural variation cases.

- [TableEval](https://arxiv.org/abs/2506.03949) (2025-06-04, revised 2025-09-21)
  - Real-world TableQA across concise, hierarchical, and nested tables in
    multiple languages/domains.
  - Product implication: test more than flat object arrays. Include nested and
    hierarchical source shapes.

- [MMTU](https://arxiv.org/abs/2506.05587) (2025-06-05, revised 2026-03-09)
  - Over 28K questions across 25 table tasks; even frontier models leave
    substantial room for improvement.
  - Product implication: "table" is a broad task family. Keep candidate
    selection conservative unless the prompt task is known.

- [RealHiTBench](https://arxiv.org/abs/2506.13405) (2025-06-16, revised 2025-12-14)
  - Hierarchical table benchmark over LaTeX, HTML, and PNG, plus a tree-based
    reasoning pipeline.
  - Product implication: hierarchy-preserving representations may matter more
    than token count for complex tables.

- [TReB](https://arxiv.org/abs/2506.18421) (2025-06-23, revised 2025-07-14)
  - Evaluates shallow and deep table reasoning across 26 sub-tasks.
  - Product implication: use sub-task buckets in the eval report, not one
    aggregate accuracy number.

- [Tabular Data Understanding with LLMs: A Survey](https://arxiv.org/abs/2508.00217) (2025-07-31)
  - Taxonomy of tabular input representations and tasks; highlights gaps around
    complex structures, large tables, long context, and multi-table scenarios.
  - Product implication: benchmark multi-table and large-table cases before
    broadening file-type support.

- [T2R-bench](https://arxiv.org/abs/2508.19813) (2025-08-27, revised 2025-09-23)
  - Table-to-report benchmark over industrial tables.
  - Product implication: report-generation tasks are higher risk for compact
    encodings than exact lookup tasks.

- [ReasonTabQA](https://arxiv.org/abs/2601.07280) (2026-01-12)
  - Industrial TableQA with multi-table structures, nested headers, and
    reasoning chains.
  - Product implication: nested headers and multi-table relationships are a
    separate capability. Avoid flattening them unless round-trip and answer
    parity are both proven.

- [ModelTables](https://arxiv.org/abs/2512.16106) (2025-12-18)
  - 90K tables from Hugging Face model cards, GitHub READMEs, and papers.
  - Product implication: a good future benchmark source for real technical
    tables because it matches developer context better than toy CSVs.

### Tokenizer and tokenization efficiency

- [zip2zip](https://arxiv.org/abs/2506.01084) (2025-06-01, revised 2025-10-24)
  - Inference-time adaptive tokenization with dynamically created hypertokens,
    reducing input/output tokens by 15-40% after uptraining.
  - Product implication: confirms tokenization can be adapted, but not available
    to a black-box Codex hook.

- [Information Capacity](https://arxiv.org/abs/2511.08066) (2025-11-11, revised 2026-03-10)
  - Efficiency metric that incorporates tokenizer efficiency across models.
  - Product implication: report model/tokenizer in every benchmark result.

- [MUTANT](https://arxiv.org/abs/2511.03237) (2025-11-05, revised 2026-03-22)
  - Multilingual tokenizer design method.
  - Product implication: non-English and multilingual files need model-specific
    tokenizers; English-heavy fallback estimates are not enough.

- [An Information-Theoretic Perspective on LLM Tokenizers](https://arxiv.org/abs/2601.09039) (2026-01-14)
  - Tokenizers are structured compressors with tradeoffs between compression,
    induced statistical structure, and robustness under domain shift.
  - Product implication: exact token counting per active tokenizer should stay
    a core contract.

- [Compute Optimal Tokenization](https://arxiv.org/abs/2605.01188) (2026-05-02)
  - Studies compression rate as an independent factor in scaling laws and
    argues compute-optimal behavior depends on bytes, not just token counts.
  - Product implication: token count is the billing and context-window target,
    but quality and compute behavior still need measured validation.

## Papers Screened But Not Product-Default Inputs

These are related but should not drive the default hook because they require
model internals, model training, non-text modalities, or output-side changes:
vision-centric token compression, GlobalCom2, KV-cache compression, binary
compressors such as Nacrith, model compression surveys, speculative decoding,
prompt parallelism, and general LLM-as-compressor data-compression papers.

## Recommended Implementation Plan

1. Add `evals/build_context_quality_dataset.py`.
   It should read benchmark corpus files, call the current candidate selector,
   and generate paired raw/optimized Inspect records for exact-answer tasks.
   Include exact leaf-value checks and reconstruction tasks, not only
   free-form answer comparison.

2. Expand `evals/context_quality.py` from a two-row smoke test into named
   slices: lookup, row retrieval, count, aggregation, missing/null, duplicates,
   nested, delimiter adversary, reconstruction, and repetitive log/codebook.

3. Add a dictionary/codebook-specific benchmark slice:
   repetitive logs, repeated telemetry records, repeated object keys, repeated
   enum strings, and adversarial repeated delimiters. Score raw-answer parity,
   reconstruction, and token savings after decoder/dictionary overhead.

4. Extend `benchmark.py run` with `--latency` or always-on timing fields:
   load milliseconds, candidate generation milliseconds, token-count
   milliseconds, write milliseconds, total hook milliseconds, and break-even
   tokens saved per millisecond.

5. Add optional external baseline hooks to the benchmark only:
   `--baseline-toon-command` and `--baseline-onto-command`. Keep runtime hook
   dependency-free unless a baseline clearly wins on both tokens and quality.

6. Add an explicit candidate risk tier:
   `standard`: raw, compact JSON, JSONL, CSV, TSV.
   `custom`: typed CSV/TSV, codebook row, typed codebook row, ONTO-like.
   Default to `standard` for fallback token counters or unproven model families.

7. Add a tokenizer setup helper or docs section:
   `python scripts/download_tokenizer_json.py Qwen/Qwen2.5-7B-Instruct`.
   This can use `huggingface_hub` or document `hf download ... tokenizer.json`.
   The local machine currently did not have `hf` on PATH during this sweep.

8. Add `UserPromptSubmit` raw-intent skip terms:
   exact bytes, verbatim, original formatting, whitespace, line number, raw
   text, show file, quote the file, delimiter, comma, tab, JSON syntax.

## Current Local Evidence Rechecked

- Unit tests: `.venv/bin/python -m unittest discover -s tests` passed, 13 tests.
- Benchmark rerun against `data/benchmark-corpus` completed and wrote
  `reports/benchmark-report.json` and `reports/benchmark-report.md`.
- The latest local benchmark shows 24 files, 17,333,717 raw tokens,
  15,067,226 optimized tokens, and 2,266,491 tokens saved on `gpt-5.4-mini` with
  `tiktoken`.
