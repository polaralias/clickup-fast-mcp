# clickup-fast-mcp

FastMCP Python server for ClickUp, implemented as a parity proxy over the existing `clickup-mcp` runtime.

## What is migrated

- Full ClickUp MCP tool surface from `clickup-mcp`
- Existing ClickUp auth/config env model (the legacy runtime still owns tool execution)
- No UI routes or OAuth web pages

## Configuration

This server forwards through `scripts/clickup_stdio_bridge.mjs`, which boots the
ClickUp MCP runtime.

Legacy backend resolution order:

1. `CLICKUP_LEGACY_REPO` (if set)
2. `../clickup-mcp` (sibling repo)
3. Auto-bootstrap clone into `.vendor/clickup-mcp` (default enabled)

- `CLICKUP_LEGACY_REPO` (optional): absolute path to `clickup-mcp`
- `CLICKUP_AUTO_BOOTSTRAP` (optional, default `true`): disable with `false`
- `CLICKUP_LEGACY_REPO_URL` (optional): override bootstrap git URL
- `CLICKUP_NODE_BIN` / `NODE_BIN` (optional): Node executable
- All existing `clickup-mcp` env vars are supported and passed through unchanged

Auto-bootstrap requires `git` and `npm` in the runtime image.

Optional FastMCP bearer auth for HTTP transport:

- `MCP_API_KEY` (single key), or
- `MCP_API_KEYS` (comma-separated)

## Run

```bash
# HTTP (default)
python server.py

# stdio
FASTMCP_TRANSPORT=stdio python server.py
```
