"""The header-derived context a file's session starts with: its imports, the image (field) to
start on and the extra session directories (moved out of pool.py, which re-exports these names)."""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from client.async_client import PoolAsyncClient

from .config import Config
from .project_sessions import project_context
from .sibling_imports import master_dir_of, resolve_sibling_imports, sibling_imports as _sibling_imports

logger = logging.getLogger(__name__)


async def header_fields(
    client: PoolAsyncClient, text: str, master_dir: Optional[str] = None,
) -> Tuple[List[str], Optional[str]]:
    """(imports, suggested_field) of a full .thy source's header.

    Asks the server's canonical parser (`POST /api/v1/parse_theory_header`,
    comments stripped first so a leading `(* TASK: ... *)` comment can never
    pollute the imports — isabelle-pool-server-header-imports-issue.md). Going through
    the endpoint keeps the MCP free of server code and guarantees it parses
    headers, and picks the session image for them, exactly as the server it
    talks to does (`suggested_field`: the largest distribution image named)."""
    resp = await client.parse_theory_header(text)
    raw = list(resp.get("imports") or [])
    imports = resolve_sibling_imports(raw, master_dir)
    suggested = resp.get("suggested_field") or None
    if imports != raw:
        # The siblings' own imports decide the image too: a sibling importing
        # "S.T" needs the image of S, or the wrapper loads S's theories from
        # source on the default image (minutes to hours). Only the field choice
        # sees them; the wrapper still imports just the siblings.
        try:
            deeper = await _sibling_imports(client, imports, set())
            if deeper:
                merged = await client.parse_theory_header(header_text(raw + deeper))
                suggested = merged.get("suggested_field") or suggested
        except Exception:  # noqa: BLE001  (the field is a hint; the header's own stands)
            pass
    return imports, suggested


async def header_imports(client: PoolAsyncClient, text: str) -> List[str]:
    """Import names from a full .thy source's header (quotes stripped)."""
    imports, _ = await header_fields(client, text)
    return imports


def header_text(imports: List[str]) -> str:
    """A one-line header naming `imports`, for the server's parser when a tool
    gets an import list instead of a source (isabelle_run_code)."""
    return "theory Scratch imports " + " ".join(f'"{i}"' for i in imports) + " begin"


def field_for(suggested: Optional[str]) -> str:
    """The field a binding or scratch session starts on: the server's
    suggestion when Config.FIELD_FROM_IMPORTS is on and the header named a
    session image, else Config.DEFAULT_FIELD (ISABELLE_MCP_LSP_FIELD, "HOL":
    what the client sent on its own before the knob existed). An image the
    server lacks fails the acquire until it is built."""
    if Config.FIELD_FROM_IMPORTS and suggested:
        return suggested
    return Config.DEFAULT_FIELD


async def file_context(
    client: PoolAsyncClient, text: str, file_path: str, heap_session: Optional[str] = None,
) -> Tuple[List[str], str, Tuple[str, ...]]:
    """(imports, field, session_dirs) for `file_path`: imports naming sessions of the file's own
    project (project_sessions.py) give its ROOT directories and the image it builds on, else
    field_for(the server's suggestion); then the server may switch to an up-to-date image that
    already contains the imported theories (image_select.py)."""
    imports, suggested = await header_fields(client, text, master_dir_of(file_path))
    if heap_session:  # a heap-pool session has its own image and directories
        return imports, field_for(suggested), ()
    field, dirs = project_context(imports, file_path) if Config.PROJECT_SESSIONS else (None, ())
    field = field_for(suggested) if field is None else field
    if Config.SELECT_FIELD and imports:
        try:
            field = await client.select_field(imports, field, list(dirs)) or field
        except Exception as e:  # an older server without the endpoint: keep the field
            logger.debug("select_field unavailable: %s", e)
    return imports, field, dirs
