/goal lean clean (we do not have llm api to test fo just setup the skeleton for now relating to that. 

# Deep Research: context-compression Production Readiness Assessment

## Goal: What's needed to publish and deploy in Fortune 500 environments

---

## 1. Competitive Landscape (as of May 2026)

### Direct Competitors

| Project | Approach | Token Savings | Answer Parity | Production Status |
|---------|----------|---------------|---------------|-------------------|
| **TOON** (toon-format/toon) | Custom notation (object→key:value rows) | ~40% on mixed structured | Claimed, limited evals | NPM package, 6K+ GitHub stars, blog posts showing 30-60% savings in real agent stacks |
| **ONTO** (arxiv 2604.17512) | Columnar pipe-delimited notation | 46-51% on synthetic ops data | Tested on 4 task types with GPT-5.4-mini, **Q2 counting fails across ALL formats** | Academic paper, no production tooling |
| **Dictionary-Encoding** (arxiv 2604.13066) | LZ-style meta-token replacement | 20-80% on logs | Decompression verified (0.92-0.98 Levenshtein), **not task-accuracy verified** | Academic paper only |
| **LLMLingua/2** (microsoft/LLMLingua) | Neural token pruning | 2-20x on natural language | Extensively benchmarked, **lossy by design** | 6.2K GitHub stars, requires GPU, 115ms-21s latency |
| **context-compression** (this repo) | Tokenizer-aware format selector | 13.3% aggregate, up to 73% on repetitive JSON | **Round-trip lossless verified, NO task-accuracy evidence** | Local hook, 4 adapter stubs |

### Key Competitive Differentiation

**context-compression is the only project that is simultaneously:**
1. Lossless (verified round-trip)
2. Deterministic (same input → same output)
3. Tokenizer-specific (exact counts, not estimates)
4. Zero-dependency runtime (stdlib Python only)
5. Multi-agent (Codex, Pi, Hermes, OpenClaw, MCP adapters)
6. Format-agnostic (selector picks best from candidate pool)

**TOON and ONTO invent one format and claim it's best. This project acknowledges no single format wins everywhere and selects per-file.**

---

## 2. What the Research Actually Proves (and Doesn't)

### PROVEN by literature:

| Claim | Evidence Source | Strength |
|-------|---------------|----------|
| Eliminating repeated JSON keys saves 40-50% tokens | ONTO §5.1 Table 3: key elimination = 113% of gross savings | **Strong** (exact measurement) |
| Format choice changes model behavior | Table Meets LLM §5.2: HTML > NL+Sep by 6.76%, order matters | **Strong** (controlled experiment) |
| Lossless dictionary encoding works for repetitive data | Dictionary-Encoding §5.1: 20-80% compression on logs, 0.92+ decompression | **Moderate** (decompression ≠ task accuracy) |
| Compression overhead can erase latency gains | Prompt Compression in the Wild §4.1: LLMLingua adds 3-21s overhead | **Strong** (real hardware measurement) |
| Models can learn in-context dictionaries | Dictionary-Encoding §5.2: Claude 0.994 exact match on template decompression | **Strong** (12/14 datasets perfect) |
| Moderate compression helps long-context, hurts short-context | Empirical Study §5.1: performance improves then degrades with ratio | **Strong** (13 datasets, 6 methods) |
| Token count ≠ comprehension accuracy | ONTO §5.4 Table 5: Q2 counting fails at 15-40% across ALL formats including JSON | **Critical** (format-independent failure) |

### NOT YET PROVEN for context-compression specifically:

| Gap | Why It Matters | Severity for F500 |
|-----|---------------|-------------------|
| No task-accuracy evaluation | "Same data, fewer tokens" doesn't guarantee "same answers" | **Blocking** |
| No multi-model validation | Only tested on GPT-5.5 tokenizer; no Claude/Gemini/Llama accuracy data | **Blocking** |
| No production latency profile | 66s for 28 files is benchmark-only; no per-request p50/p95 numbers | **High** |
| No caching strategy | Same file re-read = full recomputation | **High** |
| Adapter contracts unverified against live hosts | Pi/Hermes/OpenClaw adapters may not match real plugin APIs | **Medium** |
| No security audit | Hook processes untrusted file content; no sandboxing | **Medium** |

---

## 3. The Critical Research Gap: Task-Accuracy Evidence

### What ONTO Found (The Warning)

ONTO tested 4 question types on GPT-5.4-mini with 50 records x 20 runs:

| Task | JSON | ONTO (warm) | Verdict |
|------|------|-------------|---------|
| Q1 Lookup (IoT) | 100% | 95% | ONTO **worse** (-5%) |
| Q2 Count | 30% | 40% | All formats fail; ONTO slightly better |
| Q3 List | 100% | 100% | Parity |
| Q4 Max | 100% | 100% | Parity |
| **Q1 Lookup (Metrics)** | **25%** | **65%** | ONTO **better** (+40%) |

**Lesson:** Format change helps some tasks and hurts others. You cannot assume parity; you must measure it.

### What Dictionary-Encoding Found (The Promise)

On template-based decompression with Claude 3.7 Sonnet:
- 12/14 datasets: **perfect 1.000 exact match**
- Mac dataset (hardest): 0.928 exact match, 0.977 Levenshtein
- Full-scale (100K entries): 0.969 exact match

On algorithmic compression (cross-log patterns):
- Most datasets: >0.90 Levenshtein similarity
- Mean ROUGE: 0.96
- **But: this measures decompression, not downstream task accuracy**

### What Table Meets LLM Found (The Risk)

- HTML format beats all others by **6.76%** on structural understanding
- Zero-shot drops accuracy by **30.38%** vs 1-shot
- **Adding format explanations hurts search/retrieval tasks**
- This means context-compression's decoder instructions ("Types: i=int n=num...") could actively hurt lookup performance

---

## 4. Production Readiness Roadmap (Lean, Drop-In)

### Phase 1: Evidence That Unlocks F500 Confidence (2-3 weeks)

**The single most important deliverable: paired task-accuracy evaluation.**

```
For each (model, file, task_type):
  raw_answer = query(model, raw_file, question)
  opt_answer = query(model, optimized_file, question)
  assert opt_answer == raw_answer  # or equivalent accuracy
```

**Minimum viable eval matrix:**

| Model Family | Models to Test |
|-------------|---------------|
| OpenAI | GPT-4o, GPT-4o-mini, GPT-5.5 |
| Anthropic | Claude 3.5 Sonnet, Claude 4 |
| Google | Gemini 2.0 Flash, Gemini 2.5 Pro |
| Open-source | Llama 3.3 70B, Qwen 2.5 72B |

**Task types (from literature):**

| Task | What it tests | Success criteria |
|------|--------------|-----------------|
| Exact lookup | "What is the value of field X in row Y?" | 100% match |
| Count | "How many rows have field X > value?" | >=95% match with raw |
| Aggregation | "What is the max/min/sum of field X?" | 100% match |
| List extraction | "List all unique values of field X" | Set equality |
| Nested value | "What is row[Y].nested.field?" | 100% match |
| Schema inference | "What are the column names and types?" | 100% match |
| Missing/null | "Which rows have null in field X?" | 100% match |
| Reconstruction | "Output the complete row for id=Y as JSON" | Exact match |

**Publication threshold for F500:**
- 0 regressions on lookup/aggregation/schema (these are deterministic)
- <=2% accuracy drop on counting (known model weakness)
- <=1% drop on nested value extraction
- Tested on >=3 model families
- Tested on >=5 diverse datasets (not just Titanic)

### Phase 2: Production Hardening (1-2 weeks)

**2.1 Sidecar caching with content-addressed lookup:**
```python
cache_key = sha256(file_path + file_mtime + file_size + model_slug)
if cache_hit(cache_key):
    return cached_sidecar  # <1ms
```
Target: <5ms for cache hits (covers 90%+ of agent reads in practice).

**2.2 Candidate tier system:**
```python
TIER_SAFE = {"raw", "compact-json", "csv", "tsv", "column-json"}  # Familiar formats
TIER_ADVANCED = {"codebook-json", "typed-csv", "typed-tsv"}         # Needs eval proof
TIER_EXPERIMENTAL = {"codebook-row", "typed-codebook-row"}          # Custom grammar

# Default: TIER_SAFE only
# Opt-in with CONTEXT_OPTIMIZER_CANDIDATE_TIER=advanced after eval proof
```

**2.3 Per-file latency budget:**
```python
MAX_HOOK_MS = 500  # Hard ceiling for interactive hooks
if estimated_processing_ms > MAX_HOOK_MS:
    return noop()  # Don't block the agent
```

**2.4 Security boundaries:**
- File size hard cap (default 5MB, configurable)
- No network access during selection
- Sidecar directory sandboxed to `.codex/context-cache/`
- Report includes SHA-256 of source + output for tamper detection
- No eval of file content (no `exec`, no `import`)

### Phase 3: Enterprise Integration (2-4 weeks)

**3.1 Drop-in packaging:**
```
pip install context-compression          # Core selector
pip install context-compression[codex]   # + Codex hook
pip install context-compression[hermes]  # + Hermes plugin
pip install context-compression[mcp]     # + MCP server
```

**3.2 Observability (required for F500):**
```json
{
  "event": "context_optimization",
  "timestamp": "2026-05-14T02:44:18Z",
  "source_file": "data/customers.json",
  "source_bytes": 245000,
  "source_tokens": 89234,
  "optimized_tokens": 52341,
  "savings_ratio": 0.413,
  "selected_format": "codebook-json",
  "latency_ms": 234,
  "model": "gpt-4o",
  "cache_hit": false,
  "decision": "selected"
}
```
Ship as structured log + optional OpenTelemetry span.

**3.3 Configuration for enterprise:**
```toml
[context-compression]
enabled = true
candidate_tier = "safe"              # "safe", "advanced", "experimental"
max_file_bytes = 5_000_000
min_savings_ratio = 0.10             # 10% minimum to activate
min_saved_tokens = 256               # Don't bother for tiny wins
max_hook_latency_ms = 500            # Hard ceiling
cache_enabled = true
cache_dir = ".codex/context-cache"
telemetry = "structured-log"         # "structured-log", "otel", "none"
models_allowed = ["gpt-4o", "gpt-4o-mini", "claude-*"]  # Allowlist
```

**3.4 Compliance:**
- Data never leaves the local machine (no network calls)
- Source files never modified
- Sidecars are derivative works, not copies (relevant for data governance)
- Full audit trail via report files + telemetry
- Opt-out is always available (set `enabled = false` or remove hook)

---

## 5. Research-Backed Positioning for Publication

### The Paper Thesis (If Writing Up)

> "Existing structured-data compression for LLMs either invents one format and assumes it's universally best (TOON, ONTO), or applies lossy compression that changes model behavior (LLMLingua). We present a deterministic selector that picks the lowest-token lossless representation per file per tokenizer from a verified candidate set. On a benchmark of 28 files across 7 public datasets, the selector saves 13.3% aggregate tokens (up to 73% on repetitive structured data), provably round-trips to the source value, and matches raw-context task accuracy within 1% across GPT-4o, Claude 3.5, and Gemini 2.0 on [N] task types."

### Why This Beats The Competition

| Axis | TOON | ONTO | LLMLingua | context-compression |
|------|------|------|-----------|---------------------|
| Lossless | Y (format-level) | Y (format-level) | N (lossy) | Y (verified per-file) |
| Deterministic | Y | Y | N (model-dependent) | Y |
| Per-tokenizer optimal | N (format-fixed) | N (format-fixed) | N | Y |
| Zero dependencies | N (npm) | N (not shipped) | N (PyTorch, GPU) | Y (stdlib Python) |
| Handles varied data | N (arrays only) | N (flat/nested records) | Y (any text) | Y (JSON/JSONL/CSV/TSV) |
| Task-accuracy proven | Limited | Limited (4 tasks, 1 model) | Extensive | **TODO** |
| Production tooling | npm package | None | Python package | Hook + adapters |
| Enterprise-ready | No observability | No | No | **With Phase 3** |

### The Honest Limitations Section

1. **Structured data only.** Natural language, code, images, and mixed-format documents are out of scope. LLMLingua remains better for natural-language prompt compression.

2. **Savings are data-dependent.** Highly repetitive JSON/JSONL with uniform schemas sees 40-73% savings. Already-compact CSVs or diverse nested structures see 5-10%. The selector correctly no-ops when savings are below threshold.

3. **Custom codebook formats require model familiarity.** Codebook-json and typed-codebook-row are custom prompt languages that frontier models handle well but smaller models may struggle with. The tiered candidate system mitigates this.

4. **Hook coverage is limited to whole-file reads.** Partial reads, paginated access, search-then-read patterns, and non-Bash tool paths are not optimized.

---

## 6. What to Do Right Now (Priority Order)

| # | Action | Time | Why |
|---|--------|------|-----|
| 1 | **Run paired task-accuracy evals on GPT-4o + Claude** | 3 days | This is THE blocking gap. Without it, the project is a token counter, not a quality-safe optimizer. ONTO proved format changes help some tasks and hurt others. |
| 2 | **Add content-addressed sidecar caching** | 1 day | 66s/28 files is unusable for production hooks. Cache hits should be <5ms. |
| 3 | **Implement candidate tiers (safe/advanced/experimental)** | 1 day | Drop custom codebook from defaults until eval proof. Ship compact-json + column-json + csv/tsv as safe defaults. |
| 4 | **Add an ONTO-style pipe-delimited candidate** | 1 day | ONTO's research shows 46-51% savings with good task accuracy. It's simpler than codebook and models handle it better. |
| 5 | **Write the eval results into EVIDENCE.md** | 1 day | With numbers from #1, the publication claim becomes defensible. |
| 6 | **Package as pip-installable** | 2 days | F500 won't `git clone` and `chmod +x`. They need `pip install context-compression`. |
| 7 | **Add structured telemetry/observability** | 2 days | Enterprise requirement for any production middleware. |
| 8 | **Ship** | - | Once #1 proves accuracy parity, the rest is packaging. |

---

## 7. Bottom Line

**The architecture is right. The implementation is solid. The evidence is incomplete.**

The project has a genuinely defensible position in the landscape - it's the only tool that selects rather than invents, verifies rather than assumes, and targets exact tokenizers rather than approximating. But the gap between "provably lossless representation" and "provably same model behavior" is exactly what every paper in this space says you must close before claiming production safety.

Close that gap with 3 days of eval work, and this is publishable + deployable. Without it, it's an impressive engineering project that a responsible F500 CISO would block.

---

## References (Key Papers Cited)

1. **ONTO** - "A Token-Efficient Columnar Notation for LLM Input Optimization" (arxiv 2604.17512, April 2026)
2. **Dictionary-Encoding** - "Lossless Prompt Compression via Dictionary-Encoding and In-Context Learning" (arxiv 2604.13066, March 2026)
3. **Prompt Compression in the Wild** - (arxiv 2604.02985, April 2026)
4. **Table Meets LLM** - "Can Large Language Models Understand Structured Table Data?" (arxiv 2305.13062, 2023)
5. **An Empirical Study on Prompt Compression** - (arxiv 2505.00019, April 2025)
6. **LLMLingua-2** - "Data Distillation for Efficient and Faithful Task-Agnostic Prompt Compression" (arxiv 2403.12968, 2024)
7. **Lossless Token Sequence Compression via Meta-Tokens** - (arxiv 2506.00307, May 2025)
8. **Compute Optimal Tokenization** - (arxiv 2605.01188, May 2026)
9. **The Structured Output Benchmark** - (arxiv 2604.25359, April 2026)
10. **When Correct Isn't Usable** - (arxiv 2605.02363, May 2026)
11. **An Information-Theoretic Perspective on LLM Tokenizers** - (arxiv 2601.09039, January 2026)
12. **Token-Oriented Object Notation vs JSON** - (arxiv 2603.03306, February 2026)
