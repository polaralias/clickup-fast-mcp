from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import AccessToken, TokenVerifier

DEFAULT_LEGACY_REPO_URL = "https://github.com/polaralias/clickup-mcp.git"


class StaticApiKeyVerifier(TokenVerifier):
    def __init__(self, api_keys: Iterable[str], base_url: str | None = None) -> None:
        super().__init__(base_url=base_url)
        self._api_keys = [key for key in api_keys if key]

    async def verify_token(self, token: str) -> AccessToken | None:
        for key in self._api_keys:
            if secrets.compare_digest(token, key):
                return AccessToken(token=token, client_id="clickup-fast-mcp", scopes=[])
        return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def _bootstrap_legacy_repo(target: Path) -> Path:
    if target.exists():
        return target

    if not _parse_bool(os.getenv("CLICKUP_AUTO_BOOTSTRAP"), True):
        raise FileNotFoundError(
            f"ClickUp legacy repo was not found at {target}. "
            "Set CLICKUP_LEGACY_REPO to a valid path or enable CLICKUP_AUTO_BOOTSTRAP."
        )

    git_bin = shutil.which("git")
    npm_bin = shutil.which("npm")
    if not git_bin or not npm_bin:
        raise RuntimeError(
            "Unable to bootstrap clickup-mcp automatically because git/npm are unavailable. "
            "Either install git+npm in the runtime image, or set CLICKUP_LEGACY_REPO."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    repo_url = os.getenv("CLICKUP_LEGACY_REPO_URL", DEFAULT_LEGACY_REPO_URL).strip() or DEFAULT_LEGACY_REPO_URL
    _run([git_bin, "clone", "--depth", "1", repo_url, str(target)], cwd=target.parent)
    _run([npm_bin, "ci", "--omit=dev"], cwd=target)

    entrypoint = target / "dist" / "server" / "index.js"
    if not entrypoint.exists():
        _run([npm_bin, "ci"], cwd=target)
        _run([npm_bin, "run", "build"], cwd=target)
    return target


def _legacy_root() -> Path:
    override = os.getenv("CLICKUP_LEGACY_REPO")
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.exists():
            raise FileNotFoundError(
                f"CLICKUP_LEGACY_REPO is set but does not exist: {candidate}"
            )
        return candidate

    sibling = (_repo_root().parent / "clickup-mcp").resolve()
    if sibling.exists():
        return sibling

    vendored = (_repo_root() / ".vendor" / "clickup-mcp").resolve()
    return _bootstrap_legacy_repo(vendored)


def _load_api_keys() -> list[str]:
    keys: list[str] = []
    single = os.getenv("MCP_API_KEY")
    if single:
        keys.append(single.strip())

    multi = os.getenv("MCP_API_KEYS")
    if multi:
        for raw in multi.split(","):
            token = raw.strip()
            if token:
                keys.append(token)

    return list(dict.fromkeys(keys))


def build_server():
    legacy_root = _legacy_root()
    package_manifest = legacy_root / "package.json"
    bridge_script = _repo_root() / "scripts" / "clickup_stdio_bridge.mjs"
    if not package_manifest.exists() or not bridge_script.exists():
        raise FileNotFoundError(
            "Missing ClickUp bridge prerequisites. Ensure clickup-mcp exists and "
            "scripts/clickup_stdio_bridge.mjs is present."
        )

    env = {key: str(value) for key, value in os.environ.items()}
    env["CLICKUP_LEGACY_REPO"] = str(legacy_root)

    node_bin = os.getenv("CLICKUP_NODE_BIN", os.getenv("NODE_BIN", "node"))
    transport = StdioTransport(
        command=node_bin,
        args=[str(bridge_script)],
        env=env,
        cwd=str(_repo_root()),
    )

    api_keys = _load_api_keys()
    auth = StaticApiKeyVerifier(api_keys=api_keys, base_url=os.getenv("BASE_URL")) if api_keys else None

    return create_proxy(
        transport,
        name="clickup-fast-mcp",
        instructions=(
            "FastMCP proxy for clickup-mcp. All tools are served by the legacy ClickUp MCP runtime "
            "over stdio while preserving its existing environment-based configuration."
        ),
        auth=auth,
    )


server = build_server()


def main() -> None:
    transport_name = os.getenv("FASTMCP_TRANSPORT", "streamable-http").strip().lower()

    if transport_name == "stdio":
        server.run()
    else:
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        server.run(transport=transport_name, host=host, port=port)


if __name__ == "__main__":
    main()
