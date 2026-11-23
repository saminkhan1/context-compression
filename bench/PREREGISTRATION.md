# Pre-registration: querying vs reading structured data (claude-haiku-4-5 only)

Written and committed **before** any confirmatory run. Anything not listed here is exploratory
and is labelled so in the report. The scope of every claim is one model (claude-haiku-4-5, via
Claude Code CLI 2.1.327 `-p`, default sampling, thinking off unless an arm is named `-think`).

## What was already seen (dev data, not confirmatory)

- TOON-benchmark seeds 101, 12345 and **777** are development data. Seed 777's per-question
  failures were read before this document was written.
- Earlier headline numbers (15x vs a "plain agent", 9.6x/68x in dollars) are withdrawn: the 15x
  was dominated by Claude Code's Bash tool schema (about 3,900 fixed tokens per turn), and the
  CLI bills large prompts as 1-hour cache writes at 2x list price, so its `cost_usd` is not a price.
- Dev result on seed 777, large regime, controlled lean harness (tools off, 810-token base):
  code 97.9% @1,236 tok/Q; lean agent+preview 97.5% @3,154; lean agent no preview 95.4% @6,240;
  native Claude Code Bash agent+preview 98.7% @10,758; TOON in context 61.5% @51,631.
  Preview ablations (no sample rows; names only) did not change accuracy.
- Dev result on DataBench dev split (10 tables, 217 Q; official comparator): `code` 74.7%, `agent`
  84.8%, `agent-nopreview` 77.0%, in-context `table` 50% (n=20). Only 3 of 80 tables have
  column names carrying a `<gx:type>` suffix that the questions also use; left as-is for all arms.
- Infrastructure errors (throttling, timeouts) are retried up to 3 times by the harness and are
  not scored as wrong unless every attempt fails.

## Frozen method

- Harness: `bench/harness.py` (TOON-benchmark sets) and `bench/harness_db.py` (DataBench); their
  SHA-256 and the prompt fingerprint are recorded in the freeze addendum below.
- Every arm receives the byte-identical data file (`json.dumps(data, indent=2)`; DataBench:
  parquet). Model-written code runs with the pinned venv interpreter (`requirements.lock`).
- Answers are graded by the benchmark's own comparator, unchanged (TOON `compareAnswers`;
  DataBench `Evaluator.default_compare`). Agent arms must end with `FINAL: <answer>`; no other
  rewriting. A second "lenient" grade (last line, markdown stripped) is reported as sensitivity.
- Model errors and missing answers count as wrong; infrastructure errors are retried (see above). No question is dropped, except the 5
  TOON structural-corruption questions that are excluded in every arm (as in dev).
- Token accounting: raw tokens = input + cache + output over all calls. Two cost schedules at
  claude-haiku-4-5 list price ($1/M input, $5/M output): *no-cache*, and *ideal-cache* (a turn's
  prefix re-read at 0.1x, the schedule most favourable to multi-turn agents). Local code
  execution time is not counted and is not measured.

## Confirmatory sets

- **A. TOON benchmark, fresh seeds 9001, 9002, 9003** (generated before dev analysis, never run
  until now). Large regime (token-efficiency datasets, 239 Q/seed) and small regime (accuracy
  datasets, 239 Q/seed).
  - Arms on all six sets: `code`, `code-namesonly`, `agent`, `agent-nopreview`.
  - In-context `toon` and `ctab2`: small regime x3 seeds; large regime on seed 9001 only
    (about 12M tokens per pass; no subsampling).
  - `tool` (native Claude Code Bash + preview) on large seed 9001.
  - `toon-think` (thinking on) on small seed 9001.
  - Run-to-run variance: `code` and `agent` repeated 3x on dev seed 777 large.
- **B. DataBench (cardiffnlp/databench, split `qa`, 80 tables, 1,830 questions)**: questions and
  answers written by others. Ten datasets (seeded split in `split.json`, `random.Random(12)`)
  were used only to adapt loading and answer formats; the other 70 are the test split, run once
  with the frozen harness. In-context `table` (CSV) is run only for tables of <= 30,000 tokens
  (eligibility by size alone).

## Primary hypotheses and decision rules

Primary treatment: **`agent`** = schema preview + a code-execution loop (lean harness, python
only, at most 6 executions). Single-shot `code` is a secondary arm: on DataBench *dev* tables it
scored 74.7% against 84.8% for `agent` (23 of 217 scripts crashed on column names that a second
turn would have fixed), so it is not expected to generalise and is reported as such.

Cluster = (set, dataset); all intervals are 95% cluster bootstrap (5,000 draws), p-values are
cluster sign-flip tests (100k permutations). Question-level McNemar is reported but is
anti-conservative. Holm correction across the accuracy tests H1-H4.

- **H1 (vs reading, large files).** TOON-benchmark seed 9001 large: `agent` is more accurate than
  in-context `toon`, and uses at least 10x fewer raw tokens (lower cluster bound > 10).
- **H2 (effect of the preview).** `agent` vs `agent-nopreview` over the six TOON-benchmark sets:
  (a) token ratio no-preview/preview has an *ideal-cache* lower bound of at least 1.5; (b)
  accuracy difference reported with its cluster interval (superiority claimed only if the
  interval excludes 0).
- **H3 (non-inferiority vs a lean agent).** `agent` accuracy is not more than 3 pp below
  `agent-nopreview` (one-sided 95% lower bound > -3 pp) on the six TOON-benchmark sets.
- **H4 (generalisation).** On the DataBench test split: H2 and H3 are repeated; for eligible
  tables the in-context `table` arm is compared with `agent` on accuracy and tokens.
- **Secondary (exploratory, no thresholds).** `code` and `code-namesonly` vs everything; the
  native Claude Code Bash arm; thinking-on in-context; token and dollar ratios under both schedules.

Claim language is fixed in advance: a "several-fold" saving is claimed only when the lower bound
under the *ideal-cache* schedule is at least 2; "more accurate" only when the cluster interval
excludes 0; otherwise the report says "no detectable difference". If a rule fails, the website and
README are worded to match what passed. No arm, seed or dataset is added or removed after seeing
confirmatory results; any deviation is listed under "Deviations" with its reason.

## Known limitations (stated, not hidden)

- One model. Findings do not transfer to other models without testing.
- TOON-benchmark questions are templated lookups, counts and filters on synthetic data (they
  favour code); DataBench is the independent check, but its tables are public and could be in
  training data.
- The code arm is single-shot and cannot recover from a bad script; the lean agent is
  python-only with at most 6 executions; neither is the production harness of any vendor.
- Single run per arm (apart from the variance study), sampling is not controllable through the CLI.
- Preview generators were written by the authors for the TOON-benchmark shapes (DataBench's
  was adapted on dev tables only).

## Freeze addendum

Frozen immediately before the first confirmatory run (sha256 prefixes):

- prompt fingerprint (harness.py + prompts): `86448b6af0773eec`
- harness.py `5dff41cbaffebc94`, harness_db.py `e38349afa0ead4b1`, study_stats.py `6706ac47a3fcf342`, grade.ts `85ceaf5d3507e1d4`, DataBench split.json `71914dadaa17104d`
- repo commit containing the files above: `ac822ae`  (this addendum is committed right after)

## Deviations

1. **Usage-limit outage (2026-11-20).** The CLI account hit its 5-hour usage limit during the confirmatory
   runs, and calls that failed on it were saved as error rows (large-9003 `code` 108/239, small-9002
   `agent-nopreview` 149/239, DataBench `agent` 173 of 1,572). These are infrastructure failures, not
   model answers, so the rows were deleted and re-run with no other change. `cli()` in harness.py was then
   patched to wait out a usage limit (retry every 10 minutes, up to 12 hours) instead of recording an error;
   this touches no prompt, arm or scoring logic. harness.py hash changed accordingly (see git history).

2. **Compute cuts (2026-11-20, decided before any of these arms had been run).** To save tokens the
   following planned arms were dropped and are *not* part of the confirmatory study: the 3x repeats of
   `code`/`agent` on seed 777 (so run-to-run variance is not measured), `ctab2`, `tool` and `toon-think`,
   in-context `toon` on large seed 9001, and the DataBench in-context `table` arm. Consequences:
   H1 (vs reading, large files) has no confirmatory test; the only in-context large-regime evidence is the
   full 239-question run on dev seed 777 and is labelled dev. H4's in-context comparison is dropped.
   In-context `toon` is still run on the three small-regime seeds.
3. **Reading cost computed offline (2026-11-20, no model calls).** The token cost of reading a file in
   context is deterministic, so for the large regime on seeds 9001-9003 the in-context TOON cost is
   *estimated* by tokenising the exact prompts (o200k) and scaling by the per-dataset Claude/o200k ratio
   measured on dev seed 777 (the estimate reproduces seed 777's measured mean, 51,605 vs 51,631; fresh
   seeds: 51,702 / 51,474 / 51,380 tokens per question). The token half of H1 is therefore reported from
   this estimate, labelled as estimated; the accuracy half rests on dev seed 777 plus the three fresh
   small-regime in-context runs.
4. **Three in-context tests restored (2026-11-20, user decision, before any of them ran).** Because the
   cuts in item 2 left gaps, the following are added back to the confirmatory study: in-context `toon` on
   large seed 9001 (all 239 questions), `toon-think` on small seed 9001, and the DataBench in-context `table`
   arm. To save tokens the DataBench `table` eligibility cutoff is lowered from 30,000 to 10,000 tokens
   (size alone, 6 test tables, 166 questions), so that arm's claim is scoped to small real tables. Item 3's
   offline estimate stays as a cross-check on the measured large-seed run. harness_db.py changed (cutoff
   constant only). Items 1-2's other cuts stand (no repeats, no ctab2, no native-tool arm).

5. **16 usage-limit errors re-run (2026-11-23).** The harness waited its 12-hour maximum for a *weekly* limit;
   16 DataBench `agent-nopreview` rows were saved as errors, deleted and re-run (answers, not repeats).

## Results against the pre-registered rules (claude-haiku-4-5)

Full output: `results2/FINAL_STATS.txt` (`final_stats.sh`). Cluster = (set, dataset); intervals are 95% cluster bootstrap.

| Rule | Result | Verdict |
| --- | --- | --- |
| H1 accuracy: `agent` > in-context `toon`, large seed 9001 | 97.5% vs 61.9%, +35.3 pp [+20.6, +47.5] | **passed** |
| H1 tokens: >= 10x fewer, lower bound > 10 | 17.2x [5.3, 31.1] (offline estimate agrees: 51,702 vs measured 51,754 tokens/Q) | **failed** (mean >= 10x, interval not) |
| H2(a): preview token saving, ideal-cache lower bound >= 1.5 (six TOON-benchmark sets) | 1.89x [1.81, 1.96] (raw 1.96x [1.86, 2.05]) | **passed** |
| H2(b): preview accuracy, six sets | +1.1 pp [+0.2, +2.1]; sign-flip p = 0.089 | borderline, small |
| H3: `agent` not > 3 pp below `agent-nopreview` | lower bound +0.3 pp | **passed** |
| H4: DataBench test, preview accuracy | 86.8% vs 81.0%, +5.8 pp [+4.0, +7.5] | **passed** |
| H4: DataBench, preview token saving (rule H2(a)) | 1.00x raw [0.88, 1.11]; 1.19x ideal-cache [1.10, 1.27] | **failed** |
| H4: DataBench in-context `table` (<= 10k-token tables, n = 166) vs `agent` | 52.4% vs 88.0% on the same questions, +35.6 pp [+25.8, +48.8]; tokens 1.02x [0.65, 1.53] | accuracy gap large; no token gap |

Secondary (exploratory): single-shot `code` is non-inferior to `agent` on the six synthetic sets (+0.4 pp
[-0.2, +1.1], 2.5x fewer raw tokens) but **inferior on DataBench** (-7.5 pp [-10.6, -4.7]). Preview ablation:
names-only vs full preview, +0.1 pp [-0.5, +0.7], 12% fewer tokens, so the sample rows and types add nothing.

**Unplanned finding that qualifies H1.** In-context TOON with thinking on (`toon-think`, small seed 9001):
95.0% vs 63.6% with thinking off (+31.0 pp). The agent is not detectably better than thinking-on reading
(97.1% vs 95.0%, +2.1 pp [-1.8, +6.9]) and uses 1.9x fewer tokens. The large "agent vs reading" accuracy gap is
therefore measured against thinking-off reading; **thinking-on reading was not tested on large files**.
6. **JSON baselines added at the user's request (2026-11-23, exploratory, not pre-registered).** In-context
   `json-pretty` and `json-compact` (thinking off) on small seed 9001 only, to compare with the most common
   data format. Large-regime JSON accuracy is not run (a full pass would cost about 17M+ tokens for compact and
   about 21M for pretty JSON, and the largest pretty file is an estimated 192k tokens, at the edge of the 200k
   window); large-regime JSON sizes are reported from offline tokenisation instead, scaled by the per-dataset
   Claude/o200k ratio measured on TOON prompts (an approximation for JSON). Estimated reading cost per question on
   large seed 9001: json-pretty 87,386, json-compact 57,047, toon 51,745.

**JSON baselines (exploratory, small seed 9001, thinking off, n = 239, 8 clusters).** JSON pretty 61.9% at 9,026
tokens/Q; JSON compact 59.8% at 6,128; TOON 63.6% at 5,385; agent with preview 97.1% at 3,276. Agent vs JSON
pretty: +34.9 pp [+21.7, +45.1], 2.7x fewer tokens [1.6, 3.8]; vs JSON compact: +37.0 pp [+23.7, +47.2], 1.85x fewer
[1.08, 2.63]. TOON vs JSON pretty: +1.8 pp [-1.9, +4.9] (not distinguishable), 1.68x fewer tokens; TOON vs JSON compact:
+3.9 pp [+0.7, +7.5] (sign-flip p = 0.06). Thinking-on JSON was not tested. harness.py gained `json-compact` as an
in-context arm for this (no other change).
