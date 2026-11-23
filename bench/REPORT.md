# Read or query? How agents should handle structured data, and how we fooled ourselves first

*Wexpro Labs, November 2026. Every result is for one model, claude-haiku-4-5. Data, code and the
pre-registration are in this repository, and every number here regenerates from a fresh clone (see
[Reproduce](#7-reproduce)).*

## TL;DR

- **Our first headline was wrong.** We had claimed "15x fewer tokens, 98–100% accurate". Four measurement traps
  produced it. Each is common, and each is easy to fall into again (section 2). Once we controlled for them, the
  15x became 2–5x.
- **A schema preview helps agents, but differently on synthetic and real data.** The preview is about 150 tokens
  of column names and types, shown before the agent writes code.
  - On six fresh synthetic sets it halves the agent's tokens: 1.9x, 95% CI 1.86–2.05.
  - On 70 real DataBench tables it raises accuracy from 81.0% to 86.8%: +5.8 pts, CI 4.0–7.5. It saves no
    tokens there.
  - Column names alone work as well as the full preview.
- **Reading a big file into context costs a lot, and most real tables don't fit at all.**
  - On 2,000-row-class files, reading cost 17x more tokens than an agent run (CI 5–31x, depending on size).
    With thinking off, reading was also 35 pts less accurate.
  - 42 of 70 DataBench tables are too large to read into a 200k window.
- **The reading baseline's failures are counting failures, and thinking fixes most of them.**
  - With thinking on, reading small files scores 95.0%. The agent is not detectably better (+2.1 pts, CI −1.8 to
    +6.9), but it costs 1.9x fewer tokens.
  - We did not test thinking-on reading on large files.
- **One-shot code is brittle.** It matches the agent on synthetic data but loses 7.5 pts on real tables, where
  65 of its scripts crashed.

## 1. The question

Agents increasingly meet JSON, CSV and log files as tool output or workspace data. They can read them in two
ways:

- **Read:** put the whole file in context. Token-efficient notations like TOON try to make this cheaper.
- **Query:** let the model write code against the file. The model then sees only what it computes.

We asked three things:

- Which approach is cheaper and more accurate?
- Does a short schema preview make querying better?
- Do the answers hold on real data, not only on the synthetic benchmark the notations were tuned on?

## 2. How we fooled ourselves first

Our first public version reported that a code-from-preview approach used **15x fewer tokens than a plain agent**
and answered **98–100%** correctly, against 69% for TOON. Colleagues pushed back, and they were right. Four traps
produced those numbers.

| Trap | What happened | What it was worth |
| --- | --- | --- |
| **Harness overhead counted as method** | The "plain agent" baseline ran in Claude Code with the Bash tool enabled. That tool's schema and system prompt add **3,896 tokens to every turn**: 4,706 tokens for a trivial prompt with the tool, 810 without. Our method ran with tools off. | Most of the 15x. On an equal lean harness, the gap was 2.5x against an agent with a preview and 5x against one without. |
| **Small baseline samples** | The baselines ran on 32 of 239 questions (`--per-dataset 4`), and our method ran on all 239. | Full runs moved TOON from 68.8% to 61.5%, the agent from 93.8% to 95.0%, and our method from 100% to 98.3%. |
| **Billing artifacts read as prices** | The CLI writes every large prompt into a 1-hour cache at **2x list price**: a 60,698-token single call was billed $0.1214. Small prompts are billed at list price. | Our derived "9.6x / 68x in dollars" figures meant nothing and were withdrawn. |
| **A crippled baseline** | Reading ran with thinking off, so the model had to count thousands of rows in a single pass. | On small files, thinking raised reading from 63.6% to 95.0% (section 4.2). |

A smaller fifth trap was **dev data reused as test data**. We had read seed 777's failures before reporting on it,
and one benchmark dataset (GitHub repos) is identical on every seed.

The fixes are what the rest of this report does:

- One lean harness for every arm, so fixed overhead is equal.
- Full question sets, never subsamples.
- Raw token counts plus two stated price schedules, never the CLI's `cost_usd`.
- A thinking-on reading baseline.
- Fresh seeds and an independent benchmark, run under a pre-registration committed first.

## 3. Study design

- **Pre-registered.** [`PREREGISTRATION.md`](PREREGISTRATION.md) fixed the arms, endpoints, non-inferiority margin
  and claim language before any confirmatory run. It also records file hashes at commit `4768e96`. All six
  deviations are listed in it with reasons. The main ones:
  - a usage-limit outage, whose failed calls were re-run;
  - compute cuts, then three in-context tests restored before they ran;
  - JSON baselines added as exploratory tests.
- **Benchmarks.**
  - The official [TOON retrieval benchmark](https://github.com/toon-format/toon/tree/main/benchmarks), with its own
    datasets, question generators and grader. We used three fresh seeds (9001–9003), each in a small regime
    (≤100 records) and a large regime (up to 2,000 records), 239 questions per set.
  - [DataBench](https://huggingface.co/datasets/cardiffnlp/databench) (SemEval-2025 Task 8): real tables with
    human-written questions and an official comparator. Ten seeded dev tables were used only to adapt loading; the
    test split (70 tables, 1,613 questions) was run once.
- **Arms.** Every arm sees the byte-identical file and runs with tools off and the same 810-token base.
  - `agent`: our method, schema preview plus a Python loop of up to 6 executions, ending with `FINAL: <answer>`.
  - `agent-nopreview`: the same loop with no preview.
  - `code`: preview plus one script, no repair step.
  - In-context reading: `toon`, `toon-think`, `json-pretty`, `json-compact`, and `table` (CSV, DataBench tables
    ≤10k tokens).
- **Statistics.**
  - Intervals are 95% cluster bootstraps, with the cluster being one dataset in one set. Questions from the same
    dataset are not independent, so these intervals are wider than per-question tests.
  - p-values are cluster sign-flip tests. We also report question-level McNemar, which is anti-conservative.
  - Tokens are counted in full over every turn.
  - There are two cost schedules at list price: *no-cache*, and *ideal-cache*. Ideal-cache re-reads each turn's
    prefix at 0.1x, the schedule most favourable to multi-turn agents.

## 4. Results

### 4.1 The schema preview

| | Accuracy with preview | Without | Tokens per question, with vs without |
| --- | --- | --- | --- |
| Six synthetic sets (n = 1,434) | 97.6% | 96.4% | 3,150 vs 6,156 |
| DataBench test (n = 1,613) | 86.8% | 81.0% | 7,096 vs 7,037 |

- **Synthetic sets.**
  - Tokens: 1.96x fewer (CI 1.86–2.05); 1.89x under ideal caching (CI 1.81–1.96).
  - Accuracy: +1.1 pts (CI +0.2 to +2.1, sign-flip p = 0.089). The gain is small and borderline.
- **DataBench.**
  - Accuracy: +5.8 pts (CI +4.0 to +7.5). The biggest gains are on numeric answers (87.4% vs 78.3%) and on wide
    tables of 25+ columns (87.7% vs 79.6%).
  - Tokens: no saving (1.00x, CI 0.88–1.11). On real tables the agent explores anyway, and the preview grows with
    the number of columns.
- **Names alone suffice.** Names-only previews matched full ones (+0.1 pts, CI −0.5 to +0.7) and were 12% cheaper.

**Against the pre-registered rules:**

| Hypothesis | Verdict |
| --- | --- |
| H2(a): synthetic token saving | Passed |
| H3: non-inferiority | Passed |
| H4: DataBench accuracy gain | Passed |
| H4: DataBench token saving | Failed |

### 4.2 Reading versus querying

| Setting | Agent with preview | Reading in context | Token ratio, reading ÷ agent |
| --- | --- | --- | --- |
| Large files, TOON, thinking off (seed 9001) | 97.5% at 3,047 | 61.9% at 51,754 | 17.2x (CI 5.3–31.1) |
| Small files, TOON, thinking off (3 seeds) | 97.6% at 3,215 | 64.6% at 5,400 | 1.7x (CI 1.27–2.06) |
| Small files, TOON, **thinking on** (seed 9001) | 97.1% at 3,276 | 95.0% at 6,346 | 1.9x (CI 1.16–2.60) |
| Small files, JSON pretty / compact (seed 9001) | 97.1% at 3,276 | 61.9% at 9,026 / 59.8% at 6,128 | 2.7x / 1.9x |
| Real tables ≤10k tokens, CSV (DataBench) | 88.0% | 52.4% | 1.0x (CI 0.65–1.53) |

**Reading cost depends on file size.** On seed 9001, reading cost 0.4x the agent's cost on a 6-record config,
2.8x at 100 records, and 58.5x on 2,000 event logs. That spread is why the pre-registered rule "lower bound above
10x" **failed**, even though the mean is 17x.

**JSON costs the most to read.** Offline token counts put large-file reading at about 87k tokens per question for
pretty JSON, 57k for compact JSON and 52k for TOON. TOON was not distinguishably more accurate than pretty JSON
(+1.8 pts, CI −1.9 to +4.9).

**On DataBench, reading is often impossible.**

| Table size (CSV tokens) | Tables | Mean reading cost | Agent tokens per question |
| --- | --- | --- | --- |
| under 10k | 6 | 4.9k | 5.7k |
| 10k–200k | 22 | 63k | 5.9k |
| 200k–1M | 11 | 533k (does not fit) | 6.0k |
| 1M and over | 31 | 12.5M (does not fit) | 8.8k |

### 4.3 One-shot code

On the synthetic sets, a single script was as accurate as the agent (98.0% vs 97.6%, +0.4 pts, CI −0.2 to +1.1)
and used 2.5x fewer tokens. On DataBench it lost 7.5 pts (79.3% vs 86.8%, CI −10.6 to −4.7).

- **Why it lost:** 65 scripts crashed, mostly on column names, an error a second turn would have fixed.
- **Hardest answer type:** list-of-numbers answers dropped to 58.1%.

Synthetic benchmarks reward one-shot pipelines that real data punishes.

### 4.4 What goes wrong

The failure taxonomy is in [`results2/ANALYSIS.md`](results2/ANALYSIS.md); it uses a heuristic classifier.

- **Reading fails at counting.**
  - On small files, 81 of 87 wrong TOON answers were wrong or near-miss numbers.
  - By question type: filtering 10.4% correct, aggregation 36.5%, field lookup 98.9%.
  - With thinking on, filtering and aggregation rise to 89.6% and 93.7%.
- **Agents fail on edges.** On the synthetic sets, the agent's 35 errors out of 1,434 are mostly rounding (8),
  list order or format (6) and wrong numbers (11).
- **On DataBench, wrong values dominate for every arm.** The preview cut the agent's wrong values from 161 to 97
  and its wrong numbers from 52 to 23.

## 5. What we recommend to harness builders

1. **Don't put large structured files in context.** Give the model a short description and an execution tool.
   For most real tables, that is the only option that fits the window.
2. **Show column names before the first query.** It is cheap, and it makes agents more accurate on real data.
   Sample rows and types added nothing in our tests.
3. **Keep a repair loop.** One-shot code looks great on synthetic benchmarks and breaks on real tables.
4. **Small files can be read, if the model can think.** With thinking on, reading was nearly as accurate as
   querying, at about twice the tokens.
5. **Measure in an equal harness, on full question sets, with raw tokens.** The traps in section 2 can produce a
   10x effect out of nothing.

## 6. Limitations, and what would change our minds

- **One model.** Everything is claude-haiku-4-5. Larger models may read better, which would narrow section 4.2.
- **Thinking-on reading was not tested on large files.** If it reaches about 95% on 2,000-row files, the
  large-file case rests on cost alone. The cost case stays at about 17x tokens, plus the window limit.
- **Benchmark limits.**
  - The TOON-benchmark questions are templated lookups, counts and filters, which favour code.
  - DataBench tables are public and could be in training data.
  - The in-context arm on DataBench covers only 6 small tables.
- **Harness limits.**
  - Our lean agent is Python-only, with at most 6 executions, and is not any vendor's production harness.
  - Wall-clock time and local execution cost were not measured.
- **Single runs, but the variance is covered.** Each arm ran once. Seed-to-seed variance, which includes
  run-to-run noise, is captured by the cluster bootstrap across 3 seeds and 70 tables.
- **Not a new idea.** Answering table questions with code is established (PAL, DataBench systems). Our
  contribution is the controlled measurement and the traps.

## 7. Reproduce

All results are committed under `bench/results2/`, including raw answers, per-call token usage and agent
transcripts. Everything below runs from a fresh clone without model calls.

```sh
python -m unittest discover -s tests          # 61 tests
bash bench/final_stats.sh                     # every interval and test in this report (stdlib only)
python bench/analysis.py bench/databench      # failure taxonomy and breakdowns
```

**Verification on a fresh clone (2026-11-23):**

- All 8,843 TOON-benchmark answers were re-graded with the benchmark's own grader (`grade.ts`), and all 5,005
  DataBench answers with `databench_eval`'s comparator. None changed.
- `FINAL_STATS.txt` and `ANALYSIS.md` regenerated byte for byte.

**Re-running the models** uses `bench/harness.py` and `bench/harness_db.py`, with the claude CLI and
`bench/requirements.lock`. The confirmatory runs used 64.6M Haiku tokens, measured from the logged usage.
