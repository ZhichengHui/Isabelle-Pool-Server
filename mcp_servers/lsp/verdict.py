"""The verdict of one isabelle_multi_attempt candidate."""
from __future__ import annotations

from typing import Any, Dict


def candidate_verdict(rep: Dict[str, Any], bad: list, prefix_bad: list, used_sorry: Any) -> str:
    """One word for a candidate's check: a failure before the candidate makes its own result
    unreliable (the commands after a failed one in the same proof are not run), so it comes
    first; a report that is not a success counts as failed even when no command says why."""
    if prefix_bad:
        return "prefix_failed"
    if rep.get("timed_out"):
        return "timed_out"
    if bad or rep.get("success") is not True:
        return "failed"
    return "used_sorry" if used_sorry else "ok"
