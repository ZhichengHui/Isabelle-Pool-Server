"""Heap pool (verified per-project heaps) and the stateless header parser."""
from __future__ import annotations

from typing import Any

from ._base import HEAP_GROUPS_URL, HEAPS_URL, ClientBase


class HeapsMixin(ClientBase):
    async def heap_build(
        self, task_group: str, project: str, session_name: str | None = None,
    ) -> dict[str, Any]:
        """Build/rebuild the verified heap for (task_group, project)."""
        payload: dict[str, Any] = {"task_group": task_group, "project": project}
        if session_name is not None:
            payload["session_name"] = session_name
        response = await self._request("POST", f"{HEAPS_URL}/build", json_body=payload)
        response.raise_for_status()
        return response.json()

    async def list_heaps(self, task_group: str | None = None) -> dict[str, Any]:
        url = f"{HEAPS_URL}?task_group={task_group}" if task_group else HEAPS_URL
        return await self._get_json(url)

    async def list_available_heaps(self) -> dict[str, Any]:
        """Every heap image on disk: base session images (e.g. HOL-Analysis)
        and distribution heaps, plus pool-built ones (origin-tagged)."""
        return await self._get_json(f"{HEAPS_URL}/available")

    async def parse_theory_header(self, text: str) -> dict[str, Any]:
        """The server's canonical theory-header parse (comment-stripped,
        header-anchored) — use this instead of a local regex so all consumers
        share one parser. Session-less endpoint: lives at
        /api/v1/parse_theory_header, NOT under /api/v1/sessions/ (posting there
        matched /sessions/{session_id} and returned 405 — caught by the MCP
        smoke on 2026-09-27)."""
        response = await self._request(
            "POST", "/api/v1/parse_theory_header", json_body={"text": text}
        )
        response.raise_for_status()
        return response.json()

    async def get_heap(self, task_group: str, project: str) -> dict[str, Any]:
        """Full manifest. The project path follows the group segment verbatim
        (server uses a :path converter): get_heap("alpha", "/tmp/hp1")."""
        return await self._get_json(f"{HEAPS_URL}/{task_group}/{project}")

    async def delete_heap(self, task_group: str, project: str) -> dict[str, Any]:
        response = await self._request("DELETE", f"{HEAPS_URL}/{task_group}/{project}")
        response.raise_for_status()
        return response.json()

    async def list_heap_groups(self) -> dict[str, Any]:
        return await self._get_json(HEAP_GROUPS_URL)

    async def delete_heap_group(self, task_group: str) -> dict[str, Any]:
        response = await self._request("DELETE", f"{HEAP_GROUPS_URL}/{task_group}")
        response.raise_for_status()
        return response.json()

    async def select_image(
        self, imports: list[str], field: str | None, session_dirs: list[str] | None = None
    ) -> dict:
        """The server's choice of start image for `imports`: {field, from_source, stale}
        (server/app/services/image_select.py `selection`). An older server answers {field} only."""
        response = await self._request(
            "POST", "/api/v1/select_field",
            json_body={"imports": imports, "field": field, "session_dirs": session_dirs or []},
        )
        response.raise_for_status()
        return response.json()

    async def select_field(
        self, imports: list[str], field: str | None, session_dirs: list[str] | None = None
    ) -> str | None:
        """The image the server picks to start on for `imports` so that theories already built
        into an up-to-date image are not checked again (server/app/services/image_select.py)."""
        return (await self.select_image(imports, field, session_dirs)).get("field")
