"""Tiered timeout reminders in tool results.

A command that runs long, or a check that hits its budget, is reported at fixed elapsed times
(Config.REMINDER_S, default 30,120,300,600 s) with the number of the reminder, the time of the
next one and what to do at that level, so the agent decides whether to wait, change the step or
close the file — instead of one flat note that never changes however long the wait is."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import Config


def thresholds() -> List[float]:
    out: List[float] = []
    for part in str(Config.REMINDER_S).split(","):
        try:
            out.append(float(part))
        except ValueError:
            continue
    return sorted(t for t in out if t > 0)


def _advice(level: int, of: int) -> str:
    if level >= of:
        return ("last reminder: it most likely does not terminate. Change the step on that line "
                "(a simpler method, or `sorry` it), or isabelle_close the file to stop it; "
                "waiting longer is unlikely to help.")
    if level == 1:
        return ("it may still finish: a first check of a heavy file or step takes longer than "
                "one wait. Check again; nothing needs to change yet.")
    return ("compare it with how long this step should take. If it should be much faster, "
            "try a simpler variant with isabelle_multi_attempt on that line.")


def add_reminder(result: Dict[str, Any], timed_out: Any, budget_s: float, subject: str) -> None:
    """Add `reminder` to a tool result whose attempt ran into its budget (`timed_out`)."""
    r = reminder(budget_s, subject) if timed_out else None
    if r:
        result["reminder"] = r["message"]


def reminder(elapsed_s: float, subject: str) -> Optional[Dict[str, Any]]:
    """The reminder for `subject` (e.g. "command at line 204") that has run `elapsed_s` seconds:
    the last threshold passed, as {level, of, elapsed_s, next_reminder_at_s, message}. None
    before the first threshold or when reminders are off (REMINDER_S empty)."""
    ts = thresholds()
    passed = [t for t in ts if elapsed_s >= t]
    if not passed:
        return None
    level, of = len(passed), len(ts)
    nxt = ts[level] if level < of else None
    when = f"next reminder at {nxt:.0f} s" if nxt is not None else "no further reminder"
    return {
        "level": level, "of": of, "elapsed_s": round(elapsed_s),
        "next_reminder_at_s": nxt,
        "message": f"Reminder {level}/{of}: {subject} has run {elapsed_s:.0f} s ({when}); "
                   + _advice(level, of),
    }
