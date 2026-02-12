# asana_api.py
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

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

        # Rate limit handling
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

        # IMPORTANT: force pagination from the first request
        p.setdefault("limit", 100)

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

    def list_projects(self, workspace_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            "/projects",
            params={
                "workspace": workspace_gid,
                "opt_fields": "gid,name,archived,permalink_url",
                "limit": 100,
            },
        )

    def list_project_tasks(self, project_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            f"/projects/{project_gid}/tasks",
            params={
                "limit": 100,
                "opt_fields": ",".join(
                    [
                        "gid",
                        "name",
                        "completed",
                        "completed_at",
                        "due_on",
                        "due_at",
                        "assignee.gid",
                        "assignee.name",
                        "permalink_url",
                        "parent.gid",
                        "parent.name",
                    ]
                ),
            },
        )

    def list_subtasks(self, task_gid: str) -> List[Dict[str, Any]]:
        return self.paginate(
            f"/tasks/{task_gid}/subtasks",
            params={
                "limit": 100,
                "opt_fields": ",".join(
                    [
                        "gid",
                        "name",
                        "completed",
                        "completed_at",
                        "due_on",
                        "due_at",
                        "assignee.gid",
                        "assignee.name",
                        "permalink_url",
                        "parent.gid",
                        "parent.name",
                    ]
                ),
            },
        )


def build_task_tree(
    client: AsanaClient,
    root_tasks: List[Dict[str, Any]],
    max_depth: int = 3,
    sleep_ms: int = 0,
) -> List[Dict[str, Any]]:
    """
    Devuelve una lista plana con todas las tareas + subtareas hasta max_depth.
    Agrega: level (0..), root_gid, root_name.
    """
    out: List[Dict[str, Any]] = []

    def walk(task: Dict[str, Any], level: int, root: Dict[str, Any]) -> None:
        enriched = dict(task)
        enriched["level"] = level
        enriched["root_gid"] = root.get("gid")
        enriched["root_name"] = root.get("name")
        out.append(enriched)

        if level + 1 >= max_depth:
            return

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

        subs = client.list_subtasks(task["gid"])
        for st in subs:
            walk(st, level + 1, root)

    for t in root_tasks:
        walk(t, 0, t)

    return out
