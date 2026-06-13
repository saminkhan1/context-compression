# Usage Analytics

The lean analytics path is to aggregate verified `context-selector/v1` reports
that the selector and hooks already write. The hot hook path should not send
network telemetry: context reads must keep failing closed, stay deterministic,
and avoid leaking local paths or source content.

## Local Export

Successful Codex `PreToolUse` rewrites write reports under:

```text
.codex/context-cache/reports/
```

Export usage stats:

```sh
.venv/bin/python scripts/export_usage_stats.py --cwd "$PWD"
```

The exporter emits `context-selector-usage/v1` JSON with:

- aggregate files, selected files, raw tokens, selected tokens, saved tokens,
  and savings ratio
- breakdowns by adapter, selected format, source kind, and model
- per-result events with source hash, output hash, original kind, selected
  format, token counts, saved tokens, and savings ratio

Paths and source file names are redacted by default. For local debugging only:

```sh
.venv/bin/python scripts/export_usage_stats.py --cwd "$PWD" --include-paths
```

## Product Export Boundary

If product telemetry is added later, send only the output of this exporter by
explicit opt-in. Do not send raw source content, optimized sidecar content, or
absolute local paths by default. The useful product fields are already enough
for adoption and ROI stats:

- adapter and model
- source format to selected format
- exact versus estimated token counter label
- raw tokens, selected tokens, saved tokens, and percentage saved
- source and output hashes for deduplication without content upload
