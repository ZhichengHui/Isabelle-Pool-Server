"""Giving a session a fresh document before a whole-document load (load_document's reset path).

``Backend_Lifecycle.reset`` stops the session's Isabelle process and starts a new one with the
same initial theories, field and session directories — about as long as creating the session
(20+ s with a large heap). Two consequences:

- A session whose backend has not been called at all since it was created is already in exactly
  the state a reset produces, so the reset is skipped (it used to run on the first load of every
  new session, doubling its start-up time).
- Once the reset starts, the recorded ML pid is the process it stops. The pid is forgotten
  BEFORE the reset, so a reset that outlives the request budget is not read as a prover death
  (the death check then asks the session phase and the next probe records the new pid).
"""
from __future__ import annotations

from typing import Any


def backend_pristine(session: Any) -> bool:
    """True when the backend has not been called at all since the session was created
    (``_Isabelle_Session._call_backend`` sets ``backend_used``). Not the Python-side
    bookkeeping: a reset path clears that before it enters the theory, so a load that
    fails after the reset would leave a used document looking untouched."""
    return not getattr(session, "backend_used", True)


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
