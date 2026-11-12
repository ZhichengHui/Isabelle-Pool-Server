from __future__ import annotations

import re
from typing import Optional


class SessionError(Exception):

    def __init__(
        self,
        message: str | None = None,
        *,
        error: str | None = None,
        execution_time: float | None = None,
    ) -> None:
        detail = error or message or self.__class__.__name__
        super().__init__(detail)
        self.error = detail
        self.execution_time = execution_time


class SessionNotFound(SessionError):
    pass


class ImportsNotCovered(SessionError):
    """enter_theory asked for imports the session's context does not include:
    IPS fixes a session's theory context when it is acquired, and the imports
    of a theory header only gate loading, so the theory would be checked
    without them (docs/ISSUES.md; training-metrics review afd3de9). 409."""

    def __init__(self, uncovered: list[str], session_theories: list[str]) -> None:
        super().__init__(
            error=(
                f"imports not covered by this session's theories {session_theories}: "
                f"{uncovered}; acquire a session with these imports"
            )
        )
        self.uncovered = list(uncovered)
        self.session_theories = list(session_theories)


class HeaderFailed(SessionError):
    """enter_theory's generated header was rejected by Isabelle: an import that
    cannot be loaded, an illegal or malformed name. The client's input, not a
    server failure: 422 (review 83baa0a S1; it used to answer 500)."""


class SessionStartError(SessionError):
    pass


class ImageNotCurrent(SessionError):
    """Session start found its session image missing or not up to date and does not build it
    (ISABELLE_SESSION_START_BUILD=false, the default): the client's state, not a server
    failure. 409; the text names the image and the command that builds it."""


_IMAGE_NOT_CURRENT_RE = re.compile(r"Session image \S+ is missing or not up to date[^\n]*")


def image_not_current_text(message: str) -> Optional[str]:
    """The "Session image X is missing or not up to date ..." sentence of a session-start
    failure message, or None when the failure is something else."""
    m = _IMAGE_NOT_CURRENT_RE.search(message or "")
    return m.group(0) if m and "forbids building" in m.group(0) else None


def raise_if_image_not_current(exc: BaseException) -> None:
    """Raise ImageNotCurrent when `exc` is a session start that refused to build its image."""
    text = image_not_current_text(str(exc))
    if text:
        raise ImageNotCurrent(text) from None


class GatewayUnavailable(SessionError):
    pass


class PoolExhausted(SessionError):
    """Raised when the session pool is full and all sessions are actively
    processing requests, so no eviction candidate is available."""
    pass


class MemoryPressure(PoolExhausted):
    """Raised when memory admission refuses new work (session create/acquire,
    big-step or heap build). Carries the structured facts the 503 body shows:
    numbers, state, and the sessions involved — never lease ids. The wording of
    any advice is the client's (MCP's) job (owner decision D4)."""

    def __init__(
        self,
        message: str | None = None,
        *,
        facts: dict[str, object] | None = None,
        retry_after_s: int = 5,
        **kwargs: object,
    ) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.facts: dict[str, object] = dict(facts or {})
        self.retry_after_s = retry_after_s


class SessionLeaseError(SessionError):
    """Raised when a caller uses an invalid or missing lease token."""
    pass


class SessionBusyError(SessionError):
    """Raised when a session cannot be released/closed because work is in flight."""
    pass
