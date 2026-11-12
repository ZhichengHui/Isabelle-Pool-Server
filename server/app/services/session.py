from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import py4j

from server.app.core.config import Logging, Timeouts, RegularExp
from server.app.core.logging import get_logger, logging_context
from server.app.errors import SessionError, SessionLeaseError, SessionNotFound
from server.app.services.threaded_backend import ThreadedBackend
from server.app.services.success_checker import (
    get_error_message,
    get_output_message,
    is_syntax_successful,
)

from .internal_models import (
    CheckPointInfo,
    ProofState,
    SessionExecutionError,
    SessionStatus,
    SmallStepExecuteResult,
)
from .report_parsing import interpret_report
from .session_reset import reset_for_load
from .theory_chunks import preview_text
from .theory_parsing import parse_theory_header

logger = get_logger(__name__)


class _Isabelle_Session:

    def __init__(
        self,
        session_id: uuid.UUID,
        session_theories: List[str],
        session_field: str,
        backend,
        loaded_theories: Optional[List[str]] = None,
        dependency_key: Optional[str] = None,
        wrapper_theory: Optional[str] = None,
        task_group: Optional[str] = None,
    ):
        self.session_id = session_id
        self.theories = list(session_theories)
        self.loaded_theories = list(loaded_theories or session_theories)
        self.dependency_key = dependency_key
        self.wrapper_theory = wrapper_theory
        self.field = session_field
        self.created_at = time.time()
        self.last_activity = time.time()
        self.lease_renewed_at = 0.0  # POST .../renew_lease: the holder is alive, not the session used
        self.status = SessionStatus.ACTIVE
        self.backend: ThreadedBackend = backend

        self.command_history: List[Dict[str, Any]] = []
        self.checkpoints: Dict[int, float] = {}
        self.verified_theories: List[str] = []
        # Report of the most recent verify_chunk call (None until the first
        # one). Cleared by load_document; NOT by rollback/restore.
        self.last_chunk_report: Optional[Dict[str, Any]] = None
        # Full text of the most recent successful load_document (either path).
        # Gates the INCREMENTAL sync fast path: a load can diff against the
        # live node only if a previous full-file load established the base.
        self._loaded_text: Optional[str] = None
        # Directory the entered theory's document comes from (None: unknown).
        self._master_dir: Optional[str] = None
        # Free-form observability label (e.g. the file path a file-synced
        # client is mirroring). Set at creation; no pooling behavior change.
        self.label: Optional[str] = None
        # Task group (heap-pool tenancy, Stage 3): sessions may only use heaps
        # of their own group. Echoed in session info/listings.
        self.task_group: Optional[str] = task_group

        self._closed = False
        self.entered_thy = ""
        self.backend_used = False  # set by every backend call (session_reset.py)
        self._active_requests = 0
        self._active_requests_lock = threading.Lock()

        # Exclusive-lease support: when a session is leased, only the
        # holder (identified by lease_id) may use it.  find_session()
        # skips leased sessions so no two workers can collide.
        self._leased = False
        self._lease_id: Optional[str] = None
        self._lease_lock = threading.Lock()
        with logging_context(session_id=self.session_id, field=self.field):
            logger.info(
                "session object initialized dependency_key=%s loaded_theories=%s",
                (self.dependency_key or "")[:12],
                self.loaded_theories,
            )

    def _call_backend(self, fn, timeout: Optional[float] = None):
        self.backend_used = True
        fut: concurrent.futures.Future = self.backend.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            raise TimeoutError(
                f"Backend call timed out after {timeout:.1f}s"
            ) from None
        except RuntimeError as e:
            # A session looked up via get_session can be closed/evicted before
            # the operation reaches the worker; report that truthfully as a
            # retryable 404 rather than an opaque 500.
            if "shutting down" in str(e) or "shut down before job executed" in str(e):
                raise SessionNotFound(
                    f"Session {self.session_id} was closed while the request was in flight"
                ) from None
            raise

    def update_activity(self):
        self.last_activity = time.time()

    def is_idle(self, timeout: float = Timeouts.IDLE_DEFAULT, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.last_activity) > timeout

    @property
    def in_use(self) -> bool:
        with self._active_requests_lock:
            return self._active_requests > 0

    @property
    def active_request_count(self) -> int:
        with self._active_requests_lock:
            return self._active_requests

    def _acquire_request(self) -> None:
        with self._active_requests_lock:
            self._active_requests += 1

    def _release_request(self) -> None:
        with self._active_requests_lock:
            self._active_requests = max(0, self._active_requests - 1)

    @property
    def leased(self) -> bool:
        with self._lease_lock:
            return self._leased

    @property
    def lease_id(self) -> Optional[str]:
        with self._lease_lock:
            return self._lease_id

    def try_acquire_lease(self, lease_id: str) -> bool:
        with self._lease_lock:
            if self._leased:
                return False
            self._leased = True
            self._lease_id = lease_id
        self.update_activity()
        return True

    def acquire_lease(self, lease_id: str) -> None:
        if not self.try_acquire_lease(lease_id):
            raise SessionLeaseError(
                f"Session {self.session_id} is already leased by {self.lease_id}"
            )

    def require_lease(self, lease_id: Optional[str]) -> None:
        with self._lease_lock:
            if not self._leased or not self._lease_id:
                raise SessionLeaseError(f"Session {self.session_id} is not currently leased")
            if not lease_id:
                raise SessionLeaseError("Missing lease token for leased session")
            if self._lease_id != lease_id:
                raise SessionLeaseError(
                    f"Invalid lease token for session {self.session_id}"
                )

    def release_lease(self) -> None:
        with self._lease_lock:
            self._leased = False
            self._lease_id = None
        self.update_activity()

    def step(self, command: str, timeout: Optional[float] = None):
        if not isinstance(command, str) or command.strip() == "":
            logger.debug("ignoring empty command")
            return None
        logger.debug("backend step submitted preview=%s", preview_text(command, Logging.COMMAND_PREVIEW_CHARS))
        budget_s = timeout if timeout is not None else Timeouts.COMMAND_DEFAULT  # JVM-enforced; rolls back on expiry
        return self._call_backend(lambda: self.backend.raw.step(command, int(budget_s * 1000)), timeout=budget_s + Timeouts.BACKEND_GRACE_S)

    def open_subgoals(self, timeout: Optional[float] = None) -> List[str]:
        subgoals = self._call_backend(lambda: list(self.backend.raw.open_subgoals()), timeout=timeout)
        return [s.strip() for s in subgoals]

    def local_facts(self, timeout: Optional[float] = None) -> List[str]:
        """Read-only probe: facts in the current local proof context (transient,
        leaves the document and rollback chain untouched)."""
        facts = self._call_backend(lambda: list(self.backend.raw.local_facts()), timeout=timeout)
        return [str(f) for f in facts]

    def global_facts(self, limit: int = 100, timeout: Optional[float] = None) -> List[str]:
        """Read-only probe: theory-level facts, sorted by name, capped at ``limit``
        (transient, leaves the document and rollback chain untouched)."""
        facts = self._call_backend(lambda: list(self.backend.raw.global_facts(limit)), timeout=timeout)
        return [str(f) for f in facts]

    def in_proof(self, timeout: Optional[float] = None) -> bool:
        """True while the toplevel is inside a proof block — including after a
        successful terminal `show` with `qed` still pending. This is the "proof
        not registered yet" signal; bare subgoal counting misses the
        discharged-but-unclosed state (batch builds reject it with
        "Goal present in this block")."""
        return bool(self._call_backend(lambda: self.backend.raw.in_proof(), timeout=timeout))

    def proof_finished(self, timeout: Optional[float] = None) -> bool:
        return not self.in_proof(timeout=timeout)

    def get_source(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.get_source(), timeout=timeout)

    def command_at_line(self, line: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Read-only jEdit-style query: the command containing the 1-based `line` of the
        current node (snapshot-based; no edits, no ML probes). Passes the backend's
        JSON through as a dict, tolerating junk."""
        result = self._call_backend(lambda: self.backend.raw.command_at_line(line), timeout=timeout)
        try:
            return json.loads(result) if result else {"found": False, "error": "empty backend reply"}
        except (ValueError, TypeError):
            return {"found": False, "error": "unparseable backend reply"}

    def goals_at_line(self, line: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Read-only jEdit-style query: rendered goal state before/after the command
        containing the 1-based `line` (snapshot-based; no edits, no ML probes).
        Goal lists are empty unless show_states is on. Passes the backend's JSON
        through as a dict, tolerating junk."""
        result = self._call_backend(lambda: self.backend.raw.goals_at_line(line), timeout=timeout)
        try:
            return json.loads(result) if result else {"found": False, "error": "empty backend reply"}
        except (ValueError, TypeError):
            return {"found": False, "error": "unparseable backend reply"}

    def hover_at(self, line: int, col: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Hover info at a 1-based line/col (snapshot + Rendering; no evaluation).
        Passes the backend's JSON through as a dict, tolerating junk."""
        result = self._call_backend(
            lambda: self.backend.raw.hover_at(line, col), timeout=timeout)
        try:
            return json.loads(result) if result else {"found": False, "error": "empty backend reply"}
        except (ValueError, TypeError):
            return {"found": False, "error": "unparseable backend reply"}

    def definition_at(self, line: int, col: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Go-to-definition at a 1-based line/col (snapshot markup; no evaluation).
        Passes the backend's JSON through as a dict, tolerating junk."""
        result = self._call_backend(
            lambda: self.backend.raw.definition_at(line, col), timeout=timeout)
        try:
            return json.loads(result) if result else {"found": False, "error": "empty backend reply"}
        except (ValueError, TypeError):
            return {"found": False, "error": "unparseable backend reply"}

    def sledgehammer_at(
        self,
        line: int,
        subgoal: int = 1,
        timeout_s: int = 30,
        http_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Sledgehammer on the open goal at a 1-based line (overlay print op; no
        text edits). Returns the backend's JSON as a dict: {found, results} or
        {found: False, error}. Same busy/activity accounting as sledgehammer."""
        self.update_activity()
        self._acquire_request()
        try:
            logger.info(
                "running sledgehammer_at line=%s subgoal=%s timeout_s=%s",
                line, subgoal, timeout_s,
            )
            effective_http_timeout = http_timeout or (timeout_s + 40.0)
            result = self._call_backend(
                lambda: self.backend.raw.sledgehammer_at(line, subgoal, timeout_s),
                timeout=effective_http_timeout,
            )
            try:
                return json.loads(result) if result else {"found": False, "error": "empty backend reply"}
            except (ValueError, TypeError):
                return {"found": False, "error": "unparseable backend reply"}
        finally:
            self._release_request()

    @property
    def current_thy(self) -> str:
        return self._call_backend(lambda: self.backend.raw.current_thy_name_string())

    def save_state(self, timeout: Optional[float] = None):
        return self._call_backend(lambda: self.backend.raw.save_state(), timeout=timeout)

    def restore_state(self, state_id: int, timeout: Optional[float] = None) -> bool:
        return bool(self._call_backend(lambda: self.backend.raw.restore_state(state_id), timeout=timeout))

    def rollback(self, timeout: Optional[float] = None):
        logger.info("rolling back backend state")
        return self._call_backend(lambda: self.backend.raw.rollback(), timeout=timeout)

    def sledgehammer(
        self,
        timeout_s: int = 30,
        http_timeout: Optional[float] = None,
    ) -> list:
        """Call Isabelle's sledgehammer via the dedicated ML channel.

        Returns a list of proof method strings (e.g. ['by (metis foo)',
        'by (simp add: bar)']).  Returns an empty list if no proof is found
        within timeout_s or if the session is not in a proof state.
        """
        # Busy/activity accounting: without this, in_use stays False during a
        # long sledgehammer, so release_session succeeds mid-run and the
        # session becomes eligible for idle/memory eviction with a job in
        # flight (audit finding A4).
        self.update_activity()
        self._acquire_request()
        try:
            logger.info("running sledgehammer timeout_s=%s", timeout_s)
            effective_http_timeout = http_timeout or (timeout_s + 30.0)
            raw: "py4j.java_collections.JavaList[str]" = self._call_backend(
                lambda: self.backend.raw.sledgehammer(timeout_s),
                timeout=effective_http_timeout,
            )
            return list(raw) if raw is not None else []
        finally:
            self._release_request()

    @staticmethod
    def _build_theory_header(name: str, imports: List[str]) -> str:
        """Build a valid Isar `theory <name> imports ... begin` header. Session-qualified
        imports (HOL-Number_Theory.Number_Theory) contain '-'/'.' and MUST be quoted, else
        Isabelle splits them; the quoting lives here so every client gets it right."""
        def q(i: str) -> str:
            return i if re.fullmatch(r"[A-Za-z][\w']*", i) else f'"{i}"'
        names = [n for n in (imports or []) if n] or ["Main"]
        return f"theory {name} imports {' '.join(q(n) for n in names)} begin"

    def enter_thy(self, thy_name: str, timeout: Optional[float] = None,
                  imports: Optional[List[str]] = None,
                  master_dir: Optional[str] = None):
        """Enter a theory node. If ``imports`` is given, the server also begins the theory
        by processing a correctly-quoted ``theory ... begin`` header — so the client never
        hand-builds Isar headers and verify_chunk/step work immediately. If ``imports`` is
        omitted, behaviour is unchanged (caller supplies the header itself, e.g. corpus .thy).

        ``master_dir`` is the directory the document comes from: the backend resolves the
        document's bare-name imports against it (`isabelle build`'s rule) and reads
        them from disk. Without it they resolve in the server's own directory."""
        self.entered_thy = thy_name
        self._master_dir = master_dir
        logger.info("entering theory theory_name=%s imports=%s master_dir=%s",
                    thy_name, imports, master_dir)
        if master_dir:
            result = self._call_backend(
                lambda: self.backend.raw.enter_thy_in(thy_name, master_dir), timeout=timeout)
        else:
            result = self._call_backend(lambda: self.backend.raw.enter_thy(thy_name), timeout=timeout)
        if imports:
            header = self._build_theory_header(thy_name, imports)
            hdr = self.step(header, timeout=timeout)
            if not is_syntax_successful(hdr):
                raise SessionError(
                    error=f"theory header failed: {self._result_error(hdr)}", execution_time=0.0)
        return result

    def _reset_bookkeeping(self) -> None:
        """Clear all per-session Python-side bookkeeping after a backend reset."""
        self.command_history.clear()
        self.checkpoints.clear()
        self.verified_theories.clear()
        self.entered_thy = ""
        self._master_dir = None
        self.last_chunk_report = None
        self._loaded_text = None

    @staticmethod
    def _ends_with_theory_end(text: str) -> bool:
        """True when the document's last non-empty line is theory `end`: the state queries
        are then skipped — a successful `end` means no proof is open (backend agrees)."""
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        return bool(lines) and lines[-1] == "end"

    def _store_and_interpret_report(
        self, report_json, execution_time: float, start_time: float
    ):
        """Parse a backend chunk-status report JSON (verify_chunk /
        step_chunk_report / sync_document share the shape), store it in
        ``last_chunk_report``, and derive (report, success, error_message)."""
        report, success, error_message = interpret_report(report_json)
        self.last_chunk_report = {
            "report": report,
            "execution_time": execution_time,
            "timestamp": start_time,
        }
        return report, success, error_message

    def _step_with_report(self, text: str, timeout: float):
        """Issue ``text`` as one edit and return (success, error, output) together
        with a per-command status report, WITHOUT rolling back ordinary failures
        (backend ``step_chunk_report``; the dual of verify_chunk's transactional
        semantics). The report is stored in ``last_chunk_report``. On budget
        timeout the backend discards the edit to cancel runaway commands."""
        budget_ms = int(max(0.0, timeout) * 1000)
        probe_state = not self._ends_with_theory_end(text)
        start_time = time.time()
        # Backend bounds the work at budget_ms; give the Python call extra grace
        # so the Python side never times out before the backend returns its report.
        report_json = self._call_backend(
            lambda: self.backend.raw.step_chunk_report(text, budget_ms, probe_state),
            timeout=timeout + Timeouts.COMMAND_DEFAULT,
        )
        execution_time = time.time() - start_time
        _, success, error_message = self._store_and_interpret_report(
            report_json, execution_time, start_time)
        return success, error_message, ""

    def _sync_document(self, text: str, timeout: float):
        """INCREMENTAL fast path of ``load_document`` (Phase B1): the backend
        diffs ``text`` against the live node and submits ONE PIDE replace edit,
        so PIDE re-processes only from the first changed command onward
        (backend ``sync_document``).

        Returns (success, error, output) like ``_step_with_report``, or None
        when the backend's report carries the ``fallback`` marker
        (no begun theory / header changed) — the caller then uses the reset
        path. On success this updates ``last_chunk_report`` and
        ``_loaded_text``, and INVALIDATES all checkpoints: pre-replace
        checkpoints reconstruct text from the insert-only chain and are
        unsound after a replace edit. On budget timeout the backend does NOT
        discard the replace (no single insert to discard) — the partial state
        stays for inspection (LSP-style)."""
        budget_ms = int(max(0.0, timeout) * 1000)
        probe_state = not self._ends_with_theory_end(text)
        start_time = time.time()
        # Backend bounds the work at budget_ms; give the Python call extra grace
        # so the Python side never times out before the backend returns its report.
        report_json = self._call_backend(
            lambda: self.backend.raw.sync_document(text, budget_ms, probe_state),
            timeout=timeout + Timeouts.COMMAND_DEFAULT,
        )
        execution_time = time.time() - start_time
        try:
            report = json.loads(report_json) if report_json else {}
        except (ValueError, TypeError):
            report = {"timed_out": False, "commands": [],
                      "error": "unparseable backend report"}
        if report.get("fallback"):
            logger.info(
                "document sync declined by backend, falling back to reset reason=%s",
                report.get("fallback"),
            )
            return None
        _, success, error_message = self._store_and_interpret_report(
            report_json, execution_time, start_time)
        self._loaded_text = text
        if self.checkpoints:
            logger.info(
                "document sync invalidating %s checkpoint(s) (unsound after a replace edit)",
                len(self.checkpoints),
            )
            self.checkpoints.clear()
        return success, error_message, ""

    def _sync_candidate(self, text: str, thy_name: Optional[str],
                        imports: Optional[List[str]], report: bool,
                        master_dir: Optional[str] = None) -> bool:
        """True when ``load_document`` may attempt the incremental sync path:
        report mode on a full-file load (imports mode builds the header
        server-side and keeps the reset path), a previous full-file load left
        a base text, and the entered theory name matches the incoming text's
        header theory name (canonical parse)."""
        if not report or imports is not None:
            return False
        if self._loaded_text is None or not self.entered_thy:
            return False
        if (master_dir or None) != (self._master_dir or None):
            return False  # imports resolve differently: re-enter the theory
        name = thy_name
        if not name:
            name, _ = parse_theory_header(text)
        return bool(name) and name == self.entered_thy

    def load_document(self, text: str, thy_name: Optional[str] = None,
                      imports: Optional[List[str]] = None,
                      timeout: float = Timeouts.COMMAND_DEFAULT,
                      report: bool = False,
                      master_dir: Optional[str] = None) -> SmallStepExecuteResult:
        """Replace the session's whole document with ``text`` (the file-sync primitive).

        Two paths. The RESET path (always used for a first load, for
        ``imports`` mode, and whenever the incremental path declines): resets
        the backend (the document model is append-only, so wholesale
        replacement = fresh Repl_Session; skipped while the backend is still
        pristine, see session_reset.py), clears all bookkeeping, then
        re-enters the theory and issues ``text`` as a single edit. The
        INCREMENTAL path (Phase B1, ``report=True`` full-file re-loads of the
        same theory): the backend diffs ``text`` against the live node and
        submits ONE PIDE replace edit, so PIDE re-processes only from the
        first changed command onward (``_sync_candidate`` /
        ``_sync_document``); a ``fallback`` marker in the backend's reply
        (header changed / no begun theory) drops back to the reset path.

        Two modes, mirroring ``enter_thy``: if ``imports`` is given the server
        builds the ``theory ... begin`` header and ``text`` is the body after
        ``begin``; otherwise ``text`` must be a full .thy source including its
        own header (the file-sync case), and ``thy_name`` defaults to the
        header's name.

        With ``report=True`` the text is issued via the backend's
        ``step_chunk_report`` (reset path) or ``sync_document`` (incremental
        path): a per-command status report (same shape as verify_chunk's) is
        produced and stored in ``last_chunk_report``, WITHOUT rolling back
        ordinary failures (LSP-style: broken state stays for inspection). On
        budget timeout the reset path still discards the edit to cancel
        runaway commands; the incremental path keeps the replace (no single
        insert to discard) and reports timed_out.
        """
        self.update_activity()
        self._acquire_request()
        start_time = time.time()
        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "load_document started thy_name=%s imports=%s report=%s preview=%s",
                    thy_name, imports, report, preview_text(text, Logging.COMMAND_PREVIEW_CHARS),
                )
                sync_used = False
                try:
                    if self._sync_candidate(text, thy_name, imports, report, master_dir):
                        sync_result = self._sync_document(text, timeout)
                        if sync_result is not None:
                            success, error_message, output = sync_result
                            sync_used = True
                            execution_time = time.time() - start_time
                    if not sync_used:
                        reset_for_load(self, timeout)  # skipped on a pristine backend

                        name = thy_name
                        if not name and not imports:
                            m = RegularExp.THEORY_RE.search(text)
                            if m:
                                name = m.group(1) or m.group(2)
                        if not name:
                            raise SessionError(
                                error="load_document: could not determine theory name — "
                                      "pass thy_name, or include a 'theory ... imports ... begin' "
                                      "header in text",
                                execution_time=time.time() - start_time,
                            )

                        self.enter_thy(name, timeout=timeout, imports=imports, master_dir=master_dir)
                        if report:
                            success, error_message, output = self._step_with_report(text, timeout)
                        else:
                            result = self.step(text, timeout=timeout)
                            success = is_syntax_successful(result)
                            error_message = self._result_error(result)
                            output = self._result_output(result)
                        execution_time = time.time() - start_time
                        self._loaded_text = text
                except SessionError:
                    raise  # already typed (e.g. SessionNotFound) — keep its HTTP mapping
                except Exception as e:
                    execution_time = time.time() - start_time
                    msg = f"{type(e).__name__}"
                    if str(e):
                        msg += f": {str(e)}"
                    logger.exception("load_document backend call failed: %s", msg)
                    raise SessionError(error=msg, execution_time=execution_time) from None

                self.command_history.append(
                    {
                        "type": "document_sync" if sync_used else "document_load",
                        "command": preview_text(text, Logging.COMMAND_PREVIEW_CHARS),
                        "timestamp": start_time,
                        "success": success,
                        "subgoal_error": None,
                        "subgoals_count": 0,
                    }
                )
                logger.info(
                    "load_document finished theory=%s sync=%s success=%s execution_time=%s",
                    self.entered_thy, sync_used, success, round(execution_time, 3),
                )
                return SmallStepExecuteResult(
                    success=success,
                    output=output,
                    error=error_message if not success else None,
                    subgoal_error=None,
                    subgoals=[],
                    execution_time=execution_time,
                )
        finally:
            self._release_request()

    def _result_error(self, result) -> Optional[str]:
        return get_error_message(result)

    def _result_output(self, result) -> str:
        if result is None:
            return ""
        if hasattr(result, "total_output"):
            try:
                value = result.total_output()
                return value if isinstance(value, str) else str(value)
            except Exception:
                return str(result)
        return str(result)

    def execute_command(self, command: str, timeout: float = Timeouts.COMMAND_DEFAULT) -> SmallStepExecuteResult:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "small-step command started timeout=%s preview=%s",
                    timeout,
                    preview_text(command, Logging.COMMAND_PREVIEW_CHARS),
                )
                try:
                    result = self.step(command, timeout=timeout)
                    execution_time = time.time() - start_time
                except SessionError:
                    raise  # already typed (e.g. SessionNotFound) — keep its HTTP mapping
                except Exception as e:
                    execution_time = time.time() - start_time
                    msg = f"{type(e).__name__}"
                    if str(e):
                        msg += f": {str(e)}"
                    logger.exception("small-step backend call failed: %s", msg)
                    raise SessionError(error=msg, execution_time=execution_time) from None

                try:
                    success = is_syntax_successful(result)
                    error_message = self._result_error(result)
                    subgoal_error: Optional[str] = None
                    if command == "end" or command == "end\n" or \
                            RegularExp.THEORY_HEADER_RE.search(command):
                        subgoals = []
                    else:
                        try:
                            subgoals = self.open_subgoals(timeout=timeout)
                        except Exception as exc:
                            subgoals = []
                            subgoal_error = f"{exc.__class__.__name__}: {exc}"
                            logger.warning(
                                "open_subgoals failed after command execution; command_success=%s error=%s",
                                success,
                                subgoal_error,
                            )

                    self.command_history.append(
                        {
                            "type": "small_step",
                            "command": command,
                            "timestamp": start_time,
                            "success": success,
                            "subgoal_error": subgoal_error,
                            "subgoals_count": len(subgoals),
                        }
                    )

                    logger.info(
                        "small-step command finished success=%s subgoals=%s subgoal_error=%s execution_time=%s",
                        success,
                        len(subgoals),
                        bool(subgoal_error),
                        round(execution_time, 3),
                    )
                    return SmallStepExecuteResult(
                        success=success,
                        output=self._result_output(result),
                        error=error_message if not success else None,
                        subgoal_error=subgoal_error,
                        subgoals=subgoals,
                        execution_time=execution_time,
                    )

                except Exception as e:
                    execution_time = time.time() - start_time
                    logger.exception("small-step result processing failed")
                    raise SessionError(error=str(e), execution_time=execution_time)
        finally:
            self._release_request()

    def run_diagnostic(self, command: str, timeout: float = Timeouts.COMMAND_DEFAULT) -> SmallStepExecuteResult:
        """Run a single READ-ONLY diagnostic command (thm, term, find_theorems, print_*, ...)
        TRANSIENTLY: the backend inserts it, reads its output (waiting at most `timeout`),
        then discards the edit — script and rollback chain untouched. No subgoals, no
        command_history entry (a query, not a proof step). The router MUST have validated
        the command against core.diagnostic_guard first."""
        self.update_activity()
        self._acquire_request()
        start_time = time.time()
        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "diagnostic started timeout=%s preview=%s",
                    timeout,
                    preview_text(command, Logging.COMMAND_PREVIEW_CHARS),
                )
                try:
                    result = self._call_backend(
                        lambda: self.backend.raw.probe_transient(command, int(timeout * 1000)),
                        timeout=timeout + Timeouts.BACKEND_GRACE_S)
                    execution_time = time.time() - start_time
                except SessionError:
                    raise  # already typed (e.g. SessionNotFound) — keep its HTTP mapping
                except Exception as e:
                    execution_time = time.time() - start_time
                    msg = f"{type(e).__name__}"
                    if str(e):
                        msg += f": {str(e)}"
                    logger.exception("diagnostic backend call failed: %s", msg)
                    raise SessionError(error=msg, execution_time=execution_time) from None

                output = get_output_message(result)
                error_message = get_error_message(result)
                success = is_syntax_successful(result)
                logger.info(
                    "diagnostic finished success=%s has_output=%s execution_time=%s",
                    success,
                    bool(output),
                    round(execution_time, 3),
                )
                return SmallStepExecuteResult(
                    success=success,
                    output=output,
                    error=error_message if not success else None,
                    subgoal_error=None,
                    subgoals=[],
                    execution_time=execution_time,
                )
        finally:
            self._release_request()

    def verify_chunk(self, chunk: str, timeout: float = Timeouts.COMMAND_DEFAULT) -> Dict[str, Any]:
        """Verify a whole proof chunk in one shot under a SINGLE wall budget.

        The chunk is inserted as one PIDE edit and checked with per-session parallelism on;
        the backend returns a per-command status report (ok/failed/running/unprocessed) in
        source order. The wall budget is enforced inside the backend, so on expiry we get a
        PARTIAL report (naming the still-`running` line) rather than a timeout exception —
        only one timeout is ever surfaced. Returns {"report": <parsed>, "execution_time": s}.
        """
        self.update_activity()
        self._acquire_request()
        start_time = time.time()
        budget_ms = int(max(0.0, timeout) * 1000)
        try:
            with logging_context(session_id=self.session_id, field=self.field):
                logger.info(
                    "verify_chunk started budget_ms=%s preview=%s",
                    budget_ms,
                    preview_text(chunk, Logging.COMMAND_PREVIEW_CHARS),
                )
                try:
                    # Backend bounds the work at budget_ms; give the Python call extra grace
                    # so the Python side never times out before the backend returns its report.
                    report_json = self._call_backend(
                        lambda: self.backend.raw.verify_chunk(chunk, budget_ms),
                        timeout=timeout + Timeouts.COMMAND_DEFAULT,
                    )
                    execution_time = time.time() - start_time
                except SessionError:
                    raise  # already typed (e.g. SessionNotFound) — keep its HTTP mapping
                except Exception as e:
                    execution_time = time.time() - start_time
                    msg = f"{type(e).__name__}"
                    if str(e):
                        msg += f": {str(e)}"
                    logger.exception("verify_chunk backend call failed: %s", msg)
                    raise SessionError(error=msg, execution_time=execution_time) from None

                try:
                    report = json.loads(report_json) if report_json else {}
                except (ValueError, TypeError):
                    report = {"timed_out": False, "commands": [],
                              "error": "unparseable backend report"}
                # Retain the report of the MOST RECENT verify_chunk call
                # (success or failure) so it stays queryable after submission.
                self.last_chunk_report = {
                    "report": report,
                    "execution_time": execution_time,
                    "timestamp": start_time,
                }
                commands = report.get("commands", []) or []
                logger.info(
                    "verify_chunk finished commands=%s timed_out=%s execution_time=%s",
                    len(commands), report.get("timed_out"), round(execution_time, 3),
                )
                return {"report": report, "execution_time": execution_time}
        finally:
            self._release_request()

    def get_proof_state(self, timeout: float = Timeouts.PROOF_STATE) -> ProofState | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    subgoals = self.open_subgoals(timeout=timeout)
                    block_open = self.in_proof(timeout=timeout)
                    current_thy = self.current_thy
                    logger.debug("proof state fetched subgoals=%s block_open=%s current_theory=%s", len(subgoals), block_open, current_thy)
                    return ProofState(
                        subgoals=subgoals,
                        proof_finished=not block_open,
                        pending_qed=block_open and not subgoals,
                        current_theory=current_thy,
                    )
                except Exception as e:
                    msg = f"{type(e).__name__}"
                    if str(e):
                        msg += f": {str(e)}"
                    logger.exception("failed to fetch proof state: %s", msg)
                    return SessionExecutionError(error=msg, execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def save_checkpoint(self, timeout: float = Timeouts.CHECKPOINT_SAVE) -> CheckPointInfo | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    checkpoint_id = self.save_state(timeout=timeout)
                    timestamp = time.time()
                    self.checkpoints[checkpoint_id] = timestamp
                    logger.info("checkpoint saved checkpoint_id=%s", checkpoint_id)
                    return CheckPointInfo(checkpoint_id=checkpoint_id, timestamp=timestamp)
                except Exception as e:
                    logger.exception("failed to save checkpoint")
                    return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def restore_checkpoint(self, checkpoint_id: int, timeout: float = Timeouts.CHECKPOINT_RESTORE) -> bool | SessionExecutionError:
        self.update_activity()
        self._acquire_request()
        start_time = time.time()

        try:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    if checkpoint_id not in self.checkpoints:
                        logger.warning("checkpoint not found checkpoint_id=%s", checkpoint_id)
                        return SessionExecutionError(
                            error=f"Checkpoint {checkpoint_id} not found",
                            execution_time=time.time() - start_time,
                        )
                    if not self.restore_state(checkpoint_id, timeout=timeout):
                        # backend no longer holds it (document replace/reset): never report a
                        # restore that applied nothing (Bug 22 / REPL-4)
                        self.checkpoints.pop(checkpoint_id, None)
                        logger.warning("backend rejected checkpoint checkpoint_id=%s", checkpoint_id)
                        return SessionExecutionError(
                            error=f"Checkpoint {checkpoint_id} is unknown to the backend (invalidated by a document replace or reset); nothing restored",
                            execution_time=time.time() - start_time)
                    logger.info("checkpoint restored checkpoint_id=%s", checkpoint_id)
                    return True
                except Exception as e:
                    logger.exception("failed to restore checkpoint checkpoint_id=%s", checkpoint_id)
                    return SessionExecutionError(error=str(e), execution_time=time.time() - start_time)
        finally:
            self._release_request()

    def close(self):
        if not self._closed:
            with logging_context(session_id=self.session_id, field=self.field):
                try:
                    logger.info("closing threaded backend")
                    self.backend.close()
                finally:
                    self._closed = True
                    self.status = SessionStatus.CLOSED
                    logger.info("session closed")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
