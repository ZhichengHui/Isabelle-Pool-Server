"""Reading a backend chunk-status report (verify_chunk / step_chunk_report / sync_document share
the shape): the parsed report, whether it succeeded and the first error to show."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple


def interpret_report(report_json: Any) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """(report, success, error_message) of a report JSON from the backend."""
    try:
        report = json.loads(report_json) if report_json else {}
    except (ValueError, TypeError):
        report = {"timed_out": False, "commands": [],
                  "error": "unparseable backend report"}
    commands = report.get("commands", []) or []
    success = bool(report.get("success", False))
    error_message = report.get("error")
    if not error_message and not success:
        if report.get("timed_out"):
            stuck = next((c.get("line") for c in commands if c.get("status") == "running"), None)
            error_message = f"timed out (still running at line {stuck})" if stuck else "timed out"
        else:
            for c in commands:
                msg = next(
                    (m.get("text") for m in (c.get("messages") or []) if m.get("sev") == "error"),
                    None,
                )
                if msg:
                    error_message = f"line {c.get('line')}: {msg}"
                    break
    return report, success, error_message
