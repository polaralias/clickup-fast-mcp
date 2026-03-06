
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.tools import FunctionTool
from starlette.responses import JSONResponse

BASE_V2 = "https://api.clickup.com/api/v2/"
BASE_V3 = "https://api.clickup.com/api/v3/"
RETRY_STATUS = {429, 500, 502, 503, 504}
RUNTIME_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _runtime_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        cleaned = value.strip()
        if not cleaned or RUNTIME_PLACEHOLDER_RE.fullmatch(cleaned):
            continue
        return cleaned
    return default


class StaticApiKeyVerifier(TokenVerifier):
    def __init__(self, api_keys: Iterable[str], base_url: str | None = None) -> None:
        super().__init__(base_url=base_url)
        self._api_keys = [key for key in api_keys if key]

    async def verify_token(self, token: str) -> AccessToken | None:
        for key in self._api_keys:
            if secrets.compare_digest(token, key):
                return AccessToken(token=token, client_id="clickup-fast-mcp", scopes=[])
        return None


class ClickUpClient:
    def __init__(self, token: str, timeout_ms: int = 30000) -> None:
        self._token = token.strip()
        self._timeout = timeout_ms / 1000
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if self._token:
            self._session.headers["Authorization"] = self._token

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        use_v3: bool = False,
        files: dict[str, Any] | None = None,
    ) -> Any:
        if not self._token:
            raise ValueError("CLICKUP_API_TOKEN (or apiKey/API_KEY) is required")
        url = (BASE_V3 if use_v3 else BASE_V2) + path.lstrip("/")
        clean_params: dict[str, Any] = {}
        for k, v in (params or {}).items():
            if v is None:
                continue
            clean_params[k] = v

        for attempt in range(4):
            response = self._session.request(
                method,
                url,
                params=clean_params,
                json=body if files is None else None,
                data=body if files is not None else None,
                files=files,
                timeout=self._timeout,
            )
            if response.status_code in RETRY_STATUS and attempt < 3:
                time.sleep((2**attempt) * 0.25)
                continue
            if not response.ok:
                detail = response.text
                raise RuntimeError(f"ClickUp {response.status_code}: {detail}")
            if response.status_code == 204:
                return None
            ctype = response.headers.get("content-type", "")
            if "application/json" in ctype:
                return response.json()
            return response.text
        raise RuntimeError("Unexpected ClickUp retry state")

    def request(self, path: str, **kwargs: Any) -> Any:
        return self._request(path, **kwargs)

    def request_v3(self, path: str, **kwargs: Any) -> Any:
        kwargs["use_v3"] = True
        return self._request(path, **kwargs)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _load_manifest() -> list[dict[str, Any]]:
    path = _repo_root() / "tool_manifest_clickup.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing tool manifest: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("tool_manifest_clickup.json is invalid")
    return tools


def _load_api_keys() -> list[str]:
    keys: list[str] = []
    single = _runtime_env("MCP_API_KEY")
    if single:
        keys.append(single)
    multi = _runtime_env("MCP_API_KEYS")
    if multi:
        keys.extend([x.strip() for x in multi.split(",") if x.strip()])
    return list(dict.fromkeys(keys))


def _clickup_token() -> str:
    return _runtime_env("CLICKUP_API_TOKEN", "clickupApiToken", "apiKey", "API_KEY")


def _team_id(default: str | None = None) -> str:
    return (
        (default or "").strip()
        or _runtime_env("TEAM_ID", "teamId", "DEFAULT_TEAM_ID", "defaultTeamId")
    )


def _confirm_required(args: dict[str, Any]) -> None:
    if args.get("dryRun"):
        return
    confirm = args.get("confirm")
    if confirm in (True, "yes", "true", "TRUE", "YES"):
        return
    raise ValueError("Destructive operation requires confirm='yes' or dryRun=true")


def _to_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        if value.isdigit():
            return int(value)
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


class ClickUpRuntime:
    def __init__(self, client: ClickUpClient, manifest: list[dict[str, Any]]) -> None:
        self._client = client
        self._manifest = manifest

    def _workspace_id(self, args: dict[str, Any]) -> str:
        wid = str(args.get("workspaceId") or args.get("teamId") or _team_id()).strip()
        if not wid:
            raise ValueError("workspace/team id is required")
        return wid

    def _resolve_path(self, path: list[str]) -> dict[str, Any]:
        if not path:
            raise ValueError("path must include at least a workspace name")
        workspaces = self._client.request("team").get("teams", [])
        ws = next((x for x in workspaces if str(x.get("name", "")).lower() == str(path[0]).lower()), None)
        if not ws:
            raise ValueError(f"Workspace '{path[0]}' not found")
        result: dict[str, Any] = {"workspaceId": str(ws.get("id")), "workspaceName": ws.get("name")}
        if len(path) == 1:
            return result

        spaces = self._client.request(f"team/{result['workspaceId']}/space").get("spaces", [])
        space = next((x for x in spaces if str(x.get("name", "")).lower() == str(path[1]).lower()), None)
        if not space:
            raise ValueError(f"Space '{path[1]}' not found")
        result["spaceId"] = str(space.get("id"))
        result["spaceName"] = space.get("name")
        if len(path) == 2:
            return result

        folders = self._client.request(f"space/{result['spaceId']}/folder").get("folders", [])
        folder = next((x for x in folders if str(x.get("name", "")).lower() == str(path[2]).lower()), None)
        if folder:
            result["folderId"] = str(folder.get("id"))
            result["folderName"] = folder.get("name")
            if len(path) == 3:
                return result
            lists = self._client.request(f"folder/{result['folderId']}/list").get("lists", [])
            lst = next((x for x in lists if str(x.get("name", "")).lower() == str(path[3]).lower()), None)
            if lst:
                result["listId"] = str(lst.get("id"))
                result["listName"] = lst.get("name")
                return result
            raise ValueError(f"List '{path[3]}' not found")

        lists = self._client.request(f"space/{result['spaceId']}/list").get("lists", [])
        lst = next((x for x in lists if str(x.get("name", "")).lower() == str(path[2]).lower()), None)
        if lst:
            result["listId"] = str(lst.get("id"))
            result["listName"] = lst.get("name")
            return result
        raise ValueError(f"Folder/List '{path[2]}' not found")

    def _task_id(self, args: dict[str, Any]) -> str:
        tid = str(args.get("taskId") or "").strip()
        if tid:
            return tid
        task_name = str(args.get("taskName") or "").strip()
        if not task_name:
            raise ValueError("taskId or taskName required")
        context = args.get("context") or {}
        for item in context.get("tasks", []) if isinstance(context, dict) else []:
            if str(item.get("name", "")).lower() == task_name.lower() and item.get("id"):
                return str(item["id"])
        raise ValueError("Unable to resolve taskName from context; provide taskId")

    def _upload_from_data_uri(self, data_uri: str) -> tuple[bytes, str]:
        match = re.match(r"^data:([^;]+);base64,(.+)$", data_uri)
        if not match:
            raise ValueError("Invalid dataUri")
        mime = match.group(1)
        raw = base64.b64decode(match.group(2))
        return raw, mime

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "ping":
            return {"message": args.get("message", "pong")}
        if name == "health":
            return {
                "status": "ok",
                "server": "clickup-fast-mcp",
                "transport": _runtime_env("FASTMCP_TRANSPORT", default="streamable-http"),
            }
        if name == "tool_catalogue":
            return {"tools": self._manifest}
        if name == "workspace_capability_snapshot":
            wid = self._workspace_id(args)
            docs = True
            try:
                self._client.request_v3(f"workspaces/{wid}/docs", params={"limit": 1})
            except Exception:
                docs = False
            return {"workspaceId": wid, "docsAvailable": docs}

        if name == "workspace_list":
            return self._client.request("team")
        if name == "space_list_for_workspace":
            return self._client.request(f"team/{args['workspaceId']}/space")
        if name == "folder_list_for_space":
            return self._client.request(f"space/{args['spaceId']}/folder")
        if name == "list_list_for_space_or_folder":
            if args.get("folderId"):
                return self._client.request(f"folder/{args['folderId']}/list")
            if args.get("spaceId"):
                return self._client.request(f"space/{args['spaceId']}/list")
            raise ValueError("spaceId or folderId required")
        if name == "hierarchy_resolve_path":
            return self._resolve_path(args["path"])
        if name == "workspace_overview":
            wid = args.get("workspaceId") or self._workspace_id(args)
            spaces = self._client.request(f"team/{wid}/space").get("spaces", [])
            return {"workspaceId": wid, "spaces": spaces, "spaceCount": len(spaces)}
        if name == "workspace_hierarchy":
            wid = self._workspace_id(args)
            spaces = self._client.request(f"team/{wid}/space").get("spaces", [])
            hierarchy: list[dict[str, Any]] = []
            for space in spaces[: int(args.get("maxSpacesPerWorkspace") or len(spaces))]:
                entry = {"space": space, "folders": [], "lists": []}
                sid = str(space.get("id"))
                folders = self._client.request(f"space/{sid}/folder").get("folders", [])
                for folder in folders[: int(args.get("maxFoldersPerSpace") or len(folders))]:
                    fid = str(folder.get("id"))
                    flists = self._client.request(f"folder/{fid}/list").get("lists", [])
                    entry["folders"].append({"folder": folder, "lists": flists[: int(args.get("maxListsPerFolder") or len(flists))]})
                slists = self._client.request(f"space/{sid}/list").get("lists", [])
                entry["lists"] = slists[: int(args.get("maxListsPerSpace") or len(slists))]
                hierarchy.append(entry)
            return {"workspaceId": wid, "hierarchy": hierarchy}
        if name in {"member_list_for_workspace", "member_resolve", "member_search_by_name", "task_assignee_resolve"}:
            team_id = str(args.get("teamId") or self._workspace_id(args))
            data = self._client.request(f"team/{team_id}/member")
            members = data.get("members") or data.get("team_members") or []
            if name == "member_list_for_workspace":
                return {"teamId": team_id, "members": members}
            if name == "member_search_by_name":
                q = str(args.get("query") or "").lower()
                limit = int(args.get("limit") or 10)
                results = [m for m in members if q in str(m.get("username", "")).lower() or q in str(m.get("email", "")).lower()]
                return {"teamId": team_id, "results": results[:limit]}
            identifiers = [str(x).lower() for x in (args.get("identifiers") or [])]
            resolved = []
            for ident in identifiers:
                for m in members:
                    values = {str(m.get("id", "")).lower(), str(m.get("username", "")).lower(), str(m.get("email", "")).lower()}
                    if ident in values:
                        resolved.append(m)
                        break
            return {"teamId": team_id, "resolved": resolved}

        if name == "space_tag_list":
            return self._client.request(f"space/{args['spaceId']}/tag")
        if name == "space_tag_create":
            _confirm_required(args)
            if args.get("dryRun"):
                return {"dryRun": True, "operation": "space_tag_create", "input": args}
            body = {k: v for k, v in {"tag": args.get("name"), "tag_bg": args.get("backgroundColor"), "tag_fg": args.get("foregroundColor")}.items() if v is not None}
            return self._client.request(f"space/{args['spaceId']}/tag", method="POST", body=body)
        if name == "space_tag_update":
            _confirm_required(args)
            if args.get("dryRun"):
                return {"dryRun": True, "operation": "space_tag_update", "input": args}
            current = args.get("currentName") or args.get("name")
            body = {k: v for k, v in {"tag": args.get("name"), "tag_bg": args.get("backgroundColor"), "tag_fg": args.get("foregroundColor")}.items() if v is not None}
            return self._client.request(f"space/{args['spaceId']}/tag/{current}", method="PUT", body=body)
        if name == "space_tag_delete":
            _confirm_required(args)
            if args.get("dryRun"):
                return {"dryRun": True, "operation": "space_tag_delete", "input": args}
            return self._client.request(f"space/{args['spaceId']}/tag/{args['name']}", method="DELETE")

        if name in {"folder_create_in_space", "folder_update", "folder_delete", "list_create_for_container", "list_create_from_template", "list_update", "list_delete", "list_view_create", "space_view_create", "view_update", "view_delete"}:
            _confirm_required(args)
            if args.get("dryRun"):
                return {"dryRun": True, "operation": name, "input": args}
            if name == "folder_create_in_space":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "statuses": args.get("statuses")}.items() if v is not None}
                return self._client.request(f"space/{args['spaceId']}/folder", method="POST", body=body)
            if name == "folder_update":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "statuses": args.get("statuses")}.items() if v is not None}
                return self._client.request(f"folder/{args['folderId']}", method="PUT", body=body)
            if name == "folder_delete":
                return self._client.request(f"folder/{args['folderId']}", method="DELETE")
            if name == "list_create_for_container":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "statuses": args.get("statuses")}.items() if v is not None}
                if args.get("folderId"):
                    return self._client.request(f"folder/{args['folderId']}/list", method="POST", body=body)
                return self._client.request(f"space/{args['spaceId']}/list", method="POST", body=body)
            if name == "list_create_from_template":
                body = {"name": args.get("name"), "use_template_options": bool(args.get("useTemplateOptions"))}
                if args.get("folderId"):
                    return self._client.request(f"folder/{args['folderId']}/list/template/{args['templateId']}", method="POST", body=body)
                return self._client.request(f"space/{args['spaceId']}/list/template/{args['templateId']}", method="POST", body=body)
            if name == "list_update":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "statuses": args.get("statuses")}.items() if v is not None}
                return self._client.request(f"list/{args['listId']}", method="PUT", body=body)
            if name == "list_delete":
                return self._client.request(f"list/{args['listId']}", method="DELETE")
            if name == "list_view_create":
                body = {k: v for k, v in {"name": args.get("name"), "type": args.get("viewType"), "description": args.get("description"), "filters": args.get("filters")}.items() if v is not None}
                return self._client.request(f"list/{args['listId']}/view", method="POST", body=body)
            if name == "space_view_create":
                body = {k: v for k, v in {"name": args.get("name"), "type": args.get("viewType"), "description": args.get("description"), "filters": args.get("filters")}.items() if v is not None}
                return self._client.request(f"space/{args['spaceId']}/view", method="POST", body=body)
            if name == "view_update":
                body = {k: v for k, v in {"name": args.get("name"), "type": args.get("viewType"), "description": args.get("description"), "filters": args.get("filters")}.items() if v is not None}
                return self._client.request(f"view/{args['viewId']}", method="PUT", body=body)
            if name == "view_delete":
                return self._client.request(f"view/{args['viewId']}", method="DELETE")

        if name == "reference_link_list":
            import requests as _r
            html = _r.get("https://clickup.com/api", timeout=10).text
            links = []
            for href, label in re.findall(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.I | re.S):
                clean = re.sub(r"<[^>]+>", " ", label)
                clean = re.sub(r"\s+", " ", clean).strip()
                if not clean:
                    continue
                if href.startswith("/"):
                    href = "https://clickup.com" + href
                if href.startswith("https://clickup.com/api"):
                    links.append({"url": href, "label": clean})
            dedup = []
            seen = set()
            for item in links:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    dedup.append(item)
            return {"links": dedup[: int(args.get("limit") or 50)]}
        if name == "reference_page_fetch":
            import requests as _r
            url = str(args["url"])
            if not url.startswith("https://clickup.com/api"):
                raise ValueError("Only clickup.com/api reference URLs are supported")
            html = _r.get(url, timeout=10).text
            text = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
            text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            limit = int(args.get("maxCharacters") or 6000)
            return {"source": url, "body": text[:limit], "truncated": len(text) > limit}
        if name in {"task_create", "subtask_create", "task_update", "task_delete", "task_duplicate", "task_comment_add", "task_attachment_add", "task_tag_add", "task_tag_remove", "task_create_bulk", "subtask_create_bulk", "task_update_bulk", "task_delete_bulk", "task_tag_add_bulk", "task_search", "task_search_fuzzy", "task_search_fuzzy_bulk", "task_status_report", "task_risk_report", "task_read", "task_list_for_list", "task_comment_list", "list_custom_field_list", "task_custom_field_set_value", "task_custom_field_clear_value"}:
            if name in {"task_create", "subtask_create", "task_update", "task_delete", "task_duplicate", "task_comment_add", "task_attachment_add", "task_tag_add", "task_tag_remove", "task_create_bulk", "subtask_create_bulk", "task_update_bulk", "task_delete_bulk", "task_tag_add_bulk", "task_custom_field_set_value", "task_custom_field_clear_value"}:
                _confirm_required(args)
                if args.get("dryRun"):
                    return {"dryRun": True, "operation": name, "input": args}
            if name == "task_create":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "status": args.get("status"), "priority": args.get("priority"), "assignees": args.get("assigneeIds"), "tags": args.get("tags"), "due_date": _to_epoch_ms(args.get("dueDate"))}.items() if v is not None}
                return self._client.request(f"list/{args['listId']}/task", method="POST", body=body)
            if name == "subtask_create":
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "status": args.get("status"), "priority": args.get("priority"), "assignees": args.get("assigneeIds"), "tags": args.get("tags"), "due_date": _to_epoch_ms(args.get("dueDate")), "parent": args.get("parentTaskId")}.items() if v is not None}
                return self._client.request(f"list/{args['listId']}/task", method="POST", body=body)
            if name == "task_update":
                task_id = self._task_id(args)
                body = {k: v for k, v in {"name": args.get("name"), "description": args.get("description"), "status": args.get("status"), "priority": args.get("priority"), "assignees": args.get("assigneeIds"), "parent": args.get("parentTaskId"), "due_date": _to_epoch_ms(args.get("dueDate"))}.items() if v is not None}
                return self._client.request(f"task/{task_id}", method="PUT", body=body)
            if name == "task_delete":
                return self._client.request(f"task/{args['taskId']}", method="DELETE")
            if name == "task_duplicate":
                body = {k: v for k, v in {"list_id": args.get("listId"), "include_assignees": args.get("includeAssignees"), "include_checklists": args.get("includeChecklists")}.items() if v is not None}
                return self._client.request(f"task/{args['taskId']}/duplicate", method="POST", body=body)
            if name == "task_comment_add":
                return self._client.request(f"task/{args['taskId']}/comment", method="POST", body={"comment_text": args["comment"]})
            if name == "task_attachment_add":
                raw, mime = self._upload_from_data_uri(args["dataUri"])
                files = {"attachment": (args.get("filename") or "attachment.bin", raw, mime)}
                return self._client.request(f"task/{args['taskId']}/attachment", method="POST", files=files)
            if name == "task_tag_add":
                out = []
                for tag in args.get("tags") or []:
                    out.append(self._client.request(f"task/{args['taskId']}/tag/{tag}", method="POST"))
                return {"results": out}
            if name == "task_tag_remove":
                out = []
                for tag in args.get("tags") or []:
                    out.append(self._client.request(f"task/{args['taskId']}/tag/{tag}", method="DELETE"))
                return {"results": out}
            if name == "task_create_bulk":
                team_id = self._workspace_id(args)
                return self._client.request("task/bulk", method="POST", params={"team_id": team_id}, body={"tasks": args.get("tasks") or []})
            if name == "subtask_create_bulk":
                team_id = self._workspace_id(args)
                return self._client.request("task/bulk", method="POST", params={"team_id": team_id}, body={"tasks": args.get("subtasks") or []})
            if name == "task_update_bulk":
                team_id = self._workspace_id(args)
                return self._client.request("task/bulk", method="PUT", params={"team_id": team_id}, body={"tasks": args.get("tasks") or []})
            if name == "task_delete_bulk":
                team_id = self._workspace_id(args)
                return self._client.request("task/bulk", method="DELETE", params={"team_id": team_id}, body={"task_ids": args.get("tasks") or []})
            if name == "task_tag_add_bulk":
                team_id = self._workspace_id(args)
                return self._client.request("task/tag/bulk", method="POST", params={"team_id": team_id}, body={"operations": args.get("tasks") or []})
            if name == "task_search":
                team_id = self._workspace_id(args)
                params = {"query": args.get("query"), "page": args.get("page"), "order_by": "updated", "reverse": True, "subtasks": args.get("includeSubtasks"), "include_timl": args.get("includeTasksInMultipleLists")}
                if args.get("statuses"):
                    params["statuses"] = args["statuses"]
                if args.get("status"):
                    params["statuses"] = [args["status"]]
                if args.get("listIds"):
                    params["list_ids"] = args["listIds"]
                if args.get("tagIds"):
                    params["tags"] = args["tagIds"]
                data = self._client.request(f"team/{team_id}/task", params=params)
                tasks = data.get("tasks", []) if isinstance(data, dict) else []
                limit = int(args.get("pageSize") or len(tasks) or 50)
                return {"tasks": tasks[:limit], "total": len(tasks)}
            if name in {"task_search_fuzzy", "task_search_fuzzy_bulk"}:
                if name == "task_search_fuzzy":
                    return await self.dispatch("task_search", {"query": args.get("query"), "pageSize": args.get("limit", 10), "teamId": _team_id()})
                out = []
                for query in args.get("queries") or []:
                    out.append(await self.dispatch("task_search", {"query": query, "pageSize": args.get("limit", 10), "teamId": _team_id()}))
                return {"queries": out}
            if name in {"task_status_report", "task_risk_report"}:
                search = await self.dispatch("task_search", args)
                tasks = search.get("tasks", [])
                by_status: dict[str, int] = {}
                overdue = 0
                now_ms = int(time.time() * 1000)
                for task in tasks:
                    status = str((task.get("status") or {}).get("status") or task.get("status") or "unknown")
                    by_status[status] = by_status.get(status, 0) + 1
                    due = _to_epoch_ms(task.get("due_date"))
                    if due and due < now_ms and status.lower() not in {"closed", "done", "complete", "completed"}:
                        overdue += 1
                return {"taskCount": len(tasks), "byStatus": by_status, "overdue": overdue}
            if name == "task_read":
                task_id = self._task_id(args)
                return self._client.request(f"task/{task_id}")
            if name == "task_list_for_list":
                data = self._client.request(f"list/{args['listId']}/task", params={"page": args.get("page"), "subtasks": args.get("includeSubtasks"), "include_timl": args.get("includeTasksInMultipleLists")})
                tasks = data.get("tasks", []) if isinstance(data, dict) else []
                limit = int(args.get("limit") or len(tasks) or 100)
                return {"tasks": tasks[:limit], "total": len(tasks)}
            if name == "task_comment_list":
                task_id = self._task_id(args)
                data = self._client.request(f"task/{task_id}/comment")
                comments = data.get("comments", []) if isinstance(data, dict) else []
                limit = int(args.get("limit") or len(comments) or 50)
                return {"comments": comments[:limit]}
            if name == "list_custom_field_list":
                return self._client.request(f"list/{args['listId']}/field")
            if name == "task_custom_field_set_value":
                return self._client.request(f"task/{args['taskId']}/field/{args['fieldId']}", method="POST", body={"value": args.get("value")})
            if name == "task_custom_field_clear_value":
                return self._client.request(f"task/{args['taskId']}/field/{args['fieldId']}", method="DELETE")

        if name in {"doc_create", "doc_list", "doc_read", "doc_pages_read", "doc_page_list", "doc_page_read", "doc_page_create", "doc_page_update", "doc_search", "doc_search_bulk"}:
            if name in {"doc_create", "doc_page_create", "doc_page_update"}:
                _confirm_required(args)
                if args.get("dryRun"):
                    return {"dryRun": True, "operation": name, "input": args}
            workspace_id = str(args.get("workspaceId") or _team_id())
            if name == "doc_create":
                body = {k: v for k, v in {"name": args.get("name"), "content": args.get("content"), "folder_id": args.get("folderId")}.items() if v is not None}
                return self._client.request_v3(f"workspaces/{workspace_id}/docs", method="POST", body=body)
            if name == "doc_list":
                params = {"search": args.get("search"), "limit": args.get("limit"), "page": args.get("page"), "space_id": args.get("spaceId"), "folder_id": args.get("folderId")}
                return self._client.request_v3(f"workspaces/{workspace_id}/docs", params=params)
            if name == "doc_read":
                doc = self._client.request_v3(f"workspaces/{workspace_id}/docs/{args['docId']}")
                if args.get("includePages"):
                    pages = self._client.request_v3(f"docs/{args['docId']}/page_listing")
                    doc["pages"] = pages
                return doc
            if name == "doc_pages_read":
                return self._client.request_v3(f"docs/{args['docId']}/pages/bulk", method="POST", body={"page_ids": args.get("pageIds") or []})
            if name == "doc_page_list":
                return self._client.request_v3(f"docs/{args['docId']}/page_listing")
            if name == "doc_page_read":
                return self._client.request_v3(f"docs/{args['docId']}/pages/{args['pageId']}")
            if name == "doc_page_create":
                body = {k: v for k, v in {"title": args.get("title"), "content": args.get("content"), "parent_id": args.get("parentId"), "position": args.get("position")}.items() if v is not None}
                return self._client.request_v3(f"docs/{args['docId']}/pages", method="POST", body=body)
            if name == "doc_page_update":
                body = {k: v for k, v in {"title": args.get("title"), "content": args.get("content")}.items() if v is not None}
                return self._client.request_v3(f"docs/{args['docId']}/pages/{args['pageId']}", method="PUT", body=body)
            if name == "doc_search":
                return self._client.request_v3(f"workspaces/{workspace_id}/docs", params={"search": args.get("query"), "limit": args.get("limit")})
            if name == "doc_search_bulk":
                return {"queries": [self._client.request_v3(f"workspaces/{workspace_id}/docs", params={"search": q, "limit": args.get("limit")}) for q in (args.get("queries") or [])]}

        if name in {"task_timer_start", "task_timer_stop", "time_entry_create_for_task", "time_entry_update", "time_entry_delete", "task_time_entry_list", "time_entry_current", "time_entry_list", "time_report_for_tag", "time_report_for_container", "time_report_for_context", "time_report_for_space_tag"}:
            if name in {"task_timer_start", "task_timer_stop", "time_entry_create_for_task", "time_entry_update", "time_entry_delete"}:
                _confirm_required(args)
                if args.get("dryRun"):
                    return {"dryRun": True, "operation": name, "input": args}
            if name == "task_timer_start":
                return self._client.request(f"task/{args['taskId']}/time", method="POST", body={"start": int(time.time() * 1000)})
            if name == "task_timer_stop":
                return self._client.request(f"task/{args['taskId']}/time", method="POST", body={"end": int(time.time() * 1000)})
            if name == "time_entry_create_for_task":
                body = {k: v for k, v in {"start": _to_epoch_ms(args.get("start")), "end": _to_epoch_ms(args.get("end")), "duration": args.get("durationMs"), "description": args.get("description")}.items() if v is not None}
                return self._client.request(f"task/{args['taskId']}/time", method="POST", body=body)
            if name == "time_entry_update":
                team_id = self._workspace_id(args)
                body = {k: v for k, v in {"start": _to_epoch_ms(args.get("start")), "end": _to_epoch_ms(args.get("end")), "duration": args.get("durationMs"), "description": args.get("description")}.items() if v is not None}
                return self._client.request(f"team/{team_id}/time_entries/{args['entryId']}", method="PUT", body=body)
            if name == "time_entry_delete":
                team_id = self._workspace_id(args)
                return self._client.request(f"team/{team_id}/time_entries/{args['entryId']}", method="DELETE")
            if name == "task_time_entry_list":
                return self._client.request(f"task/{args['taskId']}/time")
            if name == "time_entry_current":
                team_id = self._workspace_id(args)
                return self._client.request(f"team/{team_id}/time_entries/current")
            if name == "time_entry_list":
                team_id = self._workspace_id(args)
                params = {"start_date": _to_epoch_ms(args.get("from")), "end_date": _to_epoch_ms(args.get("to")), "page": args.get("page")}
                return self._client.request(f"team/{team_id}/time_entries", params=params)
            base = await self.dispatch("time_entry_list", args)
            entries = base.get("data") or base.get("entries") or []
            total = 0
            for entry in entries:
                dur = entry.get("duration") or entry.get("duration_ms") or 0
                try:
                    total += int(dur)
                except Exception:
                    pass
            return {"entries": entries, "entryCount": len(entries), "totalDurationMs": total}

        raise NotImplementedError(f"Tool '{name}' is not implemented")


def _register_tools(server: FastMCP, runtime: ClickUpRuntime, manifest: list[dict[str, Any]]) -> None:
    for spec in manifest:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        params = spec.get("inputSchema") or {"type": "object", "properties": {}, "additionalProperties": True}
        desc = str(spec.get("description") or "")

        async def _fn(_name: str = name, **kwargs: Any) -> dict[str, Any]:
            try:
                return await runtime.dispatch(_name, kwargs)
            except Exception as exc:
                return {"isError": True, "error": str(exc)}

        server.add_tool(
            FunctionTool(
                name=name,
                description=desc,
                parameters=params,
                output_schema={"type": "object", "additionalProperties": True},
                fn=_fn,
            )
        )


manifest = _load_manifest()
client = ClickUpClient(_clickup_token(), timeout_ms=int(_runtime_env("CLICKUP_HTTP_TIMEOUT_MS", default="30000") or "30000"))
runtime = ClickUpRuntime(client, manifest)
api_keys = _load_api_keys()
auth = StaticApiKeyVerifier(api_keys=api_keys, base_url=_runtime_env("BASE_URL")) if api_keys else None
server = FastMCP("clickup-fast-mcp", auth=auth)
mcp = server
_register_tools(server, runtime, manifest)


@server.custom_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_health(_request):
    return JSONResponse({"status": "ok", "server": "clickup-fast-mcp"})


@server.custom_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
async def health(_request):
    return JSONResponse({"status": "ok", "server": "clickup-fast-mcp"})


@server.custom_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def healthz(_request):
    return JSONResponse({"status": "ok", "server": "clickup-fast-mcp"})


def main() -> None:
    transport_name = _runtime_env("FASTMCP_TRANSPORT", default="streamable-http").lower()
    if transport_name == "stdio":
        server.run()
    else:
        host = _runtime_env("HOST", default="0.0.0.0")
        port = int(_runtime_env("PORT", default="8000"))
        server.run(transport=transport_name, host=host, port=port)


if __name__ == "__main__":
    main()
