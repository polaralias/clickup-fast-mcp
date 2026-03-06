# clickup-fast-mcp

Native Python/FastMCP implementation of the ClickUp MCP server.

## Runtime model

- Tool surface is loaded from `tool_manifest_clickup.json` (parity names + input schemas)
- Tool execution is handled directly in Python over ClickUp APIs (`v2` + `v3`)
- No upstream sibling repo is required at runtime

## Project configuration (`fastmcp.json`)

This repository now includes a canonical `fastmcp.json` aligned with FastMCP project configuration docs:

- `source`: `server.py:mcp`
- `environment`: uv-managed Python environment from local `pyproject.toml`
- `deployment`: HTTP runtime defaults (`/mcp`) plus runtime env wiring

FastMCP CLI arguments still override config values when needed.

## Runtime env

- Required:
  - `CLICKUP_API_TOKEN` (or `apiKey` / `API_KEY`)
  - `TEAM_ID` for tools that need a workspace/team default

- Optional:
  - `MCP_API_KEY` or `MCP_API_KEYS` for HTTP bearer auth
  - `CLICKUP_HTTP_TIMEOUT_MS` (default `30000`)
  - `BASE_URL` (if needed for token verifier metadata)

## Validate and run

```bash
# Validate tool discovery / entrypoint
fastmcp inspect fastmcp.json
fastmcp inspect server.py:mcp

# Run from project config
fastmcp run

# Override transport at runtime
fastmcp run --transport stdio
```
