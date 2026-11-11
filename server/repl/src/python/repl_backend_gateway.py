"""Utilities associated with the process that runs the Scala REPL gateway."""

import concurrent.futures
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol

import py4j
import py4j.java_collections
from py4j.java_gateway import GatewayParameters, JavaGateway

from server.app.core.config import Logging, Repl
from server.app.core.logging import get_logger
from server.repl.src.python import process_tree

logger = get_logger(__name__)

# Locate the Scala gateway source RELATIVE TO THIS FILE (server/repl/src/python/
# → server/repl), never via a hard-coded /app/repl: the REPL component moved
# under server/ on 2026-09-27 and must keep working wherever the repo is mounted.
REPL_ROOT = Path(__file__).resolve().parent.parent.parent
repl_gateway_path = REPL_ROOT / "src" / "main" / "scala" / "repl" / "repl_backend_gateway.scala"
isabelle_executable = Path(os.environ.get("ISABELLE_HOME", "/opt/isabelle")) / "bin" / "isabelle"


#: Environment variable that marks every process of one gateway (see terminate).
GATEWAY_MARK_KEY = "ISABELLE_POOL_GATEWAY_MARK"


def _gateway_log_dir() -> Path:
    d = Path(Logging.LOG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


EnvStateID = int


class Outputs(Protocol):
    """Protocol for separated outputs from the Scala backend."""

    def output(self) -> str: ...
    def error(self) -> str: ...


class ReplResult(Protocol):
    # pylint: disable=missing-docstring
    """Protocol for results from the Scala backend."""

    def separated_output(self) -> Outputs: ...
    def total_output(self) -> str: ...


# pylint: disable=missing-docstring
class ReplBackend(Protocol):
    """Protocol matching the Scala ``repl.ReplBackend`` class.

    Every public method on the Scala side should have an entry here so that
    static type checkers can catch interface drift early.
    """

    # --- lifecycle (backend_lifecycle.scala) ---
    def current_thy_name_string(self) -> str: ...
    def enter_thy(self, input_thy_name: str) -> ReplResult: ...
    def reset(self) -> ReplResult: ...
    def exit(self) -> None: ...
    def get_cache_status(self) -> str: ...
    def get_cache_stats(self) -> py4j.java_collections.JavaMap[str, int]: ...
    def is_session_valid(self) -> bool: ...
    def recreate_session_if_needed(self) -> None: ...

    # --- probes (backend_probes.scala) ---
    def open_subgoals(self) -> py4j.java_collections.JavaList[str]: ...
    def in_proof(self) -> bool: ...
    def local_facts(self) -> py4j.java_collections.JavaList[str]: ...
    def global_facts(self, limit: int) -> py4j.java_collections.JavaList[str]: ...
    def sledgehammer(self, timeout_s: int) -> py4j.java_collections.JavaList[str]: ...
    # Memory facts of the session's ML process: pid (Linux) + Poly/ML runtime
    # statistics as key -> value; empty before the first theory command.
    def mem_stats(self) -> py4j.java_collections.JavaMap[str, str]: ...
    def session_phase(self) -> str: ...
    def uncovered_imports(self, session_theories: str, requested: str) -> str: ...

    def get_proof_state(self) -> ReplResult: ...
    def probe_transient(self, isar_string: str, wall_budget_ms: int) -> ReplResult: ...
    def get_source(self) -> ReplResult: ...

    # --- chunk ops (backend_chunk_ops.scala) ---
    # step: wall-bounded; on budget expiry the inserted command is ROLLED BACK
    # (transactional, like verify_chunk) and the result carries a timeout error.
    def step(self, isar_string: str, wall_budget_ms: int) -> ReplResult: ...
    def verify_chunk(self, isar_string: str, wall_budget_ms: int) -> str: ...
    def rollback(self) -> ReplResult: ...
    def save_state(self) -> EnvStateID: ...
    def restore_state(self, state_id: EnvStateID) -> bool: ...
    def vector_step(self, isar_strings: py4j.java_collections.JavaList[str]) -> ReplResult: ...
    def vectorise(self, size: int) -> None: ...
    def scalarise(self, index_to_keep: int) -> None: ...

    # --- file ops (backend_file_ops.scala) ---
    def step_chunk_report(self, isar_string: str, wall_budget_ms: int, probe_state: bool) -> str: ...
    def sync_document(self, text: str, wall_budget_ms: int, probe_state: bool) -> str: ...
    def command_at_line(self, line: int) -> str: ...
    def goals_at_line(self, line: int) -> str: ...
    def hover_at(self, line: int, col: int) -> str: ...
    def definition_at(self, line: int, col: int) -> str: ...
    def sledgehammer_at(self, line: int, subgoal: int, timeout_s: int) -> str: ...
    # fill_sorries: host = the command with report index `index`; JSON with subgoals.
    def sledgehammer_at_command(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, index: int, subgoal: int, timeout_s: int, max_proofs: int, provers: str
    ) -> str: ...


class ReplBackendGatewayProcess:
    """
    Class to manage an instance of the Scala REPL gateway process.
    """

    # Configurable via subclass or monkey-patch; avoids hardcoded magic numbers.
    POLL_INTERVAL: float = Repl.GATEWAY_POLL_INTERVAL
    POLL_TIMEOUT: float = Repl.GATEWAY_POLL_TIMEOUT
    TERMINATE_WAIT: float = Repl.GATEWAY_TERMINATE_WAIT

    def __init__(self) -> None:
        # pylint: disable=consider-using-with, subprocess-popen-preexec-fn
        log_dir = _gateway_log_dir()
        # JVM stderr goes straight to a durable append log; JVM stdout is
        # pumped there too once the Py4J port line has been consumed. This
        # survives server restarts (unlike the server's own stdout file).
        self._jvm_log = open(  # noqa: SIM115 — lives as long as the process
            log_dir / "gateway-jvm.log", "a", buffering=1, encoding="utf-8"
        )
        # Every process this gateway starts inherits the marker (provers too,
        # also after the JVM died and init adopted them): terminate() finds and
        # ends them by it, without the JVM and without trusting recorded pids.
        self.marker = uuid.uuid4().hex
        self.process = subprocess.Popen(
            [isabelle_executable, "scala", str(repl_gateway_path)],
            env={**os.environ, GATEWAY_MARK_KEY: self.marker},
            # Isabelle uses many child processes, so we start a new process group
            preexec_fn=os.setsid,
            # Port number will be passed via stdout
            stdout=subprocess.PIPE,
            stderr=self._jvm_log,
            text=True,
        )
        if self.process.stdout is None:
            raise RuntimeError(
                "Scala REPL gateway process failed to start (stdout is None)."
            )
        port = self._read_port(self.process.stdout)
        # Keep the pipe open and pump the JVM's remaining stdout into the
        # durable log — closing it here (the old behavior) silently discarded
        # everything the JVM printed after startup.
        threading.Thread(
            target=self._pump_stdout, args=(self.process.stdout,),
            name="gateway-jvm-stdout-pump", daemon=True,
        ).start()
        self.process.stdout = None
        self.gateway = JavaGateway(
            gateway_parameters=GatewayParameters(
                port=port, read_timeout=Repl.PY4J_READ_TIMEOUT
            )
        )

    def _read_port(self, stream) -> int:
        """The Py4J port: the first all-digit line of the JVM's stdout. The JVM can
        print other lines before it, e.g. a hsperfdata warning when its pid collides
        with a JVM in another pid namespace; those go to the log instead of being
        parsed as the port (that used to fail the server start)."""
        for line in stream:
            text = line.strip()
            if text.isdigit():
                return int(text)
            self._jvm_log.write(line)
        raise RuntimeError(
            "Scala REPL gateway exited before printing its port; see gateway-jvm.log."
        )

    def _pump_stdout(self, stream) -> None:
        try:
            for line in stream:
                self._jvm_log.write(line)
        except (ValueError, OSError):
            pass  # stream closed during shutdown

    def has_terminated(self) -> bool:
        """Check if the Scala REPL gateway process has terminated."""
        return self.process.poll() is not None

    #: Seconds the wedge probe may take before the gateway counts as dead.
    PROBE_TIMEOUT: float = 5.0

    def is_alive(self) -> bool:
        """Functional liveness: process up AND the gateway answers the wedge
        probe (docs/ISSUES.md Bug 9 — a JVM can be alive with its Event_Timer
        dead, which process liveness alone cannot see).

        The probe is bounded: a JVM that never answers counts as dead. Any
        error (including an older gateway lacking the ``alive`` method) reads
        as not-alive, so recovery replaces the gateway with one that has it.
        """
        if self.has_terminated():
            logger.error(
                "gateway JVM terminated exit_code=%s (see logs/gateway-jvm.log "
                "and gateway-jvm-gc.log for the cause)",
                self.process.returncode,
            )
            return False
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                self.gateway.jvm.repl.ReplBackendGateway.alive
            )
            alive = bool(future.result(timeout=self.PROBE_TIMEOUT))
            if not alive:
                # Why: a thread died of an OutOfMemoryError (the JVM marks itself
                # dead), or the Event_Timer is wedged (Bug 9). Bounded like the probe.
                try:
                    reason = pool.submit(
                        self.gateway.jvm.repl.ReplBackendGateway.fatal_error
                    ).result(timeout=self.PROBE_TIMEOUT)
                except Exception:
                    reason = ""
                logger.error(
                    "gateway liveness probe answered false: %s",
                    reason or "Event_Timer wedged (docs/ISSUES.md Bug 9)",
                )
            return alive
        except Exception as e:
            logger.warning(
                "gateway liveness probe failed: %s: %s", type(e).__name__, e
            )
            return False
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Generic polling helper — all get_repl_backend_* variants delegate
    # to this to avoid the 6× copy-paste that previously existed.
    # ------------------------------------------------------------------

    def _poll_gateway(self, method_name: str, *args) -> ReplBackend:
        """Call a factory method on the Scala ``ReplBackendGateway`` object,
        retrying on ``Py4JNetworkError`` until the gateway is ready.

        Parameters
        ----------
        method_name : name of the static method on ``repl.ReplBackendGateway``
        *args : positional arguments forwarded to the Scala method
        """
        start_time = time.time()
        while time.time() - start_time < self.POLL_TIMEOUT:
            if self.has_terminated():
                raise RuntimeError("Scala REPL gateway process has terminated.")
            try:
                factory = getattr(
                    self.gateway.jvm.repl.ReplBackendGateway, method_name
                )
                return factory(*args)
            except py4j.protocol.Py4JNetworkError:
                time.sleep(self.POLL_INTERVAL)
        raise RuntimeError(
            f"Failed to call ReplBackendGateway.{method_name} "
            f"within {self.POLL_TIMEOUT}s."
        )

    # ------------------------------------------------------------------
    # Public factory methods — signatures match the Scala gateway exactly.
    #
    # Scala source of truth:
    #   repl/src/main/scala/repl/repl_backend_gateway.scala
    # ------------------------------------------------------------------

    def get_repl_backend(self, show_states: bool) -> ReplBackend:
        # Scala: get_repl_backend(show_states: Boolean)
        return self._poll_gateway("get_repl_backend", show_states)

    def get_repl_backend_with_cache(
        self, show_states: bool, enable_cache: bool
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_cache(show_states: Boolean, enable_cache: Boolean)
        # NOTE: the old Python version incorrectly passed a `field` arg here.
        return self._poll_gateway(
            "get_repl_backend_with_cache", show_states, enable_cache
        )

    def get_repl_backend_with_full_cache_config(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_full_cache_config(
        #     show_states, enable_cache, max_cache_size, field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_full_cache_config",
            show_states, enable_cache, max_cache_size, field,
        )

    def get_repl_backend_with_memory_management(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_memory_management(
        #     show_states, enable_cache, max_cache_size, field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_memory_management",
            show_states, enable_cache, max_cache_size, field,
        )

    def get_repl_backend_with_initial_theories(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        initial_thys: "py4j.java_collections.JavaList[str]",
        field: str = "HOL",
        session_dirs: "py4j.java_collections.JavaList[str] | None" = None,
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_initial_theories(
        #     show_states, enable_cache, max_cache_size,
        #     initial_thys: java.util.List[String], field: String = "HOL",
        #     session_dirs: java.util.List[String] = List.of())
        # session_dirs: session ROOT directories for starting on a user heap
        # (heap pool, Stage 3) — like `isabelle build -d`.
        return self._poll_gateway(
            "get_repl_backend_with_initial_theories",
            show_states, enable_cache, max_cache_size,
            initial_thys, field,
            session_dirs if session_dirs is not None else
            self.gateway.jvm.java.util.List.of(),
        )

    def get_repl_backend_with_shared_cache(
        self,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        initial_thys: "py4j.java_collections.JavaList[str]",
        field: str = "HOL",
    ) -> ReplBackend:
        # Scala: get_repl_backend_with_shared_cache(
        #     show_states, enable_cache, max_cache_size,
        #     initial_thys: java.util.List[String], field: String = "HOL")
        return self._poll_gateway(
            "get_repl_backend_with_shared_cache",
            show_states, enable_cache, max_cache_size,
            initial_thys, field,
        )
    
    # Tagged backends (repl_backend_gateway.scala, "Tagged backends and the
    # reaper"): the server tags each backend with its session id, so a backend it
    # gave up on, or no longer holds, can be stopped without a handle to it.
    def get_repl_backend_tagged(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        tag: str,
        show_states: bool,
        enable_cache: bool,
        max_cache_size: int,
        initial_thys: "py4j.java_collections.JavaList[str]",
        field: str,
        session_dirs: "py4j.java_collections.JavaList[str]",
    ) -> ReplBackend:
        return self._poll_gateway(
            "get_repl_backend_tagged",
            tag, show_states, enable_cache, max_cache_size,
            initial_thys, field, session_dirs,
        )

    def close_backend(self, tag: str) -> bool:
        """Stop (in the JVM, in the background) the backend tagged ``tag``."""
        return bool(self.gateway.jvm.repl.ReplBackendGateway.close_backend(tag))

    def reap_fence(self) -> int:
        return int(self.gateway.jvm.repl.ReplBackendGateway.reap_fence())

    def reap_backends(self, live: "list[str]", fence: int) -> "list[str]":
        """Stop every tagged backend registered at or before ``fence`` whose tag
        is not in ``live``; returns the tags stopped."""
        java_live = self.gateway.jvm.java.util.ArrayList()
        for tag in live:
            java_live.add(tag)
        reaped = self.gateway.jvm.repl.ReplBackendGateway.reap_backends(java_live, fence)
        return [str(t) for t in reaped]

    def _signal_group(self, sig: int) -> None:
        """Send ``sig`` to the gateway's whole process group.

        The gateway is started with ``preexec_fn=os.setsid`` so that the
        ``isabelle scala`` launcher, the JVM it spawns, and every Isabelle
        child live in one process group. Signalling only ``self.process``
        reaches the launcher shell but leaves the JVM grandchild orphaned
        (reparented to init), which leaks a multi-GB heap on every shutdown.
        Signalling the group takes the whole tree down together.
        """
        try:
            pgid = os.getpgid(self.process.pid)
        except (ProcessLookupError, OSError):
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, OSError):
                pass
        # Fallback: group already gone or getpgid failed — signal the process.
        try:
            self.process.send_signal(sig)
        except (ProcessLookupError, OSError, ValueError):
            pass

    def terminate(self) -> None:
        """Gracefully terminate the Scala REPL gateway and all its children.

        Sends SIGTERM to the gateway process group, waits up to
        ``TERMINATE_WAIT`` for a clean exit, then SIGKILLs the group as a
        backstop so no orphaned JVM/Isabelle processes survive.
        """
        # Step 1: Shutdown Py4J gateway (tells Scala we're done).
        try:
            self.gateway.shutdown()
            print("Gateway shutdown initiated")
        except Exception as e:
            print(f"Gateway shutdown warning: {e}")

        # Step 2: Give Isabelle time to cleanup threads gracefully.
        time.sleep(0.5)

        # Step 3: SIGTERM the whole process group (not just the launcher shell).
        self._signal_group(signal.SIGTERM)
        print("Terminate signal sent to gateway process group")

        # Step 4: Wait for graceful shutdown.
        wait_start_time = time.time()
        while time.time() - wait_start_time < self.TERMINATE_WAIT:
            if self.process.poll() is not None:
                print(
                    f"Gateway leader exited after {time.time() - wait_start_time:.2f}s"
                )
                break
            time.sleep(0.1)
        else:
            print(
                f"Gateway leader did not exit after {self.TERMINATE_WAIT}s"
            )

        # Step 5: SIGKILL the group unconditionally as a backstop. If the group
        # already exited this is a no-op (ESRCH is swallowed); if the JVM
        # grandchild outlived its launcher, this reaps it instead of leaking it.
        self._signal_group(signal.SIGKILL)
        try:
            self.process.wait(timeout=2)
        except Exception:
            pass
        # Step 6: end the prover trees that outlived the JVM. They are in their
        # own process groups (bash_process calls setsid), so the group signals
        # above do not reach them, and once the JVM is gone (killed here, or
        # OOM-killed before recovery) init has adopted them: they are found by
        # the marker every process of this gateway inherited.
        marker = getattr(self, "marker", None)
        left = process_tree.marked(GATEWAY_MARK_KEY, marker) if marker else []
        survivors = process_tree.kill_trees(left, grace=self.TERMINATE_WAIT)
        if survivors:
            logger.warning(
                "ended %d process(es) left behind by the gateway JVM", len(survivors)
            )
        print("Gateway process group terminated")
