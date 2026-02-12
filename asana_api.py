# asana_api.py
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Callable

import requests

ASANA_BASE = "https://app.asana.com/api/1.0"


class AsanaClient:
    def __init__(self, access_token: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        self.timeout = timeout

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{ASANA_BASE}{path}"
        r = self.session.get(url, params=params or {}, timeout=self.timeout)

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "2"))
            time.sleep(retry_after)
            r = self.session.get(url, params=params or {}, timeout=self.timeout)

        if r.status_code >= 400:
            try:
                err_json = r.json()
            except Exception:
                err_json = {"raw_text": r.text[:500]}

            request_id = r.headers.get("X-Request-Id") or r.headers.get("x-request-id")
            raise requests.HTTPError(
                f"Asana API error {r.status_code} on {path} | request_id={request_id} | detail={err_json}",
                response=r,
            )

        return r.json()

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        p = dict(params or {})
        p.setdefault("limit", 100)  # evita "result too large"

        while True:
            payload = self._get(path, p)
            items.extend(payload.get("data", []))

            next_page = payload.get("next_page")
            if not next_page or not next_page.get("offset"):
                break

            p["offset"] = next_page["offset"]

        return items

    # --- Health check ---
    def get_me(self) -> Dict[str, Any]:
        return self._get(
            "/users/me",
            params={"opt_fields": "gid,name,email,workspaces.gid,workspaces.name"},
        )

    def get_user(self, user_gid: str) -> Dict[str, Any]:
        return self._get(
            f"/users/{user_gid}",
            params={"opt_fields": "gid,name,email,workspaces.gid"},
        )

    def list_projects(self, workspace_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            "/projects",
            params={
                "workspace": workspace_gid,
                "opt_fields": "gid,name,archived,permalink_url",
                "limit": 100,
            },
        )

    def _task_opt_fields(self) -> str:
        # Campos extra que pediste: created_at, memberships (para proyecto/medio),
        # y custom_fields (para "Medios | Tiempo de tarea")
        return ",".join(
            [
                "gid",
                "name",
                "created_at",
                "completed",
                "completed_at",
                "due_on",
                "due_at",
                "assignee.gid",
                "assignee.name",
                "permalink_url",
                "parent.gid",
                "parent.name",
                "memberships.project.gid",
                "memberships.project.name",
                "memberships.section.gid",
                "memberships.section.name",
                "custom_fields.gid",
                "custom_fields.name",
                "custom_fields.type",
                "custom_fields.display_value",
                "custom_fields.number_value",
                "custom_fields.text_value",
                "custom_fields.enum_value.name",
            ]
        )

    def list_project_tasks(self, project_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            f"/projects/{project_gid}/tasks",
            params={
                "limit": 100,
                "opt_fields": self._task_opt_fields(),
            },
        )

    def list_subtasks(self, task_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            f"/tasks/{task_gid}/subtasks",
            params={
                "limit": 100,
                "opt_fields": self._task_opt_fields(),
            },
        )

    def search_tasks_for_workspace(self, workspace_gid: str, search_params: Dict[str, Any]) -> List[Dict[str, Any]]:
        params = dict(search_params)
        params.setdefault("limit", 100)
        params.setdefault("opt_fields", self._task_opt_fields())
        return self.paginate(f"/workspaces/{workspace_gid}/tasks/search", params=params)


ProgressCb = Optional[Callable[[Dict[str, Any]], None]]


def build_task_tree(
    client: AsanaClient,
    root_tasks: List[Dict[str, Any]],
    max_depth: int = 3,
    sleep_ms: int = 0,
    progress_cb: ProgressCb = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    visited = 0
    queued = len(root_tasks)

    def emit(current_name: str, depth: int):
        if callable(progress_cb):
            progress_cb(
                {
                    "visited": visited,
                    "queued": queued,
                    "depth": depth,
                    "current_name": current_name,
                }
            )

    def walk(task: Dict[str, Any], level: int, root: Dict[str, Any]) -> None:
        nonlocal visited, queued

        visited += 1

        enriched = dict(task)
        enriched["level"] = level
        enriched["root_gid"] = root.get("gid")
        enriched["root_name"] = root.get("name")
        out.append(enriched)

        emit(str(task.get("name", "")), level)

        if level + 1 >= max_depth:
            return

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

        subs = client.list_subtasks(task["gid"])
        queued += len(subs)
        for st in subs:
            walk(st, level + 1, root)

    for t in root_tasks:
        walk(t, 0, t)

    return out
