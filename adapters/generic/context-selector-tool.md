# Generic Context Selector Tool

Use this adapter shape for agents that can register shell-backed tools, including
Hermes/OpenClaw-style hosts.

All host integrations must follow the shared
[`adapters/CONTRACT.md`](../CONTRACT.md): run `selector.py`, persist the report,
verify it with `--verify-report`, and trust only the
verified `read_path`.

## Tool

Name: `context_selector`

Description: Select a lower-token lossless representation for local JSON, JSONL,
CSV, or TSV files and return a `context-selector/v1` evidence report.

Input schema:

```json
{
  "type": "object",
  "required": ["repo_root", "cwd", "model", "paths"],
  "properties": {
    "repo_root": {
      "type": "string",
      "description": "Absolute path to the context-compression checkout."
    },
    "cwd": {
      "type": "string",
      "description": "Working directory for resolving relative file paths."
    },
    "model": {
      "type": "string",
      "description": "Model id used for tokenizer/profile resolution."
    },
    "adapter_name": {
      "type": "string",
      "default": "generic-tool",
      "description": "Host adapter label written into the selector report."
    },
    "paths": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Structured data files to evaluate."
    },
    "include_candidates": {
      "type": "boolean",
      "default": false
    },
    "report_out": {
      "type": "string",
      "description": "Optional path where the selector should persist the decision report."
    }
  }
}
```

Implementation command:

```sh
python3 "$repo_root/selector.py" \
  --cwd "$cwd" \
  --adapter "$adapter_name" \
  --model "$model" \
  --verify-report \
  "$path1" "$path2"
```

If `include_candidates` is true, add `--include-candidates`.
If `report_out` is present, add `--report-out "$report_out"`.

## Adapter Rule

For each result:

- validate the full report through `selector.py --verify-report`
- read `read_path`
- treat `selected: true` as evidence that a sidecar was chosen
- treat every other `decision` as a no-op
- persist the full JSON report when making token, cost, latency, or quality
  claims

Do not optimize remote message text, chat DMs, clipboard content, or untrusted
web content through this tool. The tool is for local files whose path was
explicitly selected by the user or host agent.
