# Harness Contract Verification

Local smokes prove this repository's selector and adapter-shaped call paths.
They do not prove that Codex, Claude Code, Hermes Agent, or OpenClaw still
expose the host surfaces our adapters rely on. Run the source-contract verifier
whenever adapter glue, install docs, or compatibility claims change.

## Sources Checked

- Codex: local `openai/codex` checkout, hook parser, and Bash updated-input
  path.
- Claude Code: installed `claude` binary, checked for the hook payload and
  response fields used by `adapters/claude-code/`.
- Hermes Agent: local `NousResearch/hermes-agent` checkout, plugin tool
  override support, built-in `read_file`, and stdio MCP client.
- OpenClaw: local `openclaw/openclaw` checkout, plugin entry and
  `api.registerTool` surface.

## Reproduce

Use local checkouts where available. The defaults match the verified workspace
used for this repo:

- Codex: `/Users/saminkhan1/Documents/Codex/2026-05-20/i-have-an-idea-so-when/codex-src`
- Hermes Agent: `~/.hermes/hermes-agent`
- OpenClaw: `/tmp/context-compression-upstream-openclaw`
- Claude Code: first `claude` executable on `PATH`

Run:

```sh
.venv/bin/python scripts/verify_harness_contracts.py
```

Override paths explicitly when checking a different checkout:

```sh
.venv/bin/python scripts/verify_harness_contracts.py \
  --codex-root /path/to/openai/codex \
  --hermes-root /path/to/hermes-agent \
  --openclaw-root /path/to/openclaw \
  --claude-bin /path/to/claude
```

Expected output:

```text
harness source contracts ok
```

## Current Findings

- Codex supports `PreToolUse` `updatedInput` when the hook response allows the
  call, and the Bash handler exposes the command string for replacement. This
  matches `hook.py`.
- Claude Code exposes `PreToolUse`, `tool_input`, `permissionDecision`, and
  `updatedInput` in the installed runtime. This matches the Read and Bash hook
  adapter in `adapters/claude-code/`.
- Hermes Agent plugins can register tools and intentionally override existing
  tools with `override=True`; the registry exposes the original `read_file`
  entry. Hermes also reads stdio MCP server configuration from
  `~/.hermes/config.yaml`. This matches `adapters/hermes-plugin/` and
  `adapters/mcp/context_selector_server.py`.
- OpenClaw exposes `definePluginEntry` and `api.registerTool`. Current source
  does not expose the transparent `before_tool_call` plugin interception surface
  this adapter previously claimed. The OpenClaw adapter is therefore an
  explicit optional `context_selector` tool only.

If this check fails, local harness smokes are not sufficient evidence. Update
the adapter and docs against the host source first, then rerun the unit suite
and smokes.
