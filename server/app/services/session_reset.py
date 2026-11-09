"""Giving a session a fresh document before a whole-document load (load_document's reset path).

``Backend_Lifecycle.reset`` stops the session's Isabelle process and starts a new one with the
same initial theories, field and session directories — about as long as creating the session
(20+ s with a large heap). Two consequences:

- A session that has had no theory, command, document load or chunk since it was created is
  already in exactly the state a reset produces, so the reset is skipped (it used to run on the
  first load of every new session, doubling its start-up time).
- Once the reset starts, the recorded ML pid is the process it stops. The pid is forgotten
  BEFORE the reset, so a reset that outlives the request budget is not read as a prover death
  (the death check then asks the session phase and the next probe records the new pid).
"""
from __future__ import annotations

from typing import Any


def backend_pristine(session: Any) -> bool:
    """True when nothing has been done on the backend's document since it was created."""
    return (
        not session.entered_thy
        and not session.command_history
        and session._loaded_text is None  # pylint: disable=protected-access
        and session.last_chunk_report is None
        and not session.checkpoints
    )


def reset_for_load(session: Any, timeout: float) -> bool:
    """Reset the backend unless it is pristine, then clear the bookkeeping.
    Returns whether a reset ran."""
    reset = not backend_pristine(session)
    if reset:
        forget = getattr(session, "forget_prover", None)
        if forget is not None:
            forget()
        session._call_backend(  # pylint: disable=protected-access
            lambda: session.backend.raw.reset(), timeout=timeout
        )
    session._reset_bookkeeping()  # pylint: disable=protected-access
    return reset
