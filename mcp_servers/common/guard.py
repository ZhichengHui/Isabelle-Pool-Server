"""The per-tool guard both MCP servers wrap every tool with (plan §2.C B3).

- an ``httpx.HTTPStatusError`` from the server becomes the structured,
  agent-facing JSON of ``memory_advice.translate_http_error`` instead of the
  bare ``"Server error '503 ...' for url ..."`` text (the 503 body used to be
  lost — research gap G5);
- after a successful call, while the client's last ``X-Memory-State`` is
  warning / critical, a MEMORY REMINDER is attached to the result (owner
  decision D3) and the optional ``on_reminder(state)`` hook runs (the LSP
  server retires idle warm scratch sessions there, by tier — the MCP frees its
  own first).

``functools.wraps`` keeps the tool's signature, docstring and ``Context``
parameter, so FastMCP's schema is unchanged (verified on mcp 1.30).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import httpx

from .memory_advice import ToolNames, reminder, translate_http_error, with_reminder
from .registry import SessionRegistry

OwnerOf = Callable[[Tuple[Any, ...], Dict[str, Any]], Any]


def make_guard(  # pylint: disable=too-many-arguments
    *,
    registry: SessionRegistry,
    tools: ToolNames,
    get_client: Callable[[], Awaitable[Any]],
    owner_of: OwnerOf,
    idle_s: float = 120.0,
    on_reminder: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Build the decorator for one server (its registry, tool names, client)."""

    def guard(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            owner = owner_of(args, kwargs)
            try:
                out = await fn(*args, **kwargs)
            except httpx.HTTPStatusError as exc:
                return json.dumps(
                    translate_http_error(
                        exc, registry, owner, tools=tools, idle_s=idle_s
                    ),
                    default=str,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # A state, not a failure, that a tool reports as its result (e.g. lsp
                # heap_notes.HeapBuilding: the server builds the image the file needs).
                result = getattr(exc, "agent_result", None)
                if result is None:
                    raise
                return json.dumps(result, default=str)
            registry.clear_memory_failures(owner)
            try:
                client = await get_client()
                state = getattr(client, "last_memory_state", "unknown")
            except Exception:  # pylint: disable=broad-exception-caught
                return out
            note = reminder(state, registry.owned(owner), tools=tools, idle_s=idle_s)
            if note is None:
                return out
            if on_reminder is not None:
                try:
                    await on_reminder(
                        state
                    )  # the hook may act by tier (warning / critical)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            return with_reminder(out, note)

        return wrapper

    return guard
