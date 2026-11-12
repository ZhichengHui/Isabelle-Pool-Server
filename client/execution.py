"""Execution: small-step commands, diagnostics, verify_chunk, whole-document load,
sledgehammer (tip and positioned), big-step builds, and the chunk-report renderer."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ._base import BASE_URL, ClientBase, extract_theory_name


class ExecutionMixin(ClientBase):
    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        budget = timeout if timeout is not None else self.timeout
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/commands",
            json_body={"command": command, "timeout": budget},
            headers=self._lease_headers(lease_id),
            # HTTP timeout must exceed the server-side command budget, else the
            # client aborts exactly when the server would report the timeout.
            timeout=budget + 30.0,
        )
        response.raise_for_status()
        return response.json()

    async def diagnostic(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a single READ-ONLY Isabelle diagnostic command and return its output.

        Use this for inspecting state WITHOUT changing the proof: ``thm <name>`` (show a
        theorem), ``term``/``prop``/``typ`` (parse & print), ``find_theorems``/``find_consts``
        (search), ``prf``, ``print_theorems``/``print_facts``/``print_*``. The command runs
        transiently (it leaves the proof script and rollback chain untouched), so it is the
        right call when ``verify_chunk`` would discard ``thm``/search output.

        Code-executing / IO commands (``ML``, ``setup``, ``*_file``, ...) are rejected by the
        server with HTTP 422. Returns ``{success, output, error, execution_time}``.
        """
        budget = timeout if timeout is not None else self.timeout
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/diagnostic",
            json_body={"command": command, "timeout": budget},
            headers=self._lease_headers(lease_id),
            # grace beyond the server-side budget (same rationale as verify_chunk)
            timeout=budget + 30.0,
        )
        response.raise_for_status()
        return response.json()

    async def verify_chunk(
        self,
        session_id: str,
        chunk: str,
        timeout: float | None = None,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a whole proof chunk in one call under a SINGLE wall budget.

        Returns a per-command status report in source order:
        ``{success, timed_out, stuck_line, commands: [{index, line, kind, status,
        messages}], execution_time}``. On timeout the report is partial and ``stuck_line``
        names the still-``running`` command (the likely loop) — no intermediate timeouts.
        """
        budget = timeout if timeout is not None else self.timeout
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/verify_chunk",
            json_body={"chunk": chunk, "timeout": budget},
            headers=self._lease_headers(lease_id),
            # client waits a bit beyond the server's wall budget (server bounds the work)
            timeout=(budget + 60.0) if budget is not None else None,
        )
        response.raise_for_status()
        return response.json()

    async def get_last_report(
        self, session_id: str, *, lease_id: str | None = None,
    ) -> dict[str, Any]:
        """The retained report of the session's most recent ``verify_chunk`` call:
        ``{report, execution_time, timestamp}``. Raises for HTTP 404 if no
        ``verify_chunk`` has run yet (or ``load_document`` cleared it)."""
        return await self._get_json(f"{BASE_URL}/{session_id}/last_report", lease_id=lease_id)

    async def load_document(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        text: str,
        *,
        thy_name: str | None = None,
        imports: list[str] | None = None,
        timeout: float | None = None,
        report: bool = False,
        lease_id: str | None = None,
        master_dir: str | None = None,
        keep_on_timeout: bool = False,
    ) -> dict[str, Any]:
        """Replace the session's whole document with ``text`` (file-sync primitive).

        Resets the backend document and all session bookkeeping, then re-enters
        the theory and issues the text as one edit. If ``imports`` is given the
        server builds the theory header and ``text`` is the body after ``begin``
        (``thy_name`` required); otherwise ``text`` must be a full .thy source
        including its own header and ``thy_name`` defaults to the header's name.

        With ``report=True`` the response carries a per-command status report
        (same shape as ``verify_chunk``; failures are NOT rolled back, so broken
        state stays for inspection). The report is also retained server-side and
        queryable via :meth:`get_last_report`.

        ``master_dir`` (absolute path, optional) is the directory of the file the
        text comes from: the server resolves the text's bare-name imports against
        it, as ``isabelle build`` does (the server must see the same disk).

        Returns ``{success, theory, output, error, execution_time, report}``.
        """
        budget = timeout if timeout is not None else self.timeout
        body = {"text": text, "thy_name": thy_name, "imports": imports,
                "timeout": budget, "report": report}
        if master_dir:
            body["master_dir"] = master_dir
        if keep_on_timeout:
            body["keep_on_timeout"] = True
        response = await self._request(
            "PUT",
            f"{BASE_URL}/{session_id}/document",
            json_body=body,
            headers=self._lease_headers(lease_id),
            timeout=(budget + 60.0) if budget is not None else None,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def format_chunk_report(report: dict[str, Any], *, max_msg: int = 300) -> str:
        """Render a ``verify_chunk`` report as a readable, source-ordered table.

        Saves callers from writing their own loop. ``report`` is the dict returned by
        :meth:`verify_chunk`. Per-command rows show a status marker, line, command kind,
        status, and any error/warning messages (whitespace-collapsed, truncated to
        ``max_msg`` chars). Returns a string; see :meth:`print_chunk_report` to print it.
        """
        marker = {"ok": "OK ", "failed": "ERR", "running": "RUN", "unprocessed": "..."}
        lines = [
            "verify_chunk: success={success} proof_open={proof_open} used_sorry={used_sorry} "
            "timed_out={timed_out} stuck_line={stuck_line} time={t:.2f}s".format(
                success=report.get("success"),
                proof_open=report.get("proof_open"),
                used_sorry=report.get("used_sorry"),
                timed_out=report.get("timed_out"),
                stuck_line=report.get("stuck_line"),
                t=float(report.get("execution_time", 0.0) or 0.0),
            )
        ]
        commands = report.get("commands") or []
        if not commands:
            lines.append("  (no commands reported)")
        for c in commands:
            status = str(c.get("status", "?"))
            lines.append(
                f"  [{marker.get(status, ' ? ')}] line {int(c.get('line', 0)):>3}  "
                f"{str(c.get('kind', '')):<7} {status}"
            )
            for m in c.get("messages") or []:
                text = " ".join(str(m.get("text", "")).split())
                if len(text) > max_msg:
                    text = text[: max_msg - 1] + "…"
                lines.append(f"        {m.get('sev', '')}: {text}")
        return "\n".join(lines)

    @staticmethod
    def print_chunk_report(report: dict[str, Any], *, max_msg: int = 300) -> None:
        """Pretty-print a :meth:`verify_chunk` report (see :meth:`format_chunk_report`)."""
        print(ExecutionMixin.format_chunk_report(report, max_msg=max_msg))

    async def sledgehammer(
        self,
        session_id: str,
        timeout_s: int = 30,
        *,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Run Isabelle's sledgehammer on the current proof goal.

        Returns a dict with keys: success (bool), suggestions (list of strings),
        raw_output (str), execution_time (float).

        The session must already be in an active proof state.
        """
        # Grace covers both the prover run AND time queued on the server-wide
        # sledgehammer semaphore (requests wait there before the ML call starts).
        http_timeout = timeout_s + 120.0
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/sledgehammer",
            json_body={"timeout_s": timeout_s},
            headers=self._lease_headers(lease_id),
            timeout=http_timeout,
        )
        response.raise_for_status()
        return response.json()

    async def sledgehammer_at(
        self, session_id: str, line: int, *, subgoal: int = 1, timeout_s: int = 30,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        """Position-explicit sledgehammer (overlay; no text edits):
        ``{found, results}`` or ``{found: False, error}``."""
        response = await self._request(
            "POST", f"{BASE_URL}/{session_id}/sledgehammer_at",
            json_body={"line": int(line), "subgoal": int(subgoal), "timeout_s": int(timeout_s)},
            headers=self._lease_headers(lease_id),
            # same grace as the tip endpoint: prover time + semaphore queueing
            timeout=timeout_s + 120.0,
        )
        response.raise_for_status()
        return response.json()

    async def fill_sorries(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        text: str,
        *,
        mode: str = "document",
        timeout_s: int = 30,
        max_total_s: float = 600.0,
        provers: str | None = None,
        load_timeout: float = 120.0,
        lease_id: str | None = None,
        master_dir: str | None = None,
    ) -> dict[str, Any]:
        """Start the server-side sledgehammer job on every ``sorry`` of ``text``
        (``POST …/fill_sorries``, 202). Returns the initial job state: ``status``,
        ``n``, ``eta_s``, ``queue_position``; poll with :meth:`fill_status`.
        409 ``fill_in_flight`` while a job runs on the session."""
        body: dict[str, Any] = {
            "text": text,
            "mode": mode,
            "timeout_s": int(timeout_s),
            "max_total_s": float(max_total_s),
            "load_timeout": float(load_timeout),
        }
        if provers:
            body["provers"] = provers
        if master_dir:
            body["master_dir"] = master_dir
        response = await self._request(
            "POST",
            f"{BASE_URL}/{session_id}/fill_sorries",
            json_body=body,
            headers=self._lease_headers(lease_id),
            timeout=load_timeout + 60.0,
        )
        response.raise_for_status()
        return response.json()

    async def fill_status(
        self, session_id: str, *, lease_id: str | None = None
    ) -> dict[str, Any]:
        """The job state (``GET …/fill_sorries``): status, per-sorry results,
        ``patched_text``, ``eta_s``, ``queue_position``, parked flags."""
        return await self._get_json(
            f"{BASE_URL}/{session_id}/fill_sorries", lease_id=lease_id
        )

    async def fill_cancel(
        self, session_id: str, *, lease_id: str | None = None
    ) -> dict[str, Any]:
        """Cancel the session's job (``DELETE …/fill_sorries``)."""
        response = await self._request(
            "DELETE",
            f"{BASE_URL}/{session_id}/fill_sorries",
            headers=self._lease_headers(lease_id),
        )
        response.raise_for_status()
        return response.json()

    async def verify_bigstep_text(
        self,
        theory_name: str,
        theory_text: str,
        *,
        field: str | None = "HOL",
        dependencies: list[str] | None = None,
        timeout: float = 300.0,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "theory_name": theory_name,
            "field": field,
            "theory": theory_text,
            "timeout": timeout,
        }
        if dependencies:
            payload["dependencies"] = dependencies
        return await self._request(
            "POST",
            f"{BASE_URL}/bigstep",
            json_body=payload,
            timeout=timeout,
        )

    async def verify_bigstep_file(
        self,
        file_path: str | Path,
        *,
        field: str | None = "HOL",
        timeout: float = 300.0,
    ) -> httpx.Response:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Theory file {path} not found.")
        theory_text = path.read_text(encoding="utf-8")
        theory_name = extract_theory_name(theory_text) or path.stem
        return await self.verify_bigstep_text(
            theory_name=theory_name,
            theory_text=theory_text,
            field=field,
            timeout=timeout,
        )
