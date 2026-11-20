# Findings: Re-examining Structured Context Compression (Nov 2026)

## Summary

1. **Headline (system-level, fresh untuned seed 777).** This compares a *code-executing
   reader* against *in-context formats*. That's a different system class from what TOON's
   benchmark was built to compare, and it's stated up front. On the benchmark's own
   questions and grader, a single Haiku turn sees a schema preview, writes a short script,
   and the harness runs it against the file. Counting **every billed token** (input and
   output, all turns, CLI overhead included):

   | Regime | Code-from-preview | TOON in context | Fewer billed tokens |
   | --- | --- | --- | --- |
   | Benchmark accuracy datasets (≤100 rows, 239 Q) | **97.5%**, 1,230 tok/Q | 64.9%, 5,410 tok/Q (80/2, p = 1e-21) | **4.4×** (3.8× vs cheapest lossless inline) |
   | Benchmark token-efficiency datasets (100–2,000 rows; all 239 Q for every arm) | **98.3%**, 1,236 tok/Q | 61.5%, 51,631 tok/Q (89/1, p = 1.5e-25) | **41.8×** (bootstrap 95%: 36.5–47.4×) |

   **Correction.** An earlier version reported this regime from 32-question baseline
   samples (TOON 68.8%, plain agent 93.8%, 14.8×, code 100%). Running every question
   gives the numbers here: the token ratios hold, and the accuracy gaps moved.

   **Against a realistic agent baseline** (all 239 large-regime questions): a plain
   tool-using agent with Bash (python3, jq, cat, head, grep) and no preview scored 95.0%
   at **18,981** billed tok/Q over 3.5 turns. Code-from-preview scored 98.3% at **1,236**:
   **15.4× fewer tokens** (bootstrap 95%: 14.7–16.1×), accuracy 98.3% vs 95.0% (9/1
   discordant, p = 0.02). Five of the agent's 12 misses were chatty answers ("Perfect! I
   found...") the strict grader rejects, so a stricter prompt would likely close most of the
   accuracy gap; the token saving is the robust result. The 15.4× is the number to quote to
   a skeptic: it isolates what the preview and single-turn design add over just letting an
   agent explore.

**Where 10x holds** (seed 777, billed tokens, TOON ÷ code), by dataset size:

   | Records | Dataset | Reduction |
   | --- | --- | --- |
   | 6 | nested config | 0.9–1.0× |
   | 100 | github | 8.4× |
   | 365 | analytics | 9.2× |
   | 500–2,000 | keyed, nested orders, nested contacts, tabular, event logs | 11–139× |

   So 10x holds once a dataset passes roughly 400 records. Below that, inline is the right
   call: the selector's lossless views.

   **Disclosure:** the github dataset is a static snapshot, so its 33 questions are
   identical on every seed, and one dev-seed fix ("keep decimals") came partly from a
   github question. Excluding github: small regime 99.0% vs 67.5%, **3.8×**; large regime
   **47.2×**.

   The cost of this path is flat in data size; inline cost grows linearly. Models can't
   count or filter thousands of rows in their heads, and a 6-line script can. The idea
   (program-aided answering over tables) is known. What's new here is the controlled
   measurement against the format SOTA. The earlier multi-turn agent arm (Bash tool, 97.1%
   on seed 12345) was correct but billed *more* than TOON on small data, because of the
   tool schema and a second turn. The single-turn variant removes that overhead.
2. **New lossless encoding (`ctab`), best token efficiency on the benchmark's datasets.**
   It cuts **27% of tokens vs TOON and 33% vs compact JSON** across TOON's own
   token-efficiency datasets. The lead holds on o200k and cl100k, and it holds with the
   primer *included*, which TOON's published counts leave out. Information density is 6.2
   xz-bits/token, against 4.5 for TOON and 4.1 for JSON. It passed a 100k-document
   round-trip fuzz with zero failures.
3. **On accuracy, ctab matches JSON at half the tokens but does *not* beat TOON.** This is
   a negative result, stated plainly. v1 tied JSON (10/11 discordant, p = 1.0) and lost to
   TOON (4/18, p = 0.004). v2 was tuned on dev seed 101, where it reached non-significance
   (62.3% vs 64.0%, 4/8, p = 0.39). On the official seed 12345 (not truly held-out: v2 was designed from v1's error list on it), v2 scores **61.9% vs
   TOON 66.1% (5/15, p = 0.041)**. It still ties JSON (10/7, p = 0.63) and has the best
   accuracy per data token of any inline format (17.7 vs 15.9 for TOON per 1k tokens).
   Field retrieval is 100%. The gap sits in "structure-awareness" questions, which the TOON
   authors describe as testing *TOON's* syntax ("What is the 5th field name?"), plus
   counting noise. When accuracy on inline data matters more than the last ~16% of tokens,
   TOON-style layout is the better choice. When the data is large, neither format is: use
   the tool path.
4. **The repo's flagship candidate hurt answers.** `codebook-json` (dictionary indirection)
   scored 51.9%, the lowest of any arm, with field lookups at 84% vs 100% for JSON. It has
   been moved to an `experimental` tier.
5. **The transparent-rewrite hook made coding agents write broken code.** The safe tier
   replaced a JSON array with a `column-json` view. Haiku then wrote a loader for
   `[columns, rows]` that crashes on the real file. Labelling the view with the on-disk
   format fixed it. That's n = 1 per arm, so it's illustrative, not statistical, but the
   failure mode is structural. All non-raw views now carry that label.

## 1. Questioning the premise: why would a harness do this at all?

| Assumption in the original repo | What the evidence says |
| --- | --- |
| Agents `cat` large structured files whole | Real harnesses truncate (Claude Code `Read` caps at 2,000 lines), and agents reach for `jq`/`grep`/`head`. Those are exactly the calls the hook skips. Whole-file reads that the hook can rewrite are mostly *small* files, where savings are small. |
| Fewer tokens means cheaper | Prompt caching bills re-reads of an unchanged prefix at about 0.1×. Savings still scale, but the first-read win dominates. Deterministic at-ingest encoding keeps the cache prefix stable, which matters: history-rewriting policies can lose that ("Beyond Token Savings", arXiv 2609.32961). |
| Lossless means safe | Lossless is necessary, not sufficient. Equivalent data in a different shape changed answers (codebook −8.8 pp vs JSON), and a mislabelled view changed the *code* the agent wrote. |
| Format choice is the main lever | Every inline format sits within 14 pp on the same questions. The harness choice (inline vs. preview + query tool) moved accuracy 31 pp. Environment observations were about 84% of SWE-agent turn tokens in one observation-masking study (seen via a secondary source), so tool outputs, not file reads, are likely the larger target. |
| A 13.3% aggregate saving is the headline | That figure is dominated by one 3M-token code/doc corpus where typed-csv barely helps, and its biggest wins came from the candidate that turned out to hurt accuracy. |

The defensible product is a *context policy*, not a notation:

```text
small structured payload (<~2k tokens)  -> inline as-is (headers/primers don't pay for themselves)
medium payload                           -> inline best lossless view (ctab / csv / compact JSON),
                                            labelled with the on-disk format
large payload, or aggregation/filtering  -> schema + 2-row preview in context, data on disk,
                                            let the model query it (python/jq/SQL)
```

## 2. Benchmark and method

- **Benchmark:** the official [TOON retrieval benchmark](https://github.com/toon-format/toon/tree/main/benchmarks):
  seeded faker datasets, programmatic questions, and a deterministic type-aware grader
  (no LLM judge). We reuse its dataset, question, and grader code unchanged
  (`bench/export.ts`, `bench/grade.ts`). The only patch makes the faker seed configurable
  for dev/test splits.
- **Excluded:** the 5 structural-validation questions, which corrupt each format's
  rendered text with format-specific code. That leaves 239 of 244 questions.
- **Model:** `claude-haiku-4-5`, one of the four models in TOON's published table, called
  through `claude -p` with thinking disabled (verified `thinking_tokens: 0`), no tools for
  inline arms, and a fixed ~670-token CLI overhead that cancels in comparisons. Prompt
  template copied from TOON's `evaluate.ts`.
- **Statistics:** one run per arm (compute-limited), paired exact McNemar per question,
  Wilson 95% intervals. Format iteration used dev seed 101, but the v1 to v2 changes were motivated by v1's
  errors on seed 12345, so seed 12345 is not a clean held-out set for v2.
- **Reproduction check:** published Haiku numbers are TOON 65.6% / JSON 63.5% (244 Qs). We
  measure 66.1% / 60.7% on 239 Qs, so the harness reproduces the published cells within noise.

## 3. Results

### Token efficiency (no LLM calls; TOON token-efficiency datasets, seed 12345)

| Dataset | JSON pretty | JSON compact | TOON | CSV | original repo best | **ctab v2** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| tabular (2000) | 127,353 | 79,349 | 50,225 | 47,402 | 47,368 | **46,225** |
| nested orders (500) | 110,425 | 69,990 | 73,550 | – | 59,377 | **56,589** |
| analytics (365) | 22,250 | 14,216 | 9,120 | 8,388 | **8,212** | 8,390 |
| github (100) | 15,337 | 11,640 | 8,937 | 8,711 | **8,662** | 8,713 |
| event logs (2000, semi-uniform) | 181,215 | 128,530 | 154,090 | – | 128,528 | **88,093** |
| nested config | 905 | **551** | 584 | – | 551 | 557 |
| keyed flags (500) | 23,193 | 15,687 | 10,549 | – | 15,687 | **10,041** |
| nested contacts (1000) | 80,006 | 47,018 | 26,991 | – | 31,092 | **26,340** |
| **Total** | 560,684 | 366,981 | 334,046 | – | 299,477 | **244,948** |

The total is o200k_base; cl100k_base gives the same ranking (ctab 247k vs TOON 333k). The
updated selector (`hook.py`, advanced tier, view header and primer counted) totals
**246,038**, or −26.3% vs TOON. Per-dataset `min()` selection means it never does worse
than compact JSON.

**Information density** (LTCB lens: count the "decompressor"; xz size is only an upper
bound on information content, so read this as a practical yardstick, not a ceiling). The yardstick is
xz-extreme-compressed bits of the data divided by tokens, with ctab's 104-token primer
included: JSON compact 4.11, TOON 4.52, **ctab 6.18 bits/token**.

### Retrieval accuracy (claude-haiku-4-5, official seed 12345, 239 questions)

| Arm | Accuracy (95% CI) | Data tokens / Q (o200k) | Acc. per 1k data tokens | Field retrieval | Aggregation | Filtering | Structure |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| JSON pretty | 60.7% [54.4, 66.6] | 6,964 | 8.7 | 100.0 | 28.6 | 12.5 | 80.6 |
| TOON | 66.1% [59.9, 71.8] | 4,154 | 15.9 | 97.8 | 42.9 | 14.6 | 94.4 |
| codebook-json (original repo, flat sets only, n=133) | 51.9% [43.5, 60.2] | 5,103 | 10.2 | 84.3 | 18.8 | 3.7 | 82.6 |
| ctab v1 (encoder in commit ac1468d, `bench/ctab_v1.py`) | 60.3% [53.9, 66.2] | 3,474 | 17.3 | 97.8 | 34.9 | 12.5 | 72.2 |
| ctab v2 (seed 12345; v2 was designed from v1's errors on this seed, so not truly held-out) | 61.9% [55.6, 67.8] | 3,503 | 17.7 | 100.0 | 33.3 | 14.6 | 77.8 |
| schema preview + Bash/python tool, 2 turns (billed: 10,341 input tok/Q, more than TOON's 5,418) | 97.1% [94.1, 98.6] | 232 data / 10,341 billed | 418.7 data-only | 98.9 | 96.8 | 100.0 | 88.9 |

Paired McNemar (A-only / B-only correct):

- tool vs TOON 77/3, p = 1.4e-19
- TOON vs JSON 16/3, p = 0.004
- ctab v1 vs JSON 10/11, p = 1.0
- ctab v1 vs TOON 4/18, p = 0.004
- codebook vs TOON 2/16, p = 0.001
- ctab v2 vs JSON 10/7, p = 0.63
- ctab v2 vs TOON 5/15, p = 0.041

**Billed cost caveat for the tool arm.** On these small datasets (≤100 rows), the tool arm
used *more* total input tokens per question: 10.3k vs 5.4k for TOON, at $0.0118 vs $0.0094.
That's because of the Bash tool schema and a second turn (2.0 turns on average, 156 output
tokens). The preview stays ~100–330 tokens regardless of size, while inline cost grows
linearly: TOON is 50k–154k tokens for the 2,000-row datasets. The crossover is a few
hundred rows. Agent harnesses already pay the tool-schema cost, so in practice the
marginal cost is the preview plus tool I/O.

### Dev seed 101 (used to tune ctab v1 → v2)

ctab v2 62.3% vs TOON 64.0% (4/8, p = 0.39). The two arms tie on field retrieval (100/100),
aggregation (34.9/34.9), and filtering (12.5/12.5). The gap is structure-awareness,
80.6 vs 91.7.

v1 → v2 changes, from the official-seed v1 error analysis:

- grouped headers `customer{id,name}` instead of dotted names
- keyed maps as `key: values` rows
- redundant, decoder-verified `#items` list-length columns, so the model reads counts
  instead of counting

### Probe: transparent rewrite vs. code generation (n = 1 per arm)

| What the model saw for `employees.json` | Generated `get_email()` on the real file |
| --- | --- |
| raw pretty JSON (1,886 tok) | correct |
| original hook's safe-tier sidecar, `column-json` (708 tok) | **crashes**: indexes `data[0]` as column list |
| ctab view with on-disk-format header | correct |

## 4. Related research

- **Serialization formats:** TOON's benchmark is self-published; its token savings
  replicate, but its accuracy edge varies by method. [Notation Matters (arXiv 2605.29676)](https://arxiv.org/abs/2605.29676)
  tested TOON and TRON in agentic tool loops and found up to −9 pp for TOON and
  multi-turn degradation. [ONTO (arXiv 2604.17512)](https://arxiv.org/abs/2604.17512)
  reports 46–51% savings on synthetic records, with counting at 15–40% in *every* format.
  That's consistent with our finding that counting is a model limitation, not a format one.
  [TQA-Bench](https://arxiv.org/abs/2411.19504) found JSON worst for multi-table QA.
- **Dictionary/lossless prompt compression:** [Dictionary-Encoding ICL (arXiv 2604.13066)](https://arxiv.org/abs/2604.13066)
  verifies decompression fidelity, not task accuracy. Our codebook result shows why that
  proxy is insufficient.
- **Agent context management:** [Beyond Token Savings (arXiv 2609.32961)](https://arxiv.org/abs/2609.32961)
  ran about 35k runs on SWE-bench Verified and Terminal-Bench. Compression used 0.4–0.7×
  the tokens with success between −11 and +0.7 pp, and history rewriting can invalidate
  cache prefixes. [SWE-Pruner (2601.16746)](https://arxiv.org/abs/2601.16746),
  [Squeez (2604.04979)](https://arxiv.org/abs/2604.04979), and [CoACT (2607.02911)](https://arxiv.org/abs/2607.02911)
  all prune *tool observations*, which supports the view that tool outputs are the bigger
  target.
- **Compression theory ([Large Text Compression Benchmark](https://www.mattmahoney.net/dc/text.html)):**
  entries ranked 4–10 on enwik9 are transformer/context-mixing hybrids at 89–96 MB (LTCB, Nov
  2026; ranks 1–3 not read), at more than 100k ns/byte. LLM-plus-arithmetic-coding compressors (e.g.
  [Nacrith, 2602.19626](https://arxiv.org/abs/2602.19626)) optimize bits, and their output
  is unreadable to the model that must reason over it. Two lessons transfer: count the
  decoder (here, primer tokens and comprehension cost), and use a strong compressor as an
  information-content yardstick to measure headroom (bits/token above).

### Final evaluation protocol (seed 777)

- **Pre-declared metric:** total billed tokens per question (input and output, all turns,
  all overhead), paired, at accuracy no worse than the baseline.
- **Freeze:** the code arm (prompt and preview) was frozen after dev seed 101: v1 96.7%,
  then v2 98.3% after two fixes, "keep decimals" and grouped nested fields in the preview.
  Seed 777 was generated afterwards and never inspected before scoring.
- **Large regime:** the benchmark's own question generators run on its own
  `TOKEN_EFFICIENCY_DATASETS` (`bench/export_large.ts`).
  - The ground truth matched an independent recount on a spot check (Engineering head
    count, 334 = 334).
  - A duplicate-name screen found no ambiguous lookups.
  - TOON is billed per dataset from real calls and weighted to the full 239-question mix.
  - The "cheapest lossless inline" estimate is min(compact JSON, TOON, CSV, ctab) per
    dataset in o200k, scaled by the measured Claude/o200k ratio.
  - No inline prompt overflowed the context window; the largest was event logs at 161k.
- **What's not counted:** local Python execution, which is milliseconds on these files.
  Running model-written code needs a sandbox in production.

## 5. Limitations

- One model (claude-haiku-4-5) from one family, one run per arm, and 239 questions. The
  roughly ±6 pp intervals hide small format effects; only the code/tool effect is large
  enough to be robust to this. The large regime is a single seed (777), so seed-to-seed variance is not measured.
- The questions are templated lookups, counts, filters and aggregations, which favour
  code by construction. Semantic or free-text questions over records were not tested.
- The headline was measured in `bench/run.py`. `selector.py --preview-above-tokens` emits
  the preview, but no adapter consumes `preview_path` yet; wiring that in is follow-up work.
- This is a retrieval QA benchmark. Agent-trajectory compression (SWE-bench-style
  observation pruning) was not tested and was out of compute budget.
- The benchmark datasets are synthetic and small (≤100 rows for accuracy). The
  structure-awareness category is designed around TOON's syntax.
- Token counts use OpenAI tokenizers (o200k and cl100k). Claude's tokenizer differs;
  measured Claude input tokens per question are reported in `stats.py` output.
- The tool arm's preview generator was written for this benchmark. Its misses are rounding
  and field-order questions, where the preview's field ordering was not faithful.

## 6. Reproduce

```sh
pip install tiktoken
git clone https://github.com/toon-format/toon ../toon-repo
(cd ../toon-repo && npx pnpm install --filter "@toon/benchmarks...")
# make all three faker seeds configurable (12345 / 67890 / 67891 are unchanged at BENCH_SEED=12345)
sed -i '' 's/^faker.seed(12345)/faker.seed(Number(process.env.BENCH_SEED ?? 12345))/; s/^faker.seed(67890)/faker.seed(Number(process.env.BENCH_SEED ?? 12345) + 55545)/; s/^faker.seed(67891)/faker.seed(Number(process.env.BENCH_SEED ?? 12345) + 55546)/' ../toon-repo/benchmarks/src/datasets.ts
cd bench
BENCH_SEED=777 node export.ts out/seed-777            # small regime (accuracy datasets)
BENCH_SEED=777 node export_large.ts out/large-777     # large regime (token-efficiency datasets)
python tokens.py out/seed-777                         # token efficiency, no LLM
python run.py out/seed-777 code toon                  # needs `claude` CLI; Haiku, thinking off
python run.py out/large-777 code
python run.py out/large-777 toon tool-nopreview   # all 239 questions per arm
node grade.ts results/*/*.jsonl && python stats.py results/seed-777
```

Raw per-question results are in `bench/results/`.
