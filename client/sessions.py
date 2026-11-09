"""Session lifecycle: create / acquire / release / close / enter_theory / listings."""
from __future__ import annotations

from typing import Any

from ._base import BASE_URL, ClientBase


class SessionsMixin(ClientBase):
    async def create_session(
        self,
        theories: list[str] | None = None,
        field: str | None = "HOL",
        label: str | None = None,
        task_group: str | None = None,
        heap_session: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if theories is not None:
            payload["theories"] = theories
        if field is not None:
            payload["field"] = field
        if label is not None:
            payload["label"] = label
        if task_group is not None:
            payload["task_group"] = task_group
        if heap_session is not None:
            payload["heap_session"] = heap_session
        if project is not None:
            payload["project"] = project
        response = await self._request("POST", BASE_URL, json_body=payload)
        response.raise_for_status()
        return response.json()

    async def acquire_session(
        self,
        theories: list[str] | None = None,
        field: str | None = "HOL",
        reuse_dirty: bool = True,
        task_group: str | None = None,
        heap_session: str | None = None,
        project: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"reuse_dirty": reuse_dirty}
        if theories is not None:
            payload["theories"] = theories
        if field is not None:
            payload["field"] = field
        if task_group is not None:
            payload["task_group"] = task_group
        if heap_session is not None:
            payload["heap_session"] = heap_session
        if project is not None:
            payload["project"] = project
        if label is not None:
            payload["label"] = label
        response = await self._request("POST", f"{BASE_URL}/acquire", json_body=payload)
        response.raise_for_status()
        return response.json()

    async def close_session(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "DELETE", f"{BASE_URL}/{session_id}",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def release_session(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/release",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def renew_lease(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Keep a held session's lease alive without using the session."""
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/renew_lease",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        return response.json()

    async def enter_theory(
        self, session_id: str, theory_name: str, *,
        imports: list[str] | None = None, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Enter a theory. If ``imports`` is given, the server begins the theory with a
        correctly-quoted header (no need to send a 'theory ... begin' command yourself)."""
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/enter_theory/{theory_name}",
            json_body={"imports": imports} if imports is not None else None,
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def list_sessions(self) -> dict[str, Any]:
        """All sessions in the server pool: ``{sessions: [...]}`` (no lease required)."""
        return await self._get_json(BASE_URL)

    async def get_history(
        self, session_id: str, limit: int = 50, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Command history: ``{session_id, total_commands, history}``."""
        return await self._get_json(f"{BASE_URL}/{session_id}/history?limit={int(limit)}", lease_id=lease_id)

    async def get_stats(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Per-session stats (command counts, etc.)."""
        return await self._get_json(f"{BASE_URL}/{session_id}/stats", lease_id=lease_id)
