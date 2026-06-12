# Context Compression Benchmark Report

Generated: `2026-05-22T03:18:51+00:00`
Corpus: `data/benchmark-corpus`
Corpus manifest SHA-256: `57b59d1b3d128953ddd15f6001a1793337b8c679acce5a21dbaeb2eb17e868cc`
Model: `gpt-5.4-mini` via `tiktoken`
Input price: `$5.0000` per 1M tokens
Monthly calls: `100000`

## Totals

| Files | Raw tokens | Optimized tokens | Saved tokens | Savings | Saved / call | Saved / month |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 28 | 17496442 | 15171483 | 2324959 | 13.3% | $11.624795 | $1162479.50 |

## Local Processing Time

| Load ms | Candidate ms | Token-count ms | Total ms | Saved tokens / ms | Break-even max input tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 351.4 | 5832.1 | 42420.1 | 48604.4 | 47.8 | 47834.3 |

Break-even interpretation: compression is latency-positive only when the downstream model's input throughput is at or below the break-even ceiling above.

## By Format

| Format | Files | Raw tokens | Optimized tokens | Saved tokens | Savings |
| --- | ---: | ---: | ---: | ---: | ---: |
| `csv` | 7 | 4110345 | 3800680 | 309665 | 7.5% |
| `json` | 7 | 4793580 | 3785039 | 1008541 | 21.0% |
| `jsonl` | 7 | 4478352 | 3785039 | 693313 | 15.5% |
| `tsv` | 7 | 4114165 | 3800725 | 313440 | 7.6% |

## By Source Dataset

| Source | Files | Raw tokens | Optimized tokens | Saved tokens | Savings | Winning formats |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `github-top-repos` | 4 | 822418 | 583066 | 239352 | 29.1% | codebook-json x4 |
| `hf-amazon-reviews` | 4 | 231889 | 196274 | 35615 | 15.4% | raw x1, typed-csv x3 |
| `hf-code-doc` | 4 | 12851724 | 12181661 | 670063 | 5.2% | raw x1, typed-csv x3 |
| `hf-loghub-2` | 4 | 162725 | 95180 | 67545 | 41.5% | codebook-json x4 |
| `hf-openassistant` | 4 | 2249389 | 1755212 | 494177 | 22.0% | codebook-json x4 |
| `hf-squad` | 4 | 1007209 | 275338 | 731871 | 72.7% | codebook-json x4 |
| `hf-titanic-tabular` | 4 | 171088 | 84752 | 86336 | 50.5% | codebook-json x4 |

## Candidate Ablation

| Candidate | Files | Wins | Tokens | Savings | Avg rank | Rank range |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `codebook-json` | 28 | 20 | 16406096 | 6.2% | 1.79 | 1-4 |
| `typed-csv` | 22 | 6 | 14532768 | 5.2% | 2.77 | 1-5 |
| `raw` | 28 | 2 | 17496442 | 0.0% | 4.39 | 1-8 |
| `typed-tsv` | 22 | 0 | 14541860 | 5.1% | 3.27 | 2-6 |
| `column-json` | 28 | 0 | 17476598 | 0.1% | 3.57 | 2-6 |
| `compact-json` | 28 | 0 | 17927812 | -2.5% | 5.57 | 3-8 |
| `csv` | 28 | 0 | 18175682 | -3.9% | 6.18 | 4-8 |
| `tsv` | 24 | 0 | 18022962 | -4.0% | 6.29 | 4-8 |

## Files

| File | Kind | Bytes | Best format | Raw tokens | Optimized tokens | Savings |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| `github-top-repos.csv` | csv | 637820 | codebook-json | 203946 | 147322 | 27.8% |
| `github-top-repos.json` | json | 788074 | codebook-json | 233167 | 144211 | 38.2% |
| `github-top-repos.jsonl` | jsonl | 645734 | codebook-json | 180876 | 144211 | 20.3% |
| `github-top-repos.tsv` | tsv | 637542 | codebook-json | 204429 | 147322 | 27.9% |
| `hf-amazon-reviews.csv` | csv | 192759 | raw | 49052 | 49052 | 0.0% |
| `hf-amazon-reviews.json` | json | 262978 | typed-csv | 74176 | 49074 | 33.8% |
| `hf-amazon-reviews.jsonl` | jsonl | 232976 | typed-csv | 59248 | 49074 | 17.2% |
| `hf-amazon-reviews.tsv` | tsv | 192039 | typed-csv | 49413 | 49074 | 0.7% |
| `hf-code-doc.csv` | csv | 11565523 | raw | 3045398 | 3045398 | 0.0% |
| `hf-code-doc.json` | json | 11988648 | typed-csv | 3387571 | 3045421 | 10.1% |
| `hf-code-doc.jsonl` | jsonl | 11958646 | typed-csv | 3372988 | 3045421 | 9.7% |
| `hf-code-doc.tsv` | tsv | 11565507 | typed-csv | 3045767 | 3045421 | 0.0% |
| `hf-loghub-2.csv` | csv | 96769 | codebook-json | 37825 | 23795 | 37.1% |
| `hf-loghub-2.json` | json | 119784 | codebook-json | 45907 | 23795 | 48.2% |
| `hf-loghub-2.jsonl` | jsonl | 107782 | codebook-json | 41171 | 23795 | 42.2% |
| `hf-loghub-2.tsv` | tsv | 96743 | codebook-json | 37822 | 23795 | 37.1% |
| `hf-openassistant.csv` | csv | 1362436 | codebook-json | 510074 | 443022 | 13.1% |
| `hf-openassistant.json` | json | 2147942 | codebook-json | 700537 | 434584 | 38.0% |
| `hf-openassistant.jsonl` | jsonl | 1540917 | codebook-json | 526800 | 434584 | 17.5% |
| `hf-openassistant.tsv` | tsv | 1361960 | codebook-json | 511978 | 443022 | 13.5% |
| `hf-squad.csv` | csv | 1041306 | codebook-json | 240559 | 70875 | 70.5% |
| `hf-squad.json` | json | 1174016 | codebook-json | 278193 | 66794 | 76.0% |
| `hf-squad.jsonl` | jsonl | 1085014 | codebook-json | 247191 | 66794 | 73.0% |
| `hf-squad.tsv` | tsv | 1040498 | codebook-json | 241266 | 70875 | 70.6% |
| `hf-titanic-tabular.csv` | csv | 45385 | codebook-json | 23491 | 21216 | 9.7% |
| `hf-titanic-tabular.json` | json | 185451 | codebook-json | 74029 | 21160 | 71.4% |
| `hf-titanic-tabular.jsonl` | jsonl | 137551 | codebook-json | 50078 | 21160 | 57.7% |
| `hf-titanic-tabular.tsv` | tsv | 45385 | codebook-json | 23490 | 21216 | 9.7% |
