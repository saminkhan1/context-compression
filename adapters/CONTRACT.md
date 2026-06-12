# Adapter Contract

Adapters are host-specific wrappers around the same selector/verifier flow.
They must not implement independent compression logic.

## Required Flow

1. Accept only local file paths from the host agent/tool call.
2. Run `selector.py` with:
   - `--cwd`
   - `--adapter`
   - `--model`
   - `--report-out`
   - `--verify-report`
   - one or more user-selected paths
3. Persist the full `context-selector/v1` report.
4. Return the verified report to the host.
5. Consumers may read `read_path` only after verification succeeds.

## Invariants

- Never rewrite the source file.
- Never trust `output_path` directly; only trust verified `read_path`.
- Preserve `output_sha256` in evidence logs so sidecar tampering is auditable.
- Never optimize remote URLs, chat text, clipboard content, or untrusted web
  content through this tool.
- Never claim token, cost, latency, or quality savings without persisting the
  report.
- Use host-specific naming only in the report `adapter` field.

Codex, Pi, Hermes/MCP, OpenClaw, and generic shells should remain thin adapters
over this same selector/evidence contract.
