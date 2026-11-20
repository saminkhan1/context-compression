# Benchmark Data

`data/benchmark-corpus/` is the local downloaded benchmark corpus. It is
git-ignored because the corpus is large generated data, not source code.

To create or refresh it:

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

The build step reuses existing files by default. Use `--force-download` to
refresh from public sources, and use `--allow-partial` only when an incomplete
corpus is acceptable for exploratory work.

For publication-facing claims, first verify the source coverage and row scale:

```sh
.venv/bin/python benchmark.py verify-corpus --corpus data/benchmark-corpus
```

`benchmark.py run`/`all` write the token report to
`reports/benchmark-report.{json,md}` by default (generated output, not checked
in; override with `--json-out`/`--markdown-out`).
