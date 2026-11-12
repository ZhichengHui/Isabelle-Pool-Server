# IsabelleGym Server — Issues, Fixes & Work Log

**Started:** 2026-06-04 · **Last structural update:** 2026-10-05
**Name:** the project was *IsabelleGym Server* until 2026-10-05 and is now *Isabelle Pool Server*; entries below keep the name in use at the time (metric names `isabellegym_*`, image `isabellegym-isabelle-gym`, class `IsabelleGymAsyncClient` → now `isabelle_pool_server_*`, `isabelle-pool-server`, `PoolAsyncClient`).
**Scope:** server layer (`server/app/`), REPL backend (`server/repl/`), MCP servers (`mcp_servers/`), deployment (`deploy/`).

How this file works: every confirmed defect gets a numbered **Bug N** section (symptom, root cause,
fix, how it was verified). Audit findings that are not yet promoted live in the
[Open Findings Tracker](#open-findings-tracker); the dated
[Agent Work Log](#agent-work-log-dated) at the end records what was done when, with the
internal working notes (one folder per task, gitignored, local) that hold the artifacts. Design rationale is
in `DESIGN_CHOICES.md`, not here.

---

## Table of Contents

1. [Memory Management / Session Closing](#memory-management--session-closing) — the close call chain and Bugs 1–5
   - [Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED](#bug-1-race-condition-in-threadedbackendclose--resolved)
   - [Bug 2: Join Timeout Too Short — RESOLVED](#bug-2-join-timeout-too-short--resolved)
   - [Bug 3: Leased Sessions Never Idle-Evicted — RESOLVED](#bug-3-leased-sessions-never-idle-evicted--resolved)
   - [Bug 4: TOCTOU on `in_use` Check — RESOLVED](#bug-4-toctou-on-in_use-check--resolved)
   - [Bug 5: Isabelle Processes Persist After Close — RESOLVED (not reproducible)](#bug-5-isabelle-processes-persist-after-close--resolved-not-reproducible)
- [Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED](#bug-6-gateway-oom-under-concurrent-sledgehammer--resolved)
- [Bug 7: Stale `isabelle_user_data` Volume Shadows Component Registration After Image Rebuild — RESOLVED](#bug-7-stale-isabelle_user_data-volume-shadows-component-registration-after-image-rebuild--resolved)
- [Bug 8: `close()` Rejects Its Own `exit` Job — Sessions Never Torn Down — RESOLVED](#bug-8-close-rejects-its-own-exit-job--sessions-never-torn-down--resolved)
- [Bug 9: Gateway JVM `Event_Timer` Cancelled — Server Wedges, Recovery Blind — RESOLVED](#bug-9-gateway-jvm-event_timer-cancelled--server-wedges-recovery-blind--resolved)
- [Bug 10: `GET /api/v1/sessions` Leaks `lease_id`s — Destroy Authorization Bypassable — RESOLVED](#bug-10-get-apiv1sessions-leaks-lease_ids--destroy-authorization-bypassable--resolved)
- [Bug 11: `ISABELLE_SCALA_JAVA_OPTIONS` Is Dead Config — JVM Options Never Reach the Gateway — RESOLVED](#bug-11-isabelle_scala_java_options-is-dead-config--jvm-options-never-reach-the-gateway--resolved)
- [Bug 12: Unauthenticated ML Execution Through Every Text Endpoint — RESOLVED](#bug-12-unauthenticated-ml-execution-through-every-text-endpoint--resolved)
- [Bug 13: Heap-Pool Path Traversal and Unauthenticated Destructive Heap Endpoints — RESOLVED](#bug-13-heap-pool-path-traversal-and-unauthenticated-destructive-heap-endpoints--resolved)
- [Bug 14: State Probes Leaked `ML_val` Into the Document — RESOLVED (by design change)](#bug-14-state-probes-leaked-ml_val-into-the-document--resolved-by-design-change)
- [Bug 15: Unbounded Settle Waits Wedge the Session Worker — RESOLVED](#bug-15-unbounded-settle-waits-wedge-the-session-worker--resolved)
- [Bug 16: Idle-Cleanup Sweep Blocked the Event Loop — RESOLVED](#bug-16-idle-cleanup-sweep-blocked-the-event-loop--resolved)
- [Bug 17: LSP MCP Scratch-Slot Leak Hung `multi_attempt` / `run_code` for the Rest of a Run — RESOLVED](#bug-17-lsp-mcp-scratch-slot-leak-hung-multi_attempt--run_code-for-the-rest-of-a-run--resolved)
- [Bug 18: Diff-to-Edit Offsets Corrupted the Document on Incremental Sync and Checkpoint Restore — RESOLVED](#bug-18-diff-to-edit-offsets-corrupted-the-document-on-incremental-sync-and-checkpoint-restore--resolved)
- [Bug 19: A Bogus File Path Pinned a Leased Session in the LSP MCP — RESOLVED](#bug-19-a-bogus-file-path-pinned-a-leased-session-in-the-lsp-mcp--resolved)
- [Bug 20: Concurrent First Calls on One File Created Two Sessions and Released One Mid-Use — RESOLVED](#bug-20-concurrent-first-calls-on-one-file-created-two-sessions-and-released-one-mid-use--resolved)
- [Bug 21: LSP File Bindings Reused Dirty Sessions — RESOLVED](#bug-21-lsp-file-bindings-reused-dirty-sessions--resolved)
- [Bug 22: Checkpoint Restore Reported Success for Unknown or Invalidated Ids — RESOLVED](#bug-22-checkpoint-restore-reported-success-for-unknown-or-invalidated-ids--resolved)
- [Bug 23: Theory-Name Injection Through `enter_theory` and the Theory-Name Fields — RESOLVED](#bug-23-theory-name-injection-through-enter_theory-and-the-theory-name-fields--resolved)
- [Bug 24: Thirteen LSP MCP Tools Shipped With an Empty Description — RESOLVED](#bug-24-thirteen-lsp-mcp-tools-shipped-with-an-empty-description--resolved)
- [Bug 25: `enter_theory` Accepted Imports Outside the Session's Context — RESOLVED](#bug-25-enter_theory-accepted-imports-outside-the-sessions-context--resolved)
- [Bug 26: A Session Sorted Its Imports, So a Short Name Could Mean Another Constant — RESOLVED](#bug-26-a-session-sorted-its-imports-so-a-short-name-could-mean-another-constant--resolved)
- [Open Findings Tracker](#open-findings-tracker)
- [Summary of Changes](#summary-of-changes) (Bugs 1–6)
- [Agent Work Log (dated)](#agent-work-log-dated)

---

## Memory Management / Session Closing

The full close call chain (current code) is:

```
HTTP DELETE /sessions/{id}
  → asyncio.to_thread(session_manager.close_session(...))   [session_manager.py:497]
  → session.close()                                         [session.py:562]
  → threaded_backend.close()                                [threaded_backend.py:62]
  → submit(self._backend.exit) on the worker thread         [threaded_backend.py:64]
  → ReplBackend.exit()   ← Py4J call to Scala               [repl_backend.scala:157]
  → Repl_ML_Communication.clear_channel(channel_id)
  → session_manager_instance.shutdown()                     [session_manager.scala:400]
  → remove_session_async(...) per running session  +  Server_Utils.stop_server(...)
```

**Status summary (2026-10-17):** memory awareness landed on `Impl-memory-robustness` (research gaps G1–G9 of
`claude-work/2026-10-16-research-memory-mcp-awareness/REPORT.md`): the gate now reads `/proc/meminfo` on
native hosts (it read 0 — G7), builds are memory-gated with their own concurrency knob (G3), per-session
attribution from the ML side, request-path prover-death detection, structured 503 facts + `X-Memory-State`
+ `/api/v1/memory`, budget admission with reservations, size-aware eviction of unleased sessions, and the
MCP registry/advice/reminder layer (G4, G5). Bugs 1–6 are resolved in the current code. Bug 5 (Isabelle OS processes lingering after close) was re-checked on 2026-06-04 and is **not reproducible** — clean reaping verified for normal close and close-during-sledgehammer; the accumulation-to-OOM it described is explained by the now-fixed gateway-orphan-on-shutdown (`killpg`, internal working notes) and gateway-OOM-under-concurrency (Bug 6) issues. A narrow optional hardening item remains (force-kill fallback for a genuinely wedged ML process). See the [Claude Work Log](#internal working notes-log-dated) for the dated history.

---

### Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — current code matches the recommended fix.

#### Original root cause

The old `close()` called `self._backend.exit()` directly from the calling thread, bypassing the single-worker serialisation. Because the Py4J gateway uses one socket connection and the Scala `ReplBackend` is not thread-safe, that could collide with an in-flight job on the worker thread (`Py4JNetworkError` / corrupted Scala state).

#### Verification (current code)

`exit()` is now submitted through the job queue and awaited, so it is serialised after any pending job — exactly the recommended fix:

```python
def close(self) -> None:
    logger.info("closing threaded backend worker=%s", self._name)
    exit_fut = self.submit(self._backend.exit)   # queued, not called directly
    try:
        exit_fut.result(timeout=self.EXIT_TIMEOUT)
    except Exception:
        logger.exception("backend exit raised during close worker=%s", self._name)
    finally:
        self._stop.set()
        self._t.join(timeout=self.JOIN_TIMEOUT)
        logger.info("threaded backend closed worker=%s", self._name)
```

No further action needed.

---

### Bug 2: Join Timeout Too Short — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — timeouts are now configurable and generous.

#### Original root cause

The old code used `self._t.join(timeout=2.0)`. The Scala `shutdown()` (remove sessions, join async removals, stop the Isabelle server) routinely takes 5–30 s for any non-trivial theory, so the join timed out and `close()` returned while the worker (and Isabelle subprocess) was still alive.

#### Verification (current code)

The class reads its timeouts from config (`server/app/core/config.py`), defaulting to values long enough for the Scala shutdown:

```python
class ThreadedBackend:
    EXIT_TIMEOUT: float = Repl.BACKEND_EXIT_TIMEOUT   # ISABELLE_BACKEND_EXIT_TIMEOUT, default 60.0
    JOIN_TIMEOUT: float = Repl.BACKEND_JOIN_TIMEOUT   # ISABELLE_BACKEND_JOIN_TIMEOUT, default 5.0
    QUEUE_POLL_TIMEOUT: float = Repl.BACKEND_QUEUE_POLL  # default 0.1
```

`exit_fut.result(timeout=EXIT_TIMEOUT)` waits up to 60 s for the Scala shutdown, and the subsequent join is for a loop that has already stopped. No further action needed.

> Caveat: this guarantees the *Python* call waits for `exit()` to *return*. It does not by itself guarantee the Isabelle OS process actually died — see Bug 5.

---

### Bug 3: Leased Sessions Never Idle-Evicted — RESOLVED

**File:** `server/app/services/session_manager.py`, `cleanup_idle_sessions()`
**Severity:** Medium
**Status:** ✅ Resolved — current code force-evicts abandoned leased sessions older than `self.max_lease_age` (`Server.MAX_LEASE_AGE`, env `ISABELLE_MAX_LEASE_AGE`), exactly as in the fix below.

#### Original root cause

```python
async def cleanup_idle_sessions(self) -> None:
    while True:
        await asyncio.sleep(self.cleanup_interval)
        ...
        for sid, session in list(self._lru.items()):
            if session.leased:
                continue  # never evict a leased session   <-- unconditional
```

If a client acquires a lease and then disconnects (crash, network failure, killed process), the session is never reclaimed — a steady leak for ML training loops that lease many sessions. This is a likely contributor to pool exhaustion (`PoolExhausted` / HTTP 503 on `acquire`).

#### Fix

Force-close leases that have been idle longer than `idle_timeout * 2`. The helpers used below (`session.is_idle`, `session.last_activity`, `session.lease_id`, `session.status`, `close_session(require_lease=False)`) all exist in the current code.

```python
async def cleanup_idle_sessions(self) -> None:
    max_lease_age = self.idle_timeout * 2  # abandoned leases reclaimed after this

    while True:
        await asyncio.sleep(self.cleanup_interval)
        now = time.time()
        to_close: List[uuid.UUID] = []

        with self._lock:
            for sid, session in list(self._lru.items()):
                if session.status == SessionStatus.CLOSED:
                    continue
                if session.leased:
                    if session.is_idle(max_lease_age, now=now):
                        logger.warning(
                            "force-closing abandoned leased session "
                            "session_id=%s lease_id=%s idle_for=%.0fs",
                            sid, session.lease_id, now - session.last_activity,
                        )
                        to_close.append(sid)
                    continue
                if session.is_idle(self.idle_timeout, now=now):
                    to_close.append(sid)

        for sid in to_close:
            logger.info("closing idle session session_id=%s", sid)
            try:
                self.close_session(sid, require_lease=False)
            except Exception:
                logger.exception("failed to close idle session session_id=%s", sid)
```

Optionally make `max_lease_age` configurable via `ISABELLE_MAX_LEASE_AGE_SECONDS` in `server/app/core/config.py`.

> Note: also confirm `start_cleanup_task()` is actually invoked at startup — the cleanup coroutine only runs if `start_cleanup_task()` is called (e.g. from the FastAPI lifespan). If it is never started, *no* idle eviction happens at all (leased or not).

---

### Bug 4: TOCTOU on `in_use` Check — RESOLVED

**File:** `server/app/services/session_manager.py`, `close_session()`
**Severity:** Low
**Status:** ✅ Resolved — current code re-checks `in_use` after `pop()` and restores + raises `SessionBusyError` ("became busy between in_use check and pop"), as in the fix below.

#### Original root cause

```python
with self._lock:
    session = self._lru.get(sid)
    ...
    if session.in_use:
        raise SessionBusyError(...)
    self._lru.pop(sid, None)        # no re-check after pop
    if session.leased:
        session.release_lease()
```

`in_use` reads `_active_requests` under `_active_requests_lock`, not under `_lock`, so a concurrent handler could call `_acquire_request()` between the check and the `pop()`. The window is microseconds and unlikely in a single-server deployment, but it is a logical correctness issue.

#### Fix

Re-check `in_use` after `pop()`; if a request slipped through, restore the session and raise:

```python
with self._lock:
    session = self._lru.get(sid)
    if session is None:
        raise SessionNotFound(f"Session {sid} not found")
    if session.status == SessionStatus.CLOSED:
        raise SessionNotFound(f"Session {sid} is closed")
    if require_lease:
        session.require_lease(lease_id)
    if session.in_use:
        raise SessionBusyError(f"Session {sid} is busy and cannot be closed")
    self._lru.pop(sid, None)
    if session.in_use:                       # re-check after removing from LRU
        self._lru[sid] = session             # restore
        self._lru.move_to_end(sid)
        raise SessionBusyError(
            f"Session {sid} became busy between in_use check and pop"
        )
    if session.leased:
        session.release_lease()
```

Once `pop()` succeeds, no new `get_session()` can return this session, so no new `_acquire_request()` can start; only a request already past `get_session()` but not yet at `_acquire_request()` remains a (tiny) residual risk.

---

### Bug 5: Isabelle Processes Persist After Close — RESOLVED (not reproducible)

**Files:** `repl/src/main/scala/repl/session_manager.scala` (`shutdown`, `remove_session_async`), `repl/src/main/scala/repl/server_utils.scala` (`stop_server`, `stop_session`)
**Severity:** High (operational — causes OOM over time)
**Status:** ✅ Resolved — re-checked 2026-06-04 and **not reproducible** in current code; the accumulation-to-OOM symptom is explained by two *other* issues fixed since (gateway orphan on shutdown → `killpg`, internal working notes; gateway OOM under concurrency → Bug 6). Details in the internal working notes.

#### Verification (2026-06-04, container `isabelle-gym`)

Each `ReplBackend` owns its own Scala `Session_Manager` + Isabelle server, so a close
(`session.close() → ReplBackend.exit() → session_manager_instance.shutdown() → stop_server`)
tears down that backend's whole Isabelle server and its `poly` process. Measured:

- **Normal create → work → close:** poly 2 → 4 (after create) → **2 within ~2 s** of `DELETE`, stable.
- **Close WHILE sledgehammer runs (lead #3):** (poly, provers) = (2,0) baseline → (4,3) mid-run → **(2,0) within 1 s** of `DELETE`, stable for 20 s. The forked sledgehammer thread and its ATP subprocesses (`eprover`/`z3`/`cvc5`/`vampire`) are reaped together with the session.

**Residual (separate hardening, not the OOM symptom):** a session whose ML process is
*genuinely wedged* (a non-terminating tactic that ignores interrupts) may not respond to
`Server.exit`, and there is no OS-level force-kill fallback (`kill -9` on the tracked
server PID). Tracked as an optional follow-up below.

#### Original leads (investigated; symptom no longer present)

1. **Async removals may outrace server stop.** `Session_Manager.shutdown()` forks `remove_session_async` per running session, then joins `pending_removals`, then calls `Server_Utils.stop_server`. Each backend has its **own** `Session_Manager` and therefore its **own** Isabelle server process (`Server.init` in `start_server`). Confirm every `remove_session_async` future is actually in `pending_removals` before the join (it is added synchronously today) and that `stop_session` (`Server_Commands.Session_Stop`) returns a success `return_code` rather than erroring out silently.

2. **`Server.exit(name)` may not kill the OS process.** `stop_server` calls `Server.exit(server_info.name)`, which asks the Isabelle server to shut down over its socket. If the server process (or a child `poly`/ML process) is mid-computation or wedged, it may not terminate. Verify with `ps` inside the container before/after a close that the specific server PID actually exits.

3. **A forked sledgehammer thread can hold the ML process open.** The standalone sledgehammer channel forks an `Isabelle_Thread` (see internal working notes). If a session is closed while that thread is still running a prover, the ML process may refuse to exit until the thread (or its external ATP subprocesses) finishes. Reproduce by closing a session immediately after firing sledgehammer and checking for leftover prover/`poly` processes.

4. **No OS-level reaping.** There is no fallback that force-kills a server PID if graceful `Server.exit` fails. Consider tracking the spawned server PID and `kill`-ing it (and orphaned ATP children) as a last resort during `shutdown()`.

#### Optional follow-up (hardening only)

The poly-probe check above (internal working notes) confirmed normal and
sledgehammer-interrupt closes reap cleanly. The only remaining gap is a last-resort
**force-kill fallback** in `stop_server`/`shutdown` that `kill -9`s the tracked Isabelle
server PID (and orphaned ATP children) if graceful `Server.exit` does not return within a
timeout — to cover a genuinely wedged ML process. Low priority; not the OOM symptom.

---

### Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED

**Files:** `server/app/core/config.py`, `server/app/services/session_manager.py`, `server/app/api/v1/router.py`
**Severity:** High (operational — bricked the whole server)
**Status:** ✅ Resolved — fixed and verified 2026-06-04 (scaling notes in the internal working notes).

#### Root cause

Found by the sledgehammer scaling harness (internal working notes). At ~16 concurrent `sledgehammer` calls, the single shared Py4J **gateway JVM** was OOM-killed by the kernel:

```
scala: line 68: 15680 Killed   ".../java" -Xmx4g ... repl_backend_gateway.scala
py4j.java_gateway: An error occurred while trying to connect to the Java server (127.0.0.1:39143)
```

Each `sledgehammer` is itself a multi-prover parallel job (balloons its `poly` heap + forks several ATP processes). A burst of them spikes memory; the OOM killer takes the gateway JVM; and because there was **no gateway recovery**, every subsequent request returned HTTP 500 — the server stayed bricked until manual restart. The Python cgroup admission gate (Bug-fix from 2026-06-03, see Work Log) did not prevent this: it gates memory at session-*create* time, but the spike is from *running* a heavy op on already-admitted idle sessions.

#### Fix (two parts)

1. **Concurrency semaphore** — bound in-flight sledgehammers server-wide.
   - `config.py`: `Server.MAX_CONCURRENT_SLEDGEHAMMER` (env `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER`, default `~cores/8`, i.e. 4 on a 32-core box — tracks the empirical throughput knee).
   - `session_manager.py`: `self.sledgehammer_sem = asyncio.Semaphore(...)`.
   - `router.py`: the sledgehammer endpoint runs under `async with session_manager.sledgehammer_sem:`; excess requests queue (backpressure) instead of oversubscribing.
2. **Gateway health-check + auto-restart** — `session_manager.py`:
   - `gateway_alive()` (via `ReplBackendGatewayProcess.has_terminated()`).
   - `_ensure_gateway()` now detects a dead gateway and calls `_recover_gateway_locked()` — purges the now-invalid sessions and rebuilds the gateway — so the next request recovers instead of 500-ing.
   - the background cleanup loop also recovers a dead gateway proactively.
   - `GET /` reports `gateway_alive` (status `degraded` when false) and `max_concurrent_sledgehammer`.

#### Verification (2026-06-04, container `isabelle-gym`, 32 cores)

- **Semaphore:** re-running the harness at W=16 (which previously OOM-killed the gateway) now completes **32/32** with memory flat at ~3.2 GB; no 500s, isolation still PASS.
- **Auto-restart:** the gateway-recovery script (internal working notes) SIGKILLs the gateway process group → `GET /` shows `status=degraded, gateway_alive=false` → `POST /sessions` returns **200** (auto-recovered) → `GET /` shows `status=healthy, gateway_alive=true`. Previously this was 500-forever.

### Bug 7: Stale `isabelle_user_data` Volume Shadows Component Registration After Image Rebuild — RESOLVED

**Files:** `Dockerfile`, `docker-compose.yml`, `repl/Admin/init`, `repl/Admin/container_init.sh`
**Severity:** Medium (operational — server cannot start after an image rebuild when the old volume exists)
**Status:** ✅ Fixed permanently 2026-08-08 — the container entrypoint `repl/Admin/container_init.sh` re-runs the idempotent `./repl/Admin/init` on every container start (wired via `command:` in `docker-compose.yml`), so a stale volume can no longer shadow build-time registration. (Previously worked around 2026-06-10 by manually re-running `./repl/Admin/init` inside the container.) 2026-10-07: the gate in `container_init.sh` moved into `server/repl/Admin/init` itself (fast path when the volume is registered); the entrypoint calls `init` directly and `container_init.sh` is gone.

#### Symptom

After rebuilding the image (`docker compose build isabelle-gym`) and starting a fresh container, `python -m server.app.main` dies during startup with:

```
-- [E006] Not Found Error: .../repl_backend_gateway.scala:4:7
4 |import py4j.GatewayServer
  |       Not found: py4j
...
ValueError: invalid literal for int() with base 10: ''   # gateway printed no port
server.app.errors.GatewayUnavailable: ... failed to start REPL gateway
```

#### Root cause

The Dockerfile runs `./repl/Admin/init` at build time, which registers the `repl` component (and downloads `py4j`/`spliff` contribs) under `/root/.isabelle`. But docker-compose mounts the named volume `isabelle_user_data` over `/root/.isabelle`, and Docker only copies image content into a named volume **when the volume is empty**. Any pre-existing volume (from a previous image) therefore shadows the build-time registration: the new container sees the *old* volume's state, `isabelle scala` finds no `py4j`/`repl.jar` on its classpath, the gateway script fails to compile, prints no port, and the server exits.

This bites every time the image is rebuilt while the old volume survives — exactly the standard upgrade path.

#### Workaround (verified 2026-06-10)

```bash
docker compose exec isabelle-gym ./repl/Admin/init   # re-registers into the live volume
# then start the server as usual
```

#### Proposed permanent fix

Run a cheap idempotent check at container start (entrypoint or server startup): if `$ISABELLE_HOME_USER/etc/components` does not list `/app/repl`, run `repl/Admin/init` before launching the gateway. Alternatively, drop the named volume from compose (heaps would rebuild per fresh container) or version the volume name with the image.

Related hardening (2026-07-15): `repl/Admin/init` downloads the `py4j`/`spliff` component
tarballs from the Isabelle component servers, and the official site is sometimes unstable —
a rebuild on a fresh volume can fail transiently. Consider vendoring the two tarballs into the
image (or a local component repository) so builds never depend on the network; until then, the
recovery is simply re-running `docker compose exec isabelle-gym ./repl/Admin/init` once the
site is reachable (the volume caches the download afterward).

---

### Bug 8: `close()` Rejects Its Own `exit` Job — Sessions Never Torn Down — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** Critical (P0 — every session close leaked a `poly` process + in-JVM Isabelle server)
**Introduced:** commit `e6c3869` (2026-07-13, "reject new job and emit running job for ThreadedBackend")
**Status:** ✅ Resolved 2026-07-15 (`_submit_unchecked` bypass + regression tests)

#### Root cause

`e6c3869` added a `_shutting_down` guard to `submit()` so no stale jobs reach a disconnecting
Py4J gateway. But `close()` sets the flag **first** and then calls
`self.submit(self._backend.exit)` — the guard rejected the backend's own exit job (the returned
future was pre-failed with `RuntimeError("Backend … is shutting down")`, caught and only
logged). `ReplBackend.exit()` therefore never reached the JVM: no `Session_Stop`, no
`stop_server` — the session's `poly` ML process and its per-backend in-JVM Isabelle server
leaked on **every** close. Under the MCP-comparison per-problem create/close cycling, container
memory climbed monotonically until the admission gate refused new sessions (503
`PoolExhausted: memory pressure too high`). Full analysis:
internal working notes.

#### Fix

`close()` now delivers the exit job through a private `_submit_unchecked()` that bypasses the
shutdown guard (external `submit()` calls are still rejected, and the queue is still drained
first). Regression tests in `tests/test_threaded_backend.py` — verified to FAIL on the pre-fix
code (2 of 4 tests) and pass on the fixed code:

- `test_close_delivers_exit_to_backend` — the e6c3869 regression guard
- `test_submit_rejected_after_close`
- `test_pending_jobs_cancelled_but_exit_still_runs`
- `test_worker_thread_stops_after_close`

#### Residual (pre-existing, separate)

Even when delivered, `exit()` has historically blocked >60 s (`TimeoutError` at
`EXIT_TIMEOUT` in `logs/server.log`, 2026-06-10) — the Bug 5 "wedged ML process / no OS-level
force-kill fallback" hardening item still applies and is tracked in the Phase-3 plan
(internal working notes).

### Bug 9: Gateway JVM `Event_Timer` Cancelled — Server Wedges, Recovery Blind — RESOLVED

**Files:** gateway JVM lifecycle (`repl/src/python/repl_backend_gateway.py`,
`server/app/services/session_manager*.py`)
**Severity:** High (once hit, every session create/reset 500s until container restart)
**Found:** 2026-08-15, during the `mcp_lsp_server` smoke (Stage 4 work-log entry)
**Status:** ✅ **Resolved 2026-09-08, two independent layers.**

1. **Root cause fixed upstream (Isabelle2026-RC0).** In 2025-2, `Event_Timer.request`
   schedules a bare `TimerTask`; one throwing task kills the JVM-global
   `java.util.Timer` thread, and every later `schedule()` throws
   `IllegalStateException("Timer already cancelled")`. Commit `88acf2619921`
   (isabelle-release, 2026-05-17) wraps every task in try/catch, so the wedge
   class is impossible. Verified on RC0 (`isabellegym-isabelle-gym:2026rc0`,
   port 8001): 12/12 churn rounds + concurrent HOL-Analysis build with
   `gateway_alive=true`, zero `Timer already cancelled` in the server log;
   load degrades to graceful memory-gate 503s instead of 500-forever.
2. **Detection + recovery hardened server-side** (works on 2025-2 too):
   `ReplBackendGateway.alive()` (Scala) schedules a no-op on `Event_Timer` and
   returns false on `IllegalStateException`; `ReplBackendGatewayProcess.is_alive()`
   (Python, 5 s-bounded) probes it; `SessionManager._ensure_gateway` recovers on
   *any* failed probe (dead **or** wedged), and `gateway_alive()` is a functional
   probe with a 5 s cache. Tests: `tests/test_gateway_wedge.py`.

#### Symptom

Mid-smoke, under scratch-session churn + a heap build (twice), every subsequent session
create/reset started failing with HTTP 500:

```
java.lang.IllegalStateException: Timer already cancelled
  at ... Headless$Session.use_theories ← Event_Timer.request
```

`Event_Timer` is JVM-global, so one cancellation wedges the whole gateway. The gateway
liveness check (`gateway_alive` = process liveness only) does NOT detect this state, so
gateway crash recovery never fires; only a container restart unwedges the server.

#### Status / next steps

Root cause not yet isolated. Simple close→create and reset→create cycles were ruled out
(both clean). Suspected: an eviction/teardown interleave cancelling the shared
`java.util.Timer`. Two work directions: (1) find the cancellation source (audit
`Event_Timer` users on the teardown path — session stop / server stop inside
`Server_Utils` / `Session_Manager`); (2) harden detection regardless — the gateway health
check should exercise a real round-trip (e.g. a cheap `use_theories` probe or a JVM-level
"timer alive" check) so this class of wedge triggers crash recovery instead of 500-forever.

### Bug 10: `GET /api/v1/sessions` Leaks `lease_id`s — Destroy Authorization Bypassable — RESOLVED

**Files:** `server/app/services/session_manager_helpers.py` (`list_sessions`),
`server/app/api/v1/router.py`, `server/app/main.py`, `server/app/static/admin.html`,
`mcp_lsp_server/{app,pool,config}.py`
**Severity:** High (any client could destroy any tenant's session)
**Found:** 2026-09-09, handoff `isabellegym-lease-leak-issue.md` (agents in the
Putnam runs hand-rolled DELETEs after reading the listing)
**Status:** ✅ Resolved 2026-09-09.

#### Root cause

The `X-Lease-Id` token is the only ownership proof on mutation paths
(`DELETE`, release), but the pool listing returned `"lease_id"` for **every**
session with no lease required. Three curls — list, steal, delete — killed any
session (JVM + poly torn down), bypassing the lease check entirely. The
absence of a sanctioned destroy path (release frees nothing) is what pushed
agents to improvise it.

#### Fix (four parts)

1. `list_sessions` never includes `lease_id`; the full listing moved behind
   `GET /api/v1/admin/sessions`, gated by `X-Admin-Token` against
   `ISABELLE_ADMIN_TOKEN` (empty = disabled). The admin console gets the token
   injected at serve time; without it, force-close buttons render `locked`.
2. Audit: every DELETE logs a `warning` (session id, lease prefix, request id)
   and increments `isabellegym_sessions_force_closed_total` (alertable).
3. Sanctioned destroy in the LSP MCP: `isabelle_close(file_path, destroy?)` +
   `ISABELLE_MCP_LSP_CLOSE_DESTROYS` — teardown via the binding's own lease,
   rebind-on-404 recovers afterwards (tested: recovery happens in
   `pool.call`'s 404 wrapper; `sync` short-circuits on unchanged text).
4. Tests: `tests/test_lease_security.py` (listing split, admin gate, destroy
   paths, rebind-after-destroy). Rejected as non-solutions, documented:
   task-group scoping without credentials (theater), rotating leases (breaks
   long-lived flows).

#### Verification

Live on the RC0 track: public listing clean with a live session; admin
endpoint 403/403/200 (no/wrong/correct token); bogus-lease DELETE → 403;
owner-lease DELETE → 200 and the session is gone; 8/8 in-container tests.

---

### Bug 11: `ISABELLE_SCALA_JAVA_OPTIONS` Is Dead Config — JVM Options Never Reach the Gateway — RESOLVED

**Files:** `.env` / `.env.example` (false claim), `repl/src/python/repl_backend_gateway.py`,
`repl/Admin/container_entrypoint.sh`
**Severity:** Medium (silent: a documented memory mitigation never took effect; GC behavior
invisible during the 2026-09-10 incident)
**Found:** 2026-09-13, while deploying JVM GC logging (issue 5 rec 1)
**Status:** ✅ Resolved 2026-09-13 (GC logging now wired via the user settings file).

#### Root cause

`.env` carries `ISABELLE_SCALA_JAVA_OPTIONS="-Dpolybank.heap.percent=20
-XX:MaxHeapFreeRatio=30 ..."` with a comment claiming it lets the gateway JVM
shrink its heap. **Nothing consumes this variable** — not the `isabelle scala`
toolchain, not the Scala REPL sources. The gateway JVM always ran with the
scala-launcher defaults (`-Xmx4g`, ZGC), so the heap-shrinking mitigation
never existed. Two other injection channels were verified dead at the same
time: `JAVA_TOOL_OPTIONS` is filtered out by Isabelle's environment handling,
and `ISABELLE_TOOL_JAVA_OPTIONS` set in the process env is **clobbered** by
the settings evaluation (same trap as `ML_OPTIONS`, cf. the heap-cap work).

**The only reliable channel for JVM/ML options is the user settings file**
(`$ISABELLE_HOME_USER/etc/settings`, evaluated last): `ML_OPTIONS` for poly
processes, `ISABELLE_TOOL_JAVA_OPTIONS` for Isabelle-launched JVMs.

#### Consequence discovered at the same time

The gateway JVM runs **ZGC with `-Xmx4g`** (launcher defaults). With two
heavy Analysis-scale sessions in one 4 GB heap, ZGC allocation stalls are the
prime suspect for the 2026-09-10 64-second accept freeze and the 95-minute
4–5× slowdown window (issue 5). Next occurrence will be directly visible in
the GC log instead of guesswork.

#### Fix

- `container_entrypoint.sh` appends
  `ISABELLE_TOOL_JAVA_OPTIONS="$ISABELLE_TOOL_JAVA_OPTIONS -Xlog:gc*:file=/app/logs/isabelle-jvm-gc-%p.log:...:filecount=3,filesize=10M"`
  to the user settings (idempotent), so every Isabelle-launched JVM writes a
  rotated, per-PID GC log. Verified live: `isabelle-jvm-gc-<pid>.log` written
  from gateway start.
- JVM stdout/stderr are also durably captured (`logs/gateway-jvm.log`) —
  previously the stdout pipe was closed after the port line and stderr went
  to the ephemeral server stdout, which is why 06:21 was undiagnosable.
- `.env.example` corrected: the `ISABELLE_SCALA_JAVA_OPTIONS` block is
  labelled known-dead instead of claiming it shrinks the gateway heap.

---

### Bug 12: Unauthenticated ML Execution Through Every Text Endpoint — RESOLVED

**Files:** `server/app/core/input_guards.py` (new), `server/app/api/v1/schemas/API_models.py`,
`server/app/services/heap_pool.py`, `server/app/core/config.py`
**Severity:** Critical (remote code execution in the container by any client)
**Found:** 2026-09-21 code audit (internal working notes, finding SEC-2)
**Status:** ✅ Resolved 2026-09-22.

#### Root cause

`diagnostic_guard.py` protected only `POST /diagnostic`. `POST /sessions/{id}/commands`,
`.../verify_chunk`, `PUT .../document` and the **lease-free** `POST /api/v1/sessions/bigstep`
accepted arbitrary Isar including `ML ‹OS.Process.system "…"›`; bigstep's only filter was a
regex stripping `ML_val` lines (a build-compat hack). The heap pool ran `isabelle build -d
<caller-chosen dir>`, executing whatever theories were there. The MCP tools
`isabelle_run_code` / `isabelle_sync` / `isabelle_multi_attempt` reach the same endpoints.
Additionally `_build_theory_header` quoted import names without escaping, so an import
name containing `"` could break out of the header and inject commands.

#### Fix

- `input_guards.reject_code_execution(text)`: reduce the text to its *command skeleton*
  (nested comments removed, string/cartouche/alt-string bodies blanked) and scan for the
  denylist shared with `diagnostic_guard._DANGEROUS_RE` at token boundaries. Wired as
  Pydantic validators on `CommandRequest.command`, `ChunkVerifyRequest.chunk`,
  `DocumentLoadRequest.text`, `BigStepTheoryRequest.theory` and, since 2026-10-27,
  `FillSorriesRequest.text` → HTTP 422 naming the keyword.
  `HeapPool._scan_for_code_execution` applies it recursively to project `.thy` files before
  `isabelle build`. Policy switch `ISABELLE_ALLOW_ML_COMMANDS` (default false).
- `validate_import_names` on `theories` / `imports` / `dependencies` fields (no quotes,
  whitespace or control characters).
- Tests: `tests/test_input_guards.py` (detection matrix, literal/comment false-positive
  matrix, every model → 422, policy switch, header injection).

Server-side probe `ML_val` inserts (`Backend_Probes`) never pass through the request models
and are unaffected. Legacy documents carrying a leaked probe `ML_val` (REPL-1 in the audit)
will now be rejected by bigstep instead of silently stripped — fix REPL-1 to stop the leak.

### Bug 13: Heap-Pool Path Traversal and Unauthenticated Destructive Heap Endpoints — RESOLVED

**Files:** `server/app/services/heap_pool.py`, `server/app/api/v1/router.py`,
`server/app/api/v1/schemas/API_models.py`, `server/app/core/config.py`
**Severity:** Critical (arbitrary file write/delete as the container user)
**Found:** 2026-09-21 code audit (FINDINGS SEC-3)
**Status:** ✅ Resolved 2026-09-22.

#### Root cause

`task_group` from the request body was used unvalidated as a directory component for the
manifest write (`state_dir / task_group / …` + `mkdir(parents=True)`); `DELETE
/api/v1/heaps/images/{session}?platform=` globbed raw `platform`/`session` into
`shutil.rmtree`/`unlink`; `project` was any absolute path handed to `isabelle build -d`;
none of the "Admin:" heap deletes checked a token.

#### Fix

- `validate_safe_name` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, fullmatch) on `task_group`,
  `session_name`, `heap_session`, image `session`/`platform` — request models and path
  parameters (422). `validate_project_path`: absolute and resolved under
  `ISABELLE_HEAP_ALLOWED_ROOTS` (default `/app:/root/.isabelle`; `HeapPool(allowed_roots=…)`
  for tests). `assert_within` containment on manifest paths and image paths before any
  write/delete; `HeapRejected` (422) error class.
- Owner decision: `DELETE heap`, `DELETE heap image`, `DELETE heap group` require
  `X-Admin-Token` (`router._require_admin_token`, mismatches logged); `POST /heaps/build`
  stays open (the LSP MCP's `isabelle_build_heap` keeps working) but is path-restricted.
- Tests: `tests/test_input_guards.py` (traversal payloads for every input, containment,
  admin gate 403/422, build stays open); `tests/test_heap_pool.py` adjusted to pass
  `allowed_roots=[tmp_path]` and an allowed project path.

#### Deferred (owner decision 2026-09-22)

FINDINGS SEC-1 — `GET /admin` inlines the admin token into an unauthenticated page — is
**not** fixed: a real admin login is future work; keep the port firewalled.

### Bug 14: State Probes Leaked `ML_val` Into the Document — RESOLVED (by design change)

**Severity:** High (audit REPL-1; also the reason `build_verify.py` had to scrub `ML_val` lines)
**Status:** ✅ Resolved 2026-09-30 — the state queries no longer edit the document at all.

**Symptom:** `open_subgoals` / `in_proof` / `local_facts` / `global_facts` / `sledgehammer`
were `ML_val ‹Repl.send_*_tagged …›` commands appended at the document tip, answered
through per-backend `Scala.Fun_Strings` channel queues, then removed with `discard_last_edit`.
`ReplBackend.with_probe_settle` retried the probe on ANY exception (a channel timeout
included) WITHOUT discarding the first insert, and the callers discarded once — so every
timed-out probe left one `ML_val` in the script and the rollback chain permanently. Four of
the five callers also had no `finally`, so a double failure left both.

**Root cause:** the mechanism itself — a read-only question implemented as a document
mutation that must be undone, with the undo bookkeeping spread over three files.

**Fix (design change, DESIGN_CHOICES 1.14):** every state query is now a PIDE **overlay
query**: a `Query_Operation` registered in `REPL.ML` is attached as a temporary overlay to
the document's last command (`Document_Utils.current_state_host`), runs against that
command's result state (the current toplevel state), and its instance-tagged results are
read from the host's `command_results` (`Document_Utils.overlay_query`, the machinery the
LSP-like `sledgehammer_at` already used). Nothing is inserted, so nothing can leak; the wait
is bounded by a per-query budget (`ISABELLE_REPL_*_TIMEOUT`, unchanged names) and the
overlay is always removed. Deleted: `with_probe_settle`, `send_ml_command`, `channel_id`,
`repl_ml_communication.scala` (the channel queues + `Scala_Functions` service), the ML
`send_*_tagged` senders. `probe_transient` (POST /diagnostic) remains the one insertion-based
probe (an arbitrary Isar command cannot run as a print function). Also fixed on the way:
`node_ends_with_end` (the guard every state query runs first) waited for full consolidation,
which blocked every query behind a still-running command for that command's whole runtime.

**Verified (in-container, RC2 image, internal working notes):**
27/27 checks of the live smoke (all probes in proof / after qed / after `end`, verify_chunk's
internal probes, LSP `sledgehammer_at` on the shared operation, `ML_val` absent from the
source after every step, source byte-identical after post-`end` probes); bounded-wait smoke:
a query issued while a 45 s command runs fails at the 20 s budget with a clear error, leaves
no residue, and works again once the command finishes. Unit suite 280 passed / 1 skipped
(`test_mcp_comparison_fixes.py` skipped: needs `openai`, absent from the RC2 image).

---

### Bug 15: Unbounded Settle Waits Wedge the Session Worker — RESOLVED

**Severity:** High (audit REPL-2)
**Status:** ✅ Resolved 2026-09-30 (owner decision: Option A — roll back on timeout).

**Symptom:** `Document_Utils.stable_node_snapshot` looped until EVERY command in the node
was consolidated, with no deadline, and eight helpers used it — including `output_node_results`
behind `POST /commands` (`step`), `rollback`, `probe_transient`, and the purely syntactic
`last_end_offset` / `header_end_offset` / `command_containing_line` / hover / definition. A
looping `by metis` sent through small-step, or a runaway deliberately left in place by a
timed-out `sync_document`, blocked the session's single worker thread for the command's whole
runtime: the Python future timed out, the JVM thread never returned, every later request
queued behind it, and the session was dead until the reaper closed it.

**Fix:**
- `document_utils.scala`: `node_snapshot` (waits only for a stable session, never for
  evaluation) for every syntactic / read-what-is-there query; `settled_node_snapshot(budget_ms)`
  returning `(snapshot, settled)` for the printing helpers (`output_node_results`,
  `output_command_at_offset`). `await_all_processed` deleted.
- `step(isar, wall_budget_ms)` and `probe_transient(isar, wall_budget_ms)` take the request's
  timeout (Py4J protocol change; Python passes `int(timeout*1000)` and keeps its own future
  timeout a grace period ABOVE the budget — `ISABELLE_TIMEOUT_BACKEND_GRACE`, 10 s — so the
  backend's answer always arrives first). On expiry `step` DISCARDS the command it inserted
  (removing the edit is the only cancellation primitive and frees the worker; same rule as
  `verify_chunk`) and returns `success=false` with `Command timed out after N ms (still
  running) — rolled back …`. `rollback` / `vector_step` use `ISABELLE_REPL_SETTLE_TIMEOUT`
  (default 60 s) and report a still-running node instead of waiting.
- Contract change for the small-step API: a command exceeding its timeout no longer keeps
  running in the background — it is rolled back and can be retried with a larger timeout.
  `sync_document` keeps its documented "leave it running" semantics, but nothing blocks
  behind the runaway any more (the LSP reads return what exists; `isabelle_sync` is the barrier).

**Verified (in-container, RC2 image):** `smoke_step_timeout.py` 12/12 — a 40 s spinning ML
command with `timeout: 3` returns at 3.2 s, rolled back, document unchanged, next command
runs in 0.2 s, diagnostic bounded on both paths; the Bug 14 smokes re-run green (27/27 +
bounded-wait); unit suite 281 passed / 1 skipped. Remaining related items: REPL-3 (no
cancellation beyond edit removal), SRV-3 (timeout clock starts at enqueue).

---

### Bug 16: Idle-Cleanup Sweep Blocked the Event Loop — RESOLVED

**Severity:** High (audit SRV-1)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** `cleanup_idle_sessions` (a coroutine, every `ISABELLE_CLEANUP_INTERVAL`) called
four blocking things directly on the event loop: `close_session` per idle/abandoned session
(blocks up to the backend exit/join timeouts, ~60 s each), `_relieve_memory_pressure` (closes
sessions in a loop with `time.sleep` settles), the Py4J liveness probe (up to 5 s when its
5 s cache is stale) and `_ensure_gateway` (spawns a JVM). With a pool's worth of sessions
expiring together the loop froze for minutes and every endpoint — `/healthz` included —
stopped answering. `/readyz` and `GET /` (via `get_lru_info`) also ran the probe inline.
Not affected: `/metrics` — the instrumentator's endpoint is a sync `def`, so FastAPI already
runs it (and the pool collector) in the threadpool.

**Fix:** the sweep body is now `SessionManager.cleanup_once()` (one iteration, testable;
`cleanup_idle_sessions` loops it and logs instead of dying on an exception). Every blocking
step runs via `asyncio.to_thread`; the manager's own `threading.Lock` already makes these
methods thread-safe (every other caller runs in worker threads). `/` awaits `get_lru_info`
and `/readyz` awaits `gateway_alive` through `to_thread`. No client-visible behaviour change.

**Verified:** `tests/test_cleanup_offloop.py` — a ticker task measures loop gaps while three
0.3 s closes run (max gap < 0.15 s; inline they would be ~0.3 s each), a failing close does
not kill the sweep, memory relief and the probe run on non-main threads. Live in the RC2
container with `ISABELLE_IDLE_TIMEOUT=5 ISABELLE_CLEANUP_INTERVAL=5`: two released sessions
swept while `/healthz` was polled every 100 ms — 245 samples, worst latency 4 ms, pool empty
afterwards. Unit suite 284 passed / 1 skipped. Related open item: SRV-2 (gateway recovery and
create hold the global lock for minutes) — now off the loop but still serial under the lock.

---

### Bug 17: LSP MCP Scratch-Slot Leak Hung `multi_attempt` / `run_code` for the Rest of a Run — RESOLVED

**Severity:** High (audit MCP-1)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** `LspPool.acquire_scratch` counts scratch sessions per context key and, at
`SCRATCH_POOL_SIZE`, did `await queue.get()` with no timeout. `isabelle_multi_attempt` and
`isabelle_run_code` returned the slot on the success path and in an `except Exception` — never
in a `finally` — so a `CancelledError` (a `BaseException`: the MCP client disconnecting or
timing out mid-verification, routine with a 180 s attempt budget) skipped both. With the
humanize harness's `SCRATCH_POOL_SIZE=1`, one lost slot meant every later call waited forever,
silently, for the rest of the run.

**Fix (`mcp_servers/lsp/pool.py`, `app.py`, `config.py`):**
- `LspPool.scratch_session(key)` — an async context manager that ALWAYS gives the slot back:
  normal exit / ordinary exception → release (warm); 404 → drop; cancellation or any other
  `BaseException` → drop (a load may still be in flight, so the next caller would get "busy").
  Both tools now use it; their duplicated release/drop code is gone.
- `acquire_scratch` waits with `asyncio.wait_for` (`ISABELLE_MCP_LSP_SCRATCH_WAIT_TIMEOUT`,
  default attempt timeout + 60 s) and raises `scratch pool exhausted …` naming the knobs.
- `drop_scratch`: a session whose load is still running cannot be closed OR released (the
  server answers 409 busy for both), so a background `_retire_later` task retries the close
  every 5 s for the attempt budget + grace, instead of leaving a leased-idle session blocking
  a server pool slot until the abandoned-lease reaper.

**Verified:** `tests/test_mcp_lsp_server.py` (+3): a cancelled holder's slot is dropped and the
next acquire gets a fresh session; 404 drops / other exceptions release; the wait is bounded.
Live (RC2 container, server `ISABELLE_POOL_SIZE=4`, MCP tools driven in-process with
`SCRATCH_POOL_SIZE=1`): a `multi_attempt` cancelled while its candidate was loading, then a
second `multi_attempt` with two candidates completed in 7.2 s (`by (induct xs) auto` proved,
`by simp` failed as expected) and `run_code` succeeded — before the fix the second call hung.
Unit suite 287 passed / 1 skipped. First live attempt against the default 2-slot dev server
showed the residual limit: the cancelled load keeps its server session busy until it finishes,
so a tiny server pool can still answer 503 for a few seconds — that is server capacity, not
the MCP leak (REPL-3: no cancellation primitive).

---

### Bug 18: Diff-to-Edit Offsets Corrupted the Document on Incremental Sync and Checkpoint Restore — RESOLVED

**Severity:** Critical (tracker SYNC-1; found 2026-09-30 while verifying Bug 17)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** the 2nd+ `load_document` on a session (sync path: spliff diff → PIDE replace
edits, Phase B1) left the node text CORRUPTED whenever the diff had an insert next to a
deletion: `by (induct xs) auto` → `by simp` produced `imp  by s` ("Undefined method: s"),
`by (simp)` → `by auto` produced `  byaut o` (lemma fails to parse). Every REUSED scratch
session in the LSP MCP's `multi_attempt` was therefore verifying garbage (candidates judged
wrong for the wrong reason), and LSP `isabelle_sync` re-syncs and checkpoint restores across
multi-hunk diffs were exposed to the same corruption. Fresh sessions (reset path) were fine.

**Root cause:** `Edit_Utils.text_diff_edits` and `Thy_Status.difference_edits` (two copies)
turned spliff's `delInsOpsSorted` into PIDE edits with a cumulative offset shift updated by
every op. PIDE applies an edit list SEQUENTIALLY (each edit against the text as left by the
previous ones), but spliff's ops are a SIMULTANEOUS script in base coordinates in which an
`Insert(b)` may sit INSIDE or at the end of a preceding `Delete` range — meaning "at the start
of that removed range" (`(simp)`→`auto` = `Delete(69,6) Insert(74,…,3) Insert(75,…,1)`). The
cumulative shift placed such inserts one deletion-width too early.

**Fix:** one shared `Edit_Utils.diff_edits(base, target, origin)` maps each op to its position
in the evolving text: `b + delta` past all processed ops; a position inside/at the end of the
last deleted range collapses to that range's start, and successive collapsed inserts chain
after one another. `text_diff_edits` and `difference_edits` both delegate to it.

**Verified:** Scala round-trip property check — 10,003 cases (the two corrupting texts,
"hello world"→"hallo welt", 5,000 random string pairs with and without an origin offset),
applying the produced edits sequentially reproduces the target exactly (the old code failed
3 of the fixed cases). Live: `repro_sync_diff_corruption.py` — five successive loads on one
session, all `success=True`, source identical to the loaded text every time;
`smoke_checkpoint_restore.py` — restore across a 3-lemma multi-hunk diff gives back the
checkpoint-time source byte-for-byte, back-and-forth between two checkpoints, and the session
keeps working. Bug 14/15 smokes re-run green on the new jar.

---

### Bug 19: A Bogus File Path Pinned a Leased Session in the LSP MCP — RESOLVED

**Severity:** High (audit MCP-2)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** every file-scoped LSP tool runs `get_binding` then `sync`. `_create_binding`
acquired a leased server session and registered the binding for ANY path; only `sync`, one
step later, checked `os.path.isfile` and raised. A typo or a not-yet-written file therefore
cost one leased session, parked under the bogus path until `isabelle_close` or the
abandoned-lease reaper — a few typos exhausted a small pool. The old test
`test_binding_missing_file_falls_back_to_no_theories` enshrined the order.

**Fix (`mcp_servers/lsp/pool.py`):** `_create_binding` checks the path first and raises the
same `FileNotFoundError` before any acquire; nothing is registered. The rebind-on-404 paths go
through the same function, so a file deleted mid-session now yields a clean error instead of a
fresh orphan. Test rewritten as `test_binding_missing_file_acquires_nothing` (no acquire, no
binding; the file appearing later binds normally).

**Verified:** LSP module 18/18, suite 287 passed / 1 skipped; live against the dev server:
`isabelle_open` / `isabelle_goal` / `isabelle_sync` on a nonexistent path all raise, the
server session list and leased count are unchanged, no binding exists, and a real file still
opens with `success=true`.

---

### Bug 20: Concurrent First Calls on One File Created Two Sessions and Released One Mid-Use — RESOLVED

**Severity:** High (audit MCP-3)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** `LspPool.get_binding` read `_bindings` with an unlocked `dict.get`; when two
tool calls on the same not-yet-bound file ran concurrently (a harness firing `isabelle_goal`
and `isabelle_diagnostic_messages` together right after writing the file), both missed, both
acquired a session, and the second `_create_binding` popped the first binding and RELEASED
its session while the first caller was still using it.

**Fix (`mcp_servers/lsp/pool.py`):** the lookup runs under `_bindings_lock`; a missing
binding is created SINGLE-FLIGHT per path — the first caller owns an `asyncio.Future` in
`_creating`, concurrent callers `await` it (shielded) and get the same binding or the same
error; the entry is removed in `finally`, so a failed creation is never cached and a later
call retries cleanly. The rebind-on-404 paths are unchanged.

**Verified:** `tests/test_mcp_lsp_server.py` (+2): three concurrent first calls with a slow
fake acquire → one acquire, one shared binding object, nothing released; a failing acquire
is seen by both waiters and the next call succeeds with `_creating` empty. Live: three tools
fired concurrently on a fresh file → leased count +1, one binding, no errors. Suite 289 passed
/ 1 skipped.

---

### Bug 21: LSP File Bindings Reused Dirty Sessions — RESOLVED

**Severity:** Medium (audit MCP-4)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** `LspPool._create_binding` acquired with the client default `reuse_dirty=True`,
while the chunk-centric pool has used `reuse_dirty=False` since the proof-leak incident
(DESIGN_CHOICES 1.5 / 2.2). A released binding's session still holds the previous file's
document (and `load_document` records command history, so the server counts it dirty); the
next file with the same dependency key inherited it — another attempt's proof visible between
acquire and the first sync, and that first sync then paid a full backend reset anyway, no
cheaper than a fresh session.

**Fix (`mcp_servers/lsp/pool.py`):** `acquire_session(..., reuse_dirty=False)`. Test fake now
honours the server rule (a session that loaded a document is dirty); `test_auto_open_defaults`
asserts the flag; `test_binding_warm_reuse_after_release` became `test_binding_reuse_is_clean_only`
(dirty released session skipped, clean released session reused warm).

**Verified:** LSP module 20/20, suite 289 passed / 1 skipped; live: open+sync A, close A,
open B → different session, A's old session listed idle/unleased with `commands_executed=1`,
B's source contains only B.

---

### Bug 22: Checkpoint Restore Reported Success for Unknown or Invalidated Ids — RESOLVED

**Severity:** High (audit REPL-4)
**Status:** ✅ Resolved 2026-09-30 (bookkeeping fix; no design-choice entry).

**Symptom:** `StateIDManager.valid` accepted `state_id <= count`, i.e. the next, never-issued
id; `Thy_Info.restore_state` returned an empty edit list for an id it did not hold (never
saved there, or wiped by `reset_to_fresh_base` after an incremental document replace), so
`Repl_Session.restore_state` applied nothing and returned true; and Python's
`restore_checkpoint` discarded the backend's boolean and returned true unconditionally. A
client could be told "State restored successfully" while the document was untouched.

**Fix:** Scala — `valid` is `< count`; `Thy_Info.restore_state` returns `Option` (None =
unknown) plus `has_saved_state`; `Repl_Session.restore_state` is all-or-nothing (false, no
edits, unless every entered theory holds the id). Python — `restore_state` returns the
boolean; `restore_checkpoint` drops a rejected id and returns `SessionExecutionError('unknown
to the backend (invalidated by a document replace or reset); nothing restored')`; the route
puts that reason in `message` instead of the generic string. Rollback / `verify_chunk` discards
were already sound (parent chain; restore works in both directions), so no chunk-path
invalidation was added.

**Verified:** `tests/test_checkpoint_restore.py` (3: known id restores; unknown id refused
before the backend; backend rejection → error, id dropped, no second backend call). Live:
never-issued id refused; valid restore byte-exact; checkpoints before an incremental replace
refused; earlier back-and-forth checkpoint smoke green. Suite 292 passed / 1 skipped;
`session.py` trimmed back to 897 lines (ratchet 899).

---

### Bug 23: Theory-Name Injection Through `enter_theory` and the Theory-Name Fields — RESOLVED

**Severity:** High (security; found by the TEST-1 security-input suite)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** `POST /sessions/{id}/enter_theory/{theory_name}` spliced the path parameter
unvalidated into the generated `theory <name> imports … begin` header
(`session.py::_build_theory_header`), and `DocumentLoadRequest.thy_name` /
`BigStepTheoryRequest.theory_name` had no validator either. A name such as
`T" imports Main begin ML ‹…› end` closed the quote and injected further Isar past the
code-execution guard (Bug 12 covered the text bodies and import lists, not these names).

**Fix:** the route validates the segment with `validate_import_name` (422); the two models
gained `_opt_theory_name` validators (same character policy as import names — quotes,
whitespace, `;` refused; path-like names stay legal by design).

**Verified:** `tests/test_security_inputs.py` — 3 payloads on each of the three entry points
answer 422, never reaching the (stub) session.

---

### Bug 24: Thirteen LSP MCP Tools Shipped With an Empty Description — RESOLVED

**Severity:** Medium (agent-facing; DESIGN_CHOICES 2.8 says the docstrings ARE the prompt)
**Status:** ✅ Resolved 2026-09-30.

**Symptom:** every file-scoped LSP tool wrote its docstring as `"""…""" + _SYNC_NOTE`. A
concatenation is an expression, not a docstring: `__doc__` was None and `list_tools()`
advertised those 13 tools with no description at all — agents saw bare names for
`isabelle_goal`, `isabelle_sledgehammer`, `isabelle_multi_attempt`, …

**Fix (`mcp_servers/lsp/app.py`):** real docstrings plus a `synced_tool()` decorator that
appends the sync note to `__doc__` before registering with FastMCP.

**Verified:** `tests/test_mcp_tools_smoke.py` asserts a non-empty description and an object
input schema for every tool of both servers, the exact tool-name sets (11 / 23), the
`prove_theorem` prompt, and the `success ≠ proved` wording in `verify_chunk`. (The shim
re-export assertion was dropped with the shim packages on 2026-10-05.)

---

### Bug 25: `enter_theory` Accepted Imports Outside the Session's Context — RESOLVED

**Severity:** High (silent wrong verdicts; found by the training-metrics review of afd3de9)
**Status:** ✅ Resolved 2026-10-22 (owner decision D7, training-metrics plan v1.5; commits 83baa0a and its fix).

**Symptom:** a session's theory context is fixed when it is acquired: a node's context
comes from the session's wrapper theory, which imports the acquisition-time theories, and
`Edit_Utils.get_thy_header_edits` applies the node's own header first and then the
wrapper's, which replaces it. The imports given to `enter_theory` therefore only gate
loading. A request for imports the session did not hold answered 200, and the theory was
then checked without them. Observed: the training-metrics replay on `Main` sessions (review
afd3de9; first error `Undefined type name: "real"`), and on the first version of this fix a
`Main` session entered with `HOL.Complex_Main`, and a session acquired as `HOL.Main` entered
with `HOL.Real` or `Complex_Main` (review 83baa0a): `typ real` failed in each.
The July HOL-Analysis small-step run (`evaluation/results/benchmark_runs.json`, 8 workers)
is no evidence for this bug: it recorded 2 successes and 91 failures in 93 files, and 81 of
the failures were HTTP 500s at session acquisition, 2 at `enter_theory`.

**Fix:** `enter_theory` asks the backend which requested imports lie outside the session's
context and refuses a non-empty answer with **409** `{"error": "imports_not_covered",
"uncovered": [...], "session_theories": [...]}` (acquire a session with those imports).
`Repl_Session.uncovered_imports` computes the context as the wrapper theory's ancestry: it
walks the document nodes' resolved imports (theories loaded at acquisition outside the heap)
down to the heap base's import graph (`current_background.base.loaded_theories`,
`all_preds`). Every name is resolved as Isabelle resolves an import
(`Resources.import_name`), and a session-qualified global theory is identified with its
global name (`HOL.Complex_Main` is `Complex_Main`). A requested name is judged when it
resolves into the heap graph or is qualified by a known session (`HOL-Library.Infinite_Set`
on a HOL heap); an unqualified unknown name (a project theory) is not judged, and nothing is
judged when the ancestry cannot be completed: the backend then says why (`?unjudged: ...`) and
the server logs a warning "import coverage not judged" with the session's theories, so a pass
through the old behaviour can be found in the log (review 5a4ff3a S1). Python side: `services/import_coverage.py`
(one backend call, 30 s), `ImportsNotCovered` in `errors.py`, the handler in `main.py`. The
stepwise MCP and the file MCP acquire sessions with the imports they then use, so they are
not affected.

**Related (review 83baa0a S1):** a header Isabelle rejects (an import it cannot load, a
malformed name) used to answer 500; it now answers **422** `{"error": "header_failed",
"message": ...}`. A timeout or a memory failure while loading keeps its old handling.

**Not covered:** `load_document` takes header imports the same way; it is not checked yet
(the file MCP acquires with the file's header imports, so only a direct API client can
meet it).

**Verified:** `tests/test_import_coverage.py` (7: the service call, the 409 body, covered and
empty imports, the header-failure mapping and its 422, the logged unjudged answer);
`tests/test_import_coverage_live.py`, opt-in with `ISABELLE_LIVE_TESTS=1`, holds the live
matrix below as the regression test for the Scala side (review 5a4ff3a S2). Live on the container's HOL heap, 19
cases over six sessions (`Main`, `Complex_Main`, `HOL.Main`, `HOL.Real`, `HOL.Transcendental`,
`HOL-Library.Infinite_Set`): every uncovered spelling 409, every covered one 200, `Main` +
`Some_Project_Theory` 422.

---

### Bug 26: A Session Sorted Its Imports, So a Short Name Could Mean Another Constant — RESOLVED

**Severity:** High (correct theories fail; found by the training-metrics census on the server, 2026-10-25)
**Status:** ✅ Resolved 2026-10-25 (owner decision D15, training-metrics).

**Symptom:** the census replay of `HOL-Analysis.Cartesian_Space` proved 116 of 144 blocks. The
first failures read `Type unification failed: Clash of types "_ ⇒ _" and "(_, _) vec"` with the
operand `Transposition.transpose`: `transpose` meant the swap function of
`HOL-Combinatorics.Transposition`, not the matrix transpose of `Finite_Cartesian_Product`. The
later blocks failed on facts of the failed ones.

**Cause:** `SessionManagerHelpersMixin._normalize_theories` returned `sorted(set(theories))`,
and the wrapper theory imports the session's theories in that order. The wrapper is the
node's only visible parent (Bug 25), and in Isabelle the later import wins a short name that
two parents declare. Cartesian_Space imports Transposition first; sorted, it came last.
`routes/sessions.py` sorted the heap-session theories the same way. 44 of the 94
HOL-Analysis theories have imports that sorting reorders.

**Fix:** both places drop duplicates and keep the caller's order. The dependency key and the
wrapper name follow the order, so the same imports in another order get another session.
`isabelle build` (big-step) is not affected: it checks the text's own header, and its sorted
list is only a cache key. The training-metrics corpus splitter (`theory_splitter.extract_imports`)
keeps the header order too.

**Verified:** `tests/test_import_order.py` (5). Live on the container's HOL heap
(claude-work/2026-10-25-import-order): theories `A` and `B` both declare `foo`; with imports
`[B, A]` the old server read `foo` as `B.foo`, the fixed one as `A.foo` (with `[A, B]` both read
`B.foo`); a plain `isabelle build` of the same theories agrees with the fixed server.

---

### Bug 27: A Bare Import of a Sibling Theory Did Not Resolve Like `isabelle build` — RESOLVED

**Severity:** High (files of a multi-theory session cannot be opened; found on the PGR server, 2026-11-12)
**Status:** ✅ Resolved 2026-11-12.

**Symptom:** a session's ROOT lists theories `A` and `B`, `B` has `imports A`. `isabelle build`
passes; `isabelle_open` on `B.thy` fails with `http_500` / "Cannot load theory file": the server
looks for `A` in `server/repl/thys/`.

**Cause:** two places resolved the bare name against the server's own directory. (1) The MCP sent
only the import names to `acquire`, so the wrapper theory (in `server/repl/thys/`) imported `A`
there. (2) The document's node was `Draft.B`, a name with no directory: `import_all_theories`
called `use_theories` with the default master directory, and PIDE resolves a theory command's
imports against the node's directory (`Command.blobs_info`), so it reported `Bad theory import`.

**Fix:** the LSP MCP names a same-directory sibling by its absolute path in the wrapper's imports
(`resolve_sibling_imports`) and sends `master_dir` (the file's directory) with `load_document` and
`fill_sorries` (optional field, validated by `validate_master_dir`). The server passes it to
`Backend.enter_thy_in`; `Document_Utils.thy_node_name` then names the node `master_dir/Thy.thy`,
and `import_all_theories` / `dependencies_edit` use it. The siblings' own imports are also read
for the session image (field), so `BVSmt_SmtSize`'s `BVSmt_Smt.…` import selects the
`BVSmt_Smt` image. Qualified imports and the field rule are unchanged. Limit: the MCP and the
server must see the same disk.

**Verified:** `tests/test_mcp_lsp_master_dir.py`, `tests/test_master_dir_server.py`; live
`tests/test_master_dir_live.py`; `BVSmt_SmtBound.thy` and `QFUF_Proof.thy` on PGR.

**Related, not fixed:** session start builds a missing or outdated image inside the request
(`ISABELLE_SESSION_START_BUILD`, default true), which can run beside a user's own `isabelle build`
on the same HOME and corrupt the build log database. Setting it to `false` makes the start only check.

---

### [To-do]Issue 1: How Isabelle do parallel
When have parallel "have x" statements, can we do this in step. And how do we retrive information when one line is stucked in loop. That is, we need error retrieval for a proof chunk, the server should not just return a timeout error, it should tell, when we build the MCP server, the agent what part of that proof chunk just went wrong.


---

## Open Findings Tracker

Working list for the 2026-09-21 audit findings that are still open (source and
full write-ups in the internal working notes, audit section 2).
Rules: a row is updated **before** work on it starts; **fixed** needs a commit;
**verified** needs a regression test that passes in-container; a verified
finding is then promoted to a numbered "Bug N" section above and a dated Work
Log row. Commit subjects use `fix(<ID>): ...` so `git log --grep=<ID>` finds them.
Status vocabulary: `open` · `in progress` · `fixed` · `verified` · `deferred`.

| ID | Sev | Title | Where | Status | Fix commit | Test | Notes folder |
|---|---|---|---|---|---|---|---|
| REPL-1 | High | Probe double-insert: `with_probe_settle` retries on any exception without discarding the first `ML_val` edit | `server/repl/src/main/scala/repl/repl_backend.scala:57-64` | fixed + verified 2026-09-30 by design change (state queries are PIDE overlays; `with_probe_settle`/channels deleted); see Bug 14 | 27ea15c02025b4d3690327cdef5210425e4d4940 | internal notes, `smoke_overlay_timeout.py` (live-server) | internal notes |
| REPL-2 | High | Unbounded settle loop: `stable_node_snapshot` spins with no deadline; a timed-out command left by `sync_document` wedges the worker | `server/repl/src/main/scala/repl/document_utils.scala:56-77` | fixed + verified 2026-09-30 — every wait is wall-bounded (`settled_node_snapshot`), read-only queries never wait, `step`/`diagnostic` take the request timeout and roll back on expiry (owner: Option A); see Bug 15 | 1d9f026f0e9d1436f6d03b61040e9b7fdf8c3dd1 | internal notes (+ the two overlay smokes re-run) | internal notes (Part 2) |
| SRV-1 | High | Cleanup coroutine calls sync `close_session` and `time.sleep` on the event loop | `server/app/services/session_manager_helpers.py:186,252` | fixed + verified 2026-09-30 — sweep refactored to `cleanup_once()` with every blocking step in `asyncio.to_thread`; `/` and `/readyz` probe off the loop; see Bug 16 | c4ade5100b1a9e571f6b2dc40ff3f4105ddcbec7 | `tests/test_cleanup_offloop.py` (3), internal notes (live) | internal notes (Part 3) |
| MCP-1 | High | LSP scratch-slot leak → permanent hang: release outside `finally`, `await queue.get()` without timeout | `mcp_servers/lsp/app.py`, `mcp_servers/lsp/pool.py:254` | fixed + verified 2026-09-30 — `LspPool.scratch_session` bracket (always returns the slot, drops on 404/cancellation, deferred close of busy sessions) + bounded `acquire_scratch` wait; see Bug 17 | c3b1326fb4751b68aa1d74fa12737352bb0e1006 | `tests/test_mcp_lsp_server.py` (+3), internal notes (live) | internal notes (Part 4) |
| MCP-2 | High | Binding registered before the `isfile` check; a bogus path pins a leased session | `mcp_servers/lsp/pool.py:125,149` | fixed + verified 2026-09-30 — `_create_binding` checks `isfile` before acquiring; see Bug 19 | 4e5128d0a4d3b0667cc9154f71c08a5a398067d6 | `tests/test_mcp_lsp_server.py::test_binding_missing_file_acquires_nothing`, internal notes (live) | internal notes (Part 6) |
| MCP-3 | High | `get_binding` reads `_bindings` outside the lock; two first calls → two sessions | `mcp_servers/lsp/pool.py:136` | fixed + verified 2026-09-30 — `get_binding` looks up under the lock and is single-flight per path (`_creating` futures); see Bug 20 | f83d9b5682d938f9ac7d05c07d7bf6b5f9630d1e | `tests/test_mcp_lsp_server.py` (+2), internal notes (live) | internal notes (Part 7) |
| REPL-4 | High | Checkpoint soundness: `valid` accepts `state_id == count`; unknown id restores empty edits and reports success | `server/repl/src/main/scala/repl/repl_session.scala:45`, `thy_info.scala:92` | fixed + verified 2026-09-30 — issued-ids-only validation, all-or-nothing restore, Python honours the backend result; see Bug 22 | 56b85f1 | `tests/test_checkpoint_restore.py` (3), internal notes + `smoke_checkpoint_restore.py` (live) | internal notes (Part 9) |
| MCP-4 | Med | LSP pool acquires with default `reuse_dirty=True` (stepwise uses `False`); stale errors leak across attempts (reproduced 2026-09-30 during the RC2 smoke test) | `mcp_servers/lsp/pool.py` | fixed + verified 2026-09-30 — file bindings acquire with `reuse_dirty=False` (clean-only reuse, as the stepwise pool); see Bug 21 | d4a54ae630632fa8522ef48743a260b2cf6e30a0 | `tests/test_mcp_lsp_server.py::test_binding_reuse_is_clean_only`, internal notes (live) | internal notes (Part 8) |
| TEST-1 | P0 | Missing regression suites: in-process MCP `list_tools()` smoke; security-inputs (`../`, `ML <...>`, quote injection) — `tests/test_input_guards.py` covers part | `tests/` | fixed + verified 2026-09-30 — `test_mcp_tools_smoke.py` (6) + `test_security_inputs.py` (78, HTTP-layer payload matrix); both found real gaps, fixed as Bug 23 (theory-name injection) and Bug 24 (13 LSP tools with empty descriptions); suite runs with no ignore flag (openai skip) | afd0ecc (tests), f6c9a36 (Bugs 23/24) | the two new modules; suite 376 passed / 2 skipped | internal notes (Part 10) |
| SEC-1 | Crit | `/admin` inlines the admin token into an unauthenticated page | `server/app/main.py` | deferred (owner, 2026-09-22; keep the port firewalled) | | | |
| RC2-1 | — | Isabelle2026-RC2 image: in-container unit suite, route smoke, MCP stdio smoke not yet run on the new image | `deploy/Dockerfile` | open — PARTIAL 2026-09-30: unit suite runs green on the RC2 image with no flags (376/2 skipped) and the MCP servers are smoke-tested in-process (TEST-1); still not done: a route smoke on the image itself and spawning each MCP over stdio | | | internal notes |
| RC2-2 | — | Retire `deploy/Dockerfile.rc0`, `build_rc0_image.sh`; make `Dockerfile.export` a heap-baking stage; rewrite `RC0-image-instructions.md` for the 2026 image | `deploy/` | open (after RC2-1; RC0 image itself is kept until the Isabelle2026 release) | | | |
| ENV-1 | Low | `ISABELLE_REPL_*_TIMEOUT` (now overlay budgets) set on the `python -m server.app.main` command line did not reach the gateway JVM in the RC2 dev container (budget stayed at the 20 s default); Python's own `Timeouts` read the same names, so the two sides can disagree. Check how `repl_backend_gateway.py` spawns `isabelle scala` (env inheritance / Isabelle settings scrubbing) | `server/repl/src/python/repl_backend_gateway.py:120` | open (noted 2026-09-30; owner: not important now) | | | internal notes |
| SYNC-1 | **Crit** | Incremental sync CORRUPTS the document: the 2nd+ `load_document` on a session (sync path: spliff diff → replace edits) lands edits at wrong offsets — `by (induct xs) auto`→`by simp` yields `imp  by s`, `by (simp)`→`by auto` yields `  byaut o` (found 2026-09-30 while verifying MCP-1: every REUSED scratch session in `multi_attempt` verifies garbage; LSP `isabelle_sync` re-syncs affected too). Fresh sessions load the same texts fine. Suspect `Edit_Utils.text_diff_edits` offset bookkeeping across multiple hunks | `server/repl/src/main/scala/repl/edit_utils.scala` (`text_diff_edits`), `repl_session.scala` (`replace_document`) | fixed + verified 2026-09-30 — `Edit_Utils.diff_edits` maps spliff ops to sequential PIDE edits correctly (inserts inside a deleted range collapse to its start); shared by `text_diff_edits` and `Thy_Status.difference_edits`; see Bug 18 | 76bb9db00fa48e11bcd02df318729596647f529f | Scala round-trip property check (10,003 cases), `repro_sync_diff_corruption.py`, `smoke_checkpoint_restore.py` (live) | internal notes (Part 5) |

Closed since the audit (for reference): SEC-2, SEC-3 (Bug 12, Bug 13, 2026-09-22);
DOC-1, DEP-1 (MCP package merge + `mcp<2` pin installed in the image, 2026-09-27/29);
REPL-10 (stale `repl/python/` deleted, 2026-09-27). Medium/low findings (SRV-5…12,
REPL-5…9, MCP-5…7) stay in FINDINGS.md until promoted here.

---

## Summary of Changes

| File | Change | Status |
|---|---|---|
| `server/app/services/threaded_backend.py` | Queue `exit()` + configurable `EXIT_TIMEOUT`/`JOIN_TIMEOUT` | ✅ Done (Bug 1, Bug 2) |
| `server/app/services/session_manager.py` | `cleanup_idle_sessions()`: add `max_lease_age` force-eviction path | ✅ Done (Bug 3) |
| `server/app/services/session_manager.py` | `close_session()`: re-check `in_use` after `pop()` | ✅ Done (Bug 4) |
| (none — verification only) | Isabelle server/session OS processes confirmed to terminate on close (incl. mid-sledgehammer) | ✅ Verified not-reproducible (Bug 5); optional force-kill fallback remains |
| `server/app/core/config.py`, `session_manager.py`, `router.py` | Sledgehammer concurrency semaphore + gateway auto-restart | ✅ Done (Bug 6) |
| `server/app/services/memory_monitor.py` | `/proc/meminfo` fallback (native hosts read 0 → gate inert), tiers, RSS attribution, external pressure | ✅ Done (G7, G2, G8 — 2026-10-17) |
| `server/app/services/memory_policy.py`, `build_verify.py`, `routes/heaps.py` | Builds gated by memory with their own knob; budget admission with reservations; `discard_dead` | ✅ Done (G3, G1 — 2026-10-17) |
| `mcp_servers/common/{registry,memory_advice,guard}.py`, both pools/apps | Registration list, structured advice, reminders, release tools, scratch TTL | ✅ Done (G4, G5 — 2026-10-17) |

---

## Agent Work Log (dated)

Test/demonstration artifacts live in the internal working notes (one folder per task, each
with a `NOTES.md`). **Note:** those notes are gitignored (local only) and were pruned locally in
2026-09 — several pre-2026-09 entries below reference paths that no longer exist on
disk; the entries are kept as the historical record. Summary of work completed:

| Date | Task | What was done | Artifacts |
|---|---|---|---|
| pre-2026-06 (legacy) | **Early development — previously unlogged** | Foundational work that predates this log; one-liners from each folder's NOTES.md. `research-mcp-interaction`: MCP interaction design research (Lean/Isabelle SOTA → token-efficient design). `research-parallel-proof`: Isabelle parallel proof checking → parallel `have` chunks + granular error retrieval. `impl-verify-chunk`: `verify_chunk` — single-timeout, per-command status for a proof chunk. `transactional-verify-chunk`: auto-rollback on failure/timeout. `impl-parallel-sessions`: per-session parallelism + OOM testing. `impl-diagnostic-endpoint`: diagnostic-commands endpoint. `impl-mcp-server`: the (stepwise) MCP server over IsabelleGym. `modularize-server`: split server files to ~500-line modules. `compare-mcps`: early cross-MCP comparison planning (COMPARISON_PLAN.md). `rearb`: arbitration spike scratch (ROOT + numbertheory problem). Scratch files `2026-1223.pdf`, `build_devnote_section.py`, `repro_sledgehammer_500.py`, `trash-imo-rep6-9/` are unarchived scratch, not logged work. | internal notes |
| 2026-06-03 | **Shutdown fix** | Two bugs: (1) `SessionManager.shutdown()` let `asyncio.CancelledError` escape (it's a `BaseException`, not caught by `except Exception`) → uvicorn "Application shutdown failed" + teardown skipped; (2) `ReplBackendGatewayProcess.terminate()` signalled only the launcher shell, leaving the JVM orphaned (4 GB-heap leak → container OOM `Exited 137`). Fixed: catch `CancelledError`; `killpg` the gateway process group. | internal notes |
| 2026-06-03 | **Memory-mgmt investigation** | Found the Scala memory management was dead three ways: never called by the server, wrong layer (per-backend, 1 session each), wrong metric (JVM heap, not the `poly` processes). | internal notes |
| 2026-06-03 | **Memory mgmt → Python** | Moved memory management into the Python `SessionManager`, measuring real container memory via cgroup v2 (`MemoryMonitor`). Under pressure: evict idle LRU sessions, then 503 (never kill busy/leased). Removed the dead Scala code (rebuilt `repl.jar`) and synced the Py4J layer. Also fixed `PoolExhausted` being mis-mapped to 500. | internal notes |
| 2026-06-04 | **Sledgehammer concurrency at scale** | Built an N-way isolation + throughput-scaling harness. Result: isolation PASS at N=6 (no crossed channels); throughput knee ≈ 4 on 32 cores; **surfaced Bug 6** (gateway OOM at W=16). | internal notes |
| 2026-06-04 | **Bug 6 fix** | Sledgehammer concurrency semaphore + gateway health-check/auto-restart (see Bug 6 above). Verified: W=16 no longer OOMs; killed gateway auto-recovers on next request. | internal notes |
| 2026-06-04 | **Bug 5 re-check** | Measured poly/prover process counts across create→close and close-during-sledgehammer. Both reap cleanly (≤2 s), so Bug 5 is not reproducible; marked resolved. Optional force-kill fallback for wedged ML processes noted. | internal notes |
| 2026-06-04 | **Phase 0 monitoring** | Added a Prometheus `/metrics` endpoint (HTTP histograms via instrumentator + `isabellegym_*` counters/gauges/histogram in `server/app/core/metrics.py`, fed by `get_lru_info()`/`MemoryMonitor`/gateway-recovery), `/healthz`+`/readyz` probes, and a Prometheus+Grafana+cAdvisor stack in `docker-compose.yml` with `mem_limit: 12g` and a starter dashboard. Verified end-to-end: all 3 Prometheus targets UP, Grafana dashboard provisioned, domain counters move, `memory_limit_mb` reflects the 12g cgroup. NB: image must be rebuilt (`docker compose build isabelle-gym`) to bake in the two new pip deps. | internal notes, `monitoring/` |
| 2026-07-15 | **MCP-comparison harness fixes** | Fixed the harness bugs found by the MCP-comparison audit (internal notes). Headline: the DeepSeek "empty response" runs were `max_tokens: 4096` truncation of a reasoning model (output_tokens == 4096 exactly; hidden `reasoning_content` ate the budget), NOT a content filter — raised to 32768, `RoundResult` now carries `finish_reason`/`reasoning_text`, truncated rounds are counted and honestly labelled, and a no-tool-call round gets up to 2 nudges instead of silently ending the attempt. Also: removed the first-green-chunk early termination in the IsabelleGym runner (it cut attempts at the first proved *helper* lemma — the devnote "fooled arbiter" rows); unparsable tool JSON no longer poisons the message history; `call_tool` stops swallowing `BaseException`; `isabelle_launch` gets a derived session name instead of a theory; I/Q auth token no longer routed through the model; `analyze.py` fixed (crash on zero-solved, wrong default dir, per-experiment subfolders) + error-class table. Tests: `tests/test_mcp_comparison_fixes.py`; full suite 20 passed. Verified offline only (no live model runs, per request). | internal notes |
| 2026-07-15 | **P1/P2 audit fixes (Phases 2–3)** | Fixed the P1/P2 findings of the server code audit (internal notes): A2 `_create_session` no longer closes a session while holding the manager lock; A3 base `SessionError` handler (500s now carry the real message, e.g. backend timeouts); A4 sledgehammer marks the session busy + refreshes activity; A5 client HTTP timeouts get grace beyond the server budget (`execute_command`/`diagnostic` +30 s, sledgehammer +120 s for semaphore queueing); B1 MCP `_begin_theory` closes the leased session on `enter_theory` failure; A6 memory gate subtracts `inactive_file` page cache, settles 2 s between evictions, and retries admission 3×2 s before 503; A7 a concurrently-closed session surfaces as 404 not 500; A8 empty `verify_chunk` chunks are 422 and the backend `error` (e.g. "theory not begun") is passed through; B2 MCP connection state keyed weakly by the session object (no id-recycling, auto-cleanup); B3 `close_theory` docstring says destroy, not release. Tests: `tests/test_phase2_phase3_fixes.py` (8) + existing (4), all green; in-container probes in internal notes. Also hardened the Dockerfile (Isabelle download layer before COPY, wget retries + Clarkson mirror fallback) after two silently-failed image builds traced to the unstable TUM dist server. | internal notes |
| 2026-07-15 | **Bug 8 fix (session teardown regression)** | Research into "deleted session doesn't release memory" (MCP-comparison failures) found `ThreadedBackend.close()` rejecting its own `exit` job since `e6c3869` — no session was ever torn down on the JVM side. Fixed via `_submit_unchecked()` bypass; added `tests/test_threaded_backend.py` (verified red on pre-fix code). Also produced full audits of the server/MCP layers and the comparison harness (incl. the DeepSeek "empty response" root cause: `max_tokens=4096` reasoning-truncation, not a content filter). | internal notes |
| 2026-06-10 | **Image rebuild + Bug 7** | Rebuilt the image with trimmed deps (removed `torch` — sole source of the multi-GB `nvidia-*-cu12` CUDA wheels, only imported by archival `previous works/` code — and unused `expecttest`, from `requirement.txt` + `pyproject.toml`; image 24.9 GB → ~2 GB). First server start after the rebuild failed with `Not found: py4j` in the gateway — surfaced **Bug 7** (pre-existing `isabelle_user_data` volume shadows the build-time `repl/Admin/init` registration). Worked around by re-running `./repl/Admin/init` in the container. | — |
| 2026-08-08 | **Server prep for dual MCP support + legacy cleanup** | Prepared the server for the planned LSP-like read-only MCP (research: internal notes). New server surface: `GET .../facts/local` + `GET .../facts/global?limit=` (transient read-only probes; the Scala/ML chain already existed but had zero callers), `PUT .../document` (whole-document replace: backend `reset()` + re-enter theory + one edit — the file-sync primitive), `GET .../last_report` (retained report of the most recent `verify_chunk`; cleared by `load_document`, not by rollback/restore), and an optional observability `label` on session create echoed in session info/listings. Client methods in `async_client.py` for all four. Legacy cleanup: **Bug 7 permanent fix** (`repl/Admin/container_init.sh` entrypoint re-runs idempotent `./repl/Admin/init` on every container start); fixed `REPL.ML` `pretty_local_facts` returning only the first fact (`List.hd`); removed the dead `run_diagnostic` Protocol alias (Scala side renamed to `probe_transient` in `e6c3869`); dropped never-populated bigstep `diagnostics`/`failure_location` fields from API + internal models; deleted dead `server/app/api/v1/ws.py` and `repl/src/python/session_manager.py`; removed stale `--cov=gym` pytest addopts and `gym*` packaging refs from `pyproject.toml`. Tests: `tests/test_readonly_mode_server_prep.py` (8 new, all green in-container; the two `render_chunk` + `test_mcp_comparison_fixes.py` failures are pre-existing missing `mcp`/`openai` packages in the image, unrelated). **Known limitation surfaced during smoke-testing:** channel probes (`facts/local`, `facts/global`, `open_subgoals`, sledgehammer, `diagnostic`) time out when the document tip is past a trailing `end` — a probe command appended after `end` never executes, so the ML channel never answers (pre-existing backend behavior, also affects `probe_transient`; file-synced clients should be aware). Follow-up same day: closed the file-sync diagnostics gap — `PUT .../document` now accepts `report: bool`; with `report=true` the text runs through a new backend method `step_chunk_report` (Scala, mirrors `verify_chunk`'s report but NEVER rolls back ordinary failures, LSP-style; still discards on budget timeout to cancel runaway commands) and stores the report in `last_chunk_report`, so `last_report`/`last_diagnostics` works uniformly for both MCP flavors. `proof_open` probing is skipped when the text ends with theory `end` (the limitation above). Tests: 4 more in `tests/test_readonly_mode_server_prep.py` (13 total, green). Endpoint↔MCP-tool mapping: internal notes appendix. | internal notes |
| 2026-08-12 | **Docs consolidation + Stage 1: Scala backend reorganization** | Housekeeping: moved `DESIGN_CHOICES.md`/`devnote.md`/`ISSUES.md` into new `docs/` (entry points README/AGENTS/CLAUDE stay at root; all live references updated). Stage 1 of the LSP-mode plan: split the 340-line Py4J facade `repl_backend.scala` into self-typed traits, one file per consuming workflow — `backend_lifecycle.scala`, `backend_probes.scala` (transient read-only probes, both MCPs), `backend_chunk_ops.scala` (chunk-centric MCP surface), `backend_file_ops.scala` (LSP-like file-sync surface) — `repl_backend.scala` is now a 61-line class with the shared probe plumbing. Zero behavior change (27 public methods moved verbatim, verified by grep + full smoke: verify_chunk / load_document(report) / probes / lifecycle all work through Py4J). Compiler-forced details: trait-visible members widened to `protected`; new files must be listed in `repl/etc/build.props` (Isabelle component build compiles an explicit source list). Also: file-header comments on all 13 Scala files + section markers in REPL.ML stating which workflow each serves; Scaladoc gaps filled; Protocol regrouped by concern and the missing `in_proof` declaration added. Next: Stage 2 (line+col diagnostics, goals_at_line, probe-before-end) per internal notes. | internal notes |
| 2026-08-12 | **Stage 2: Phase-2 read-only features (lean-lsp-mcp symmetry)** | Three backend upgrades, all smoke-verified live in the container. **2.1 line+col diagnostics**: `node_status_report` now emits per-command `range {start{line,col},end{line,col}}` (1-based, UTF-16 cols) via `node.command_iterator` + `Line.Document`; message-level offsets provably don't exist for DRAFT nodes (PIDE spans are position-less), so diagnostics carry their owning command's range — jEdit's granularity. Flows through `verify_chunk`/`last_report`/`load_document(report=true)`; additive `CommandRange`/`Position` schema models. **2.2 `goals_at_line` + `command_at_line`**: read-only jEdit-style line queries (`snapshot.current_command` + retained per-command STATE_MESSAGE results — no ML probes); `goals_before`/`goals_after` symmetry with lean_goal. Required flipping `ISABELLE_SHOW_STATES` default to true (config comment notes the trade-off; `.env` had it pinned false — flipped). **2.3 probes on finished theories**: root cause of the post-`end` probe hang (from the 2026-08-08 entry) confirmed empirically — past `end`, probe commands parse against the Pure bootstrap keyword table ("missing theory context"), the ML payload never runs. Fixed: `open_subgoals`/`in_proof` short-circuit to `[]`/`false` when the document ends with `end` (definitional); `sledgehammer`/`get_proof_state` fail fast instead of 2× channel timeout; `local_facts`/`global_facts`/`probe_transient` insert the probe BEFORE the `end` command via a new `Repl_Session.with_probe_before_end` bracket (mid-document `Text.Edit.insert`, bypassing Thy_Info's append-only bookkeeping, always removed in `finally`; document verified byte-identical after probing); `step_chunk_report` hardcodes `proof_open=false` after an `ok` `end`. Smoke: `thm foo`/`find_theorems` on a finished theory answer in ~0.2s (previously 2× timeout → 500); normal-path regression clean. Tests: 3 new (range mapping + goals parsing); 31 passed in-container (2 pre-existing `mcp`-package failures). | internal notes |
| 2026-08-14 | **Design decisions + checkpoint verification** | Design work for the LSP-like mode: chose **static verified heaps in a task-group-scoped heap pool** for imports over live draft-node syncing (recorded as DESIGN_CHOICES.md §1.13; both options researched — internal notes — overlays validated but deferred). Task-group tenancy + heap manifests + `/admin` page folded into the plan (internal notes). Read Tom Milan's IsabelleGym 1.0 thesis for precedent (imports as freeze/reuse boundary; editor-style access explicitly rejected for agents — supports the static-import call). **Legacy checkpoint machinery verified live** (`save_state`/`restore_state`): basic restore, multi-checkpoint history-tree restores (out-of-order A→B), branching after restore, and restore-undoes-kept-`verify_chunk` interplay — all pass; snapshots therefore planned for the new MCP surface. Housekeeping: the internal working notes reorganized into date-prefixed task folders (convention recorded in AGENTS/CLAUDE); Scala canned JSON responses centralized into `json_reports.scala`; `use_theories` spike (`2026-8-12(1)`): file-backed nodes load fine but reject interactive probe edits and lack the wrapper ML env — entry documents stay string-fed. | internal notes |
| 2026-08-15 | **Stage 3: heap-pool import system implemented** | Static verified imports per DESIGN_CHOICES.md §1.13 and internal notes. Scala: `Server_Utils.start_session` + `Session_Manager` thread `dirs` into `Session_Build.Args` (REPL sessions start on user heaps; heap sessions bypass the theories-only session cache; `load_document` resets preserve field+dirs). Python: new `services/heap_pool.py` — registry keyed by `(task_group, project)`, `isabelle build -b` orchestration (per-key lock + `ISABELLE_MAX_CONCURRENT_BUILDS` semaphore), persisted per-heap JSON manifests (ROOT text, per-file sha256/mtime, fingerprint, status) under `ISABELLE_HEAP_POOL_DIR` (in the `isabelle_user_data` volume; registry rebuilt at startup, interrupted builds come back failed). Endpoints: `POST /api/v1/heaps/build`, `GET /api/v1/heaps`, `GET/DELETE /api/v1/heaps/{group}/{project:path}` (full manifest), `GET/DELETE /api/v1/heap_groups...` (admin). Session tenancy: `task_group` (+`heap_session`/`project`) on create/acquire, 403 cross-group (namespace isolation, not security), staleness gate (source re-hash vs fingerprint → 422 with rebuild instructions), `dependency_key` gains group+fingerprint, wrapper states the qualified heap theories. Client methods for all of it. Tests: `tests/test_heap_pool.py` (10, fake-subprocess). Container smoke — ALL GREEN: build 2-theory heap → manifest with hashes → session proves with qualified import (`thm baz_lemma` unqualified works) → second session shares → beta group 403 → edit source → 422 stale → rebuild → new facts live; manifests survive server restart. Two bugs caught by the smoke: missing `HeapGroupInfo` router import (500 on /heap_groups), and rebuild without `session_name` re-derived the name from the project dir (renamed the heap) — now keeps the existing name, with a regression test. Follow-ups same day: **admin console + heap GC + metrics** — `GET /admin` serves a static heap-pool console (`server/app/static/admin.html`: groups, build/rebuild/delete, manifest viewer with per-file hashes and log tail); delete now GC's the on-disk heap image + build logs once no pool entry references the session name (`ISABELLE_HEAP_GC_IMAGES`, default true — caught that Poly/ML images are single FILES, not dirs); Prometheus gauges `isabellegym_heap_pool_heaps{task_group,status}` + `isabellegym_heap_build_seconds` via a per-scrape collector. 43 tests pass in-container (2 pre-existing mcp-package failures). | internal notes |
| 2026-08-15 | **Hover / go-to-definition / position-explicit sledgehammer** | Implemented per internal notes (spike-validated). **Hover + definition** (`GET .../hover?line=&col=`, `GET .../definition?line=&col=`): pure snapshot + `Rendering` reads on the DRAFT node (no evaluation, no edits, no overlays) — `hover_at` returns the tooltip contents (entity kind + type, e.g. `constant "List.list.hd" :: nat list ⇒ nat`); `definition_at` resolves entity markup def positions — heap/source entities to file positions (`~~/` expanded, symbol ranges decoded to 1-based UTF-16 line/col against the target file's text), the entry document's own entities to in-node line ranges via `snapshot.find_command_position`. **Position-explicit sledgehammer** (`POST .../sledgehammer_at` {line, subgoal?, timeout_s?}): first production use of the overlay machinery — new `Document_Utils.overlay_query` (attach print fn to an existing command with a full-perspective edit, poll for the instance's `finished` status marker, collect instance-tagged results, always remove) driving the new `isabellegym_sledgehammer` query op registered in REPL.ML (args [timeout_s, subgoal_i]). Key fix found by the pre-implementation spike: post-initial-method proof states are Forward mode, which `run_sledgehammer` rejects (`Proof.assert_backward`) — the op calls `Proof.enter_backward` first (read-only, safe), so any in-proof line works and subgoal selection (i=1/2 → distinct suggestions) holds. Shares the server-wide sledgehammer semaphore. Schemas (`HoverResponse`/`DefinitionResponse`/`DefinitionTarget`/`SledgehammerAt*`), Session wrappers (junk-tolerant json pass-through), client methods, Protocol synced. Tests: 7 new in test_readonly_mode_server_prep.py; 48 passed in-container (2 pre-existing mcp-package failures). Smoke: hover/def/sledgehammer_at pairs below the fold; tip sledgehammer regression clean. **Demo validation surfaced two more items** (see demo.ipynb new section): (1) KNOWN WART — the legacy tip `sledgehammer` (channel probe) fails with "Illegal application of proof command in state mode" when the tip state follows an initial-method `proof (...)` command (Forward-mode state); the overlay op's `Proof.enter_backward` normalization is the known fix if we want parity there — not applied yet (behavior change to a production path; pending decision). (2) Cosmetic: overlay sledgehammer result text concatenates writeln chunks without newlines (content complete; formatting polish if the MCP wants prettier lines). | internal notes |
| 2026-08-15 | **Stage 4: LSP-like MCP server (`mcp_lsp_server/`)** | New package implementing the file-sync MCP per internal notes + user amendments (lean-lsp-mcp tool naming, warm scratch pool, 1-based UTF-16 positions, positioned sledgehammer). 23 tools: lifecycle (`isabelle_open`/`close`/`sync`), read-only file tools (diagnostic_messages with severity filter, goal, command_at_line, proof_state, source, query, local/global_facts, hover_info, definition, sledgehammer (tip or line+subgoal), checkpoint/restore/rollback/history/last_report), scratch execution (`isabelle_multi_attempt` — file prefix + candidates fanned out bounded-parallel on warm scratch sessions; `isabelle_run_code`), heap tools (`isabelle_build_heap`, `isabelle_heap_status`). `pool.py`: bindings keyed by canonical file path, disk re-read + `load_document(report=true)` on change before every call (copied-buffer; MCP never writes files), 404 → transparent rebind, scratch pool keyed by (task_group, heap, imports, field) with cap `ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE`. Config `ISABELLE_MCP_LSP_*` (streamable-http on :8849). Tests: `tests/test_mcp_lsp_server.py` (10: sync-cache, rebind-on-404, scratch keying/reuse/drop, prefix truncation, header parsing). Container `pip install "mcp<2"` (1.29.0 — mcp 2.0 removed `mcp.server.fastmcp`; with it installed the 2 pre-existing render_chunk test failures also resolve: full suite **60 passed**). Live smoke via a real streamable-http MCP client (internal notes): all 17 steps green on a fresh JVM (open with deliberate errors → per-line/col diagnostics; goal at a proof line; hover/def on a HOL constant (file position into `/opt/isabelle/src/HOL/List.thy`) and on an own lemma (in-node range); sledgehammer subgoal=1/2 distinct suggestions; multi_attempt per-candidate verdicts; run_code; checkpoint/restore; disk-edit → next query reflects it; build_heap → open with heap_session proves with heap facts; close). **NEW BUG surfaced (unfixed, needs its own investigation)**: the gateway JVM's shared `Event_Timer` got cancelled mid-smoke (twice, under scratch/session churn + a heap build) — every subsequent session create/reset then fails 500 with `java.lang.IllegalStateException: Timer already cancelled` at `Headless$Session.use_theories ← Event_Timer.request`, and the gateway liveness check (`gateway_alive` = process liveness) doesn't see it, so crash recovery never fires; only a container restart unwedges. Ruled out as triggers: simple close→create and reset→create cycles (both clean). Likely an eviction/teardown interleave cancelling the JVM-global `java.util.Timer`. | internal notes |
| 2026-08-15 | **Eval Level 0: LSP-MCP runner in the comparison harness** | New `MCP-comparison/run_isabellegym_lsp.py` driving `mcp_lsp_server` through the same OpenAI-compatible agent loop + neutral arbiter as the other runners (internal notes Level 0). Shape difference: the LSP MCP is read-only by design, so the runner adds LOCAL sandboxed `read_file`/`write_file` tools over a per-attempt workdir copy of the problem (`_safe_workdir_path` rejects escapes); setup is `isabelle_open(file_path)` + best-effort warmup (positioned sledgehammer on the sorry-holed theorem's statement line — a prove-mode state even with the sorry below); the DONE gate uses `isabelle_proof_state`/`isabelle_source`; verdict = arbiter on the final workdir file. System key `isabellegym_lsp`; `config.yaml` gains the stdio spawn entry, `analyze.py` the system + its sledgehammer log pattern. Runner keeps `mcp`/`openai` imports lazy so unit tests import without them. Tests: `tests/test_lsp_runner.py` (5: sandbox escape rejection, read/write roundtrip + validation, tool surface, and a mock-LLM smoke — scripted write_file(good proof)→DONE through the real stdio MCP + arbiter: `arbiter_solved=True`). Caveat surfaced: the mock smoke exposed that the server I had started via `bash -lc` lost the Docker ENV PATH (login shells reset it) so bigstep's bare `isabelle` spawn 500'd — server must be started with the container ENV PATH intact (`bash -c`); noted for the ops docs. Test env note: container now has `mcp` 1.29.0 + `openai` installed (mcp 2.0 dropped `mcp.server.fastmcp`); full suite: 84 passed, 4 failed — the 4 `test_phase2_phase3_fixes.py` failures are `ModuleNotFoundError: mcp_server` from the in-flight `mcp_server`→`mcp_stepwise_server` rename (host working tree), NOT this work; before the rename the suite was 83 passed 0 failed. | internal notes |
| 2026-09-04 | **Heaps discoverability + LSP imports fix** | `GET /api/v1/heaps/available` (base images from distribution + user heaps dirs with origin/size/mtime), admin console base-images section with image delete, `isabelle_heap_status` exposes the same to agents. Fixed the LSP MCP acquiring sessions without the file's own imports (theories-at-acquire): sessions are now acquired with the header imports parsed from the file, so non-`Main` parents resolve without the ~130 s doomed attempt. Full heap tutorial written (isabelle-humanize `isabellegym-heaps-tutorial(temp).md`). | commits on `2026-RC0`; `tests/test_mcp_lsp_heaps.py` |
| 2026-09-05 | **Bug 9 fix + Isabelle 2026-RC0 track** | RC0 migration (isabelle-humanize `.m0/rc0-migration/`): REPL Scala backend ported to Isabelle2026-RC0 (`a9e1683`), RC0 Dockerfile (`af14618`). Bug 9 (Event_Timer wedge): root-caused to upstream, fixed in RC0 (`88acf2619921`); our hardening (`d865f31`): bounded 5 s `alive()` probe (Event_Timer request + Console_Logger), 5 s-cached functional `gateway_alive()`, wedge recovery in `_ensure_gateway`. 12/12 stress rounds green. | `tests/test_gateway_wedge.py`; `Dockerfile.rc0` |
| 2026-09-08 | **Admin console: session management + base heaps** | Force-close from the console (lease-aware), theories column with full hover text, base-heap listing + delete. Follow-up gap fixed same week: released/unleased sessions were uncloseable — DELETE now accepts `X-Admin-Token` for unleased sessions (leased still require the matching lease). | `server/app/static/admin.html`, router |
| 2026-09-09 | **Bug 10 (lease-leak) + theory-parsing endpoint** | Bug 10: public `GET /api/v1/sessions` no longer returns `lease_id`; new token-gated `GET /api/v1/admin/sessions`; DELETE audit logging + `isabellegym_sessions_force_closed_total`; `isabelle_close(destroy?)` MCP tool. Header parsing: canonical `parse_theory_header` (nested-comment stripping + header anchoring) backing MCP `header_imports` and `build_verify.extract_imports`; endpoint `POST /api/v1/parse_theory_header`. Verified live on the Putnam q4 problem (4362 commands, no comment pollution). | `e2f89ae`, `0ae5e04`; `tests/test_lease_security.py` (8), `tests/test_theory_parsing.py` (10) |
| 2026-09-10 | **ML heap cap (issue 4)** | Container-level per-process ML memory bound: `ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap 9216"` (9 GB) in the Isabelle user settings — the only working injection point, since the polyml component's `etc/settings` clobbers env/`.env` values. Builds capped immediately, sessions after next gateway restart. Owner decision: stay 32-bit `x86_64_32-linux` (no heap rebuild). | volume `etc/settings`; tutorial §5 |
| 2026-09-11 | **Packaging: one-command setup + turnkey export image** | `setup.sh` (env bootstrap with generated admin token → build → up → healthz wait → `--verify`/`--build-heaps`), annotated `.env.example`, `container_entrypoint.sh` (component re-register + idempotent ML-cap write + foreground API), compose now auto-starts the server. Turnkey RC0 image for reproduction: clean rebuild via `build_rc0_image.sh` (scripted assembly — the declarative BuildKit path is blocked by the JAVA_HOME quirk), lean bake (heaps+etc only, contribs verified byte-identical), cold-tested with no volume (healthz, HOL-Library acquire, small-step proof, strict bigstep), exported as 9.8 GB tar.gz with recipient runbook. Two-track branch model established: `main` = 2025-2 + features, `2026-RC0` = main + compat delta; feature backports by cherry-pick. | `setup.sh`, `repl/Admin/container_entrypoint.sh`, `.env.example`, `build_rc0_image.sh`, `Isabelle2026-RC0_version_docker_image_instruction.md` |
| 2026-09-11 | **Repo restructure + main backports** | `ba61e6c` restructure (MCP-comparison → evaluation/, bench JSONs → evaluation/, PDF → previous works/, deletions) applied to main minus the EXPORT.md rename (main never carries RC0 image instructions); test sys.paths + doc references fixed on both branches. | `760d514`, `c17ff81` on main |
| 2026-09-13 | **Issue 5 log dig + resilience recs 1–3 (Bug 11)** | Full server-log dig of the 2026-09-10 incident rewrote the handoff narrative: the 06:21 "JVM death" was a ~64 s accept stall with NO gateway restart (the IndexError deque cluster was misattributed from the 03:39 incident); the real gateway death hung an acquire 57 min because Py4J reads are unbounded and the cleanup loop's probe never fired; the 95-min slowdown was two heavy sessions contending — recovery the instant the lease reaper closed one (08:00:18). Implemented: durable JVM logs (`logs/gateway-jvm.log` + per-PID rotated GC logs `logs/isabelle-jvm-gc-<pid>.log` via user-settings `ISABELLE_TOOL_JAVA_OPTIONS`), Py4J `read_timeout` (`ISABELLE_PY4J_READ_TIMEOUT`, 900 s), acquire wall timeout (`ISABELLE_TIMEOUT_SESSION_CREATE`, 600 s → 503), probe-exception logging. **Bug 11** discovered and documented: `ISABELLE_SCALA_JAVA_OPTIONS` is dead config (nothing consumes it); gateway JVM runs ZGC at `-Xmx4g` — allocation stall is the prime suspect for the slow window. | `tests/test_gateway_resilience.py` (6); ISSUES.md Bug 11 |
| 2026-09-15 | **B1: incremental PIDE document edits (Option B)** | New channel aside the step path: `Repl_Session.replace_document` (spliff diff → `Text.Edit` list → `session.update`; PIDE re-processes only from the first changed command), Py4J `sync_document`, `load_document` fast path (same-theory `report=True` loads; header/theory-name change → reset fallback; checkpoints invalidated both sides; absolute line semantics). Key audit insight: append-only was a repo-side invariant, not PIDE — checkpoint restore already pushed mid-document spliff diffs through the same funnel. Benchmark on the 1059-line IMO file: **full load 137.6 s → tip edit 0.1 s (~1000×)**, mid-file edit 32.3 s, no-op 0.1 s, all success=True. Backported to main (`c86f79a`), merged both ways (`f32c6e7`); clean image rebuilt from the committed branch and the container recreated with entrypoint auto-start + `--restart unless-stopped` (Docker Desktop restarts no longer take the gym down). | `735e171` (RC0), `c86f79a` (main); `tests/test_document_sync.py` (12) |

| 2026-09-21 | **Full code audit + roadmap** | Read-only audit of server/, repl/, both MCP servers, client, docs/config, the `isabelle-humanize` harness's documented pain points, and a web survey of the Sept-2026 Lean/Isabelle MCP frontier. Ranked findings (3 critical: `/admin` token disclosure, unauthenticated ML execution, heap path traversal; 10 high incl. REPL probe double-insert, unbounded `stable_node_snapshot`, event-loop-blocking cleanup, LSP scratch-slot leak/hang, checkpoint soundness, dead `mcp_server` rename, `mcp<2` pin), 20-row frontier gap matrix, humanize asks with status, P0/P1/P2 roadmap. | internal notes |
| 2026-09-22 | **Bug 12 + Bug 13 fixes (audit SEC-2/SEC-3)** | New `server/app/core/input_guards.py`: code-execution guard on every text endpoint + heap sources (`ISABELLE_ALLOW_ML_COMMANDS`), import-name validation (header injection), safe-name validation for heap identifiers, `project` under `ISABELLE_HEAP_ALLOWED_ROOTS`, containment checks on manifest/image paths, admin token on destructive heap endpoints (build stays open). SEC-1 (`/admin` token) deferred by owner. Host tests: 128 passed (7 FastAPI-dependent skipped on host; container run pending Docker). | internal notes, `tests/test_input_guards.py` |

| 2026-09-27 | **Refactor: router.py → route package** | The 1,067-line `server/app/api/v1/router.py` split into `routes/{health,sessions,execution,inspection,positional,automation,checkpoints,heaps}.py` + `deps.py` (`LeasedSession` dependency replaces 25 copies of the lease/get_session/logging preamble; `sledgehammer_slot` shared by both sledgehammer endpoints) + `serializers.py`; `router.py` is an 88-line aggregate + compat facade. Largest file now 267 lines. Behaviour-preserving: OpenAPI diff 39/39 paths identical (tags aside). New `tests/test_source_limits.py` gate (600 lines, ratcheting allow-list for session.py/heap_pool.py/async_client.py). In-container: 235 passed; 1 pre-existing unrelated failure (`test_gateway_resilience` caplog vs non-propagating logger). Discovered: container `/app` is a WSL staging copy, not the working tree — uncommitted code must be `docker cp`'d in. | internal notes |

| 2026-09-27 | **Remove `server_gym/`** | `server_gym/success_checker.py` (a live server dependency mislabelled "legacy") moved to `server/app/services/success_checker.py`; dead `server_gym/isabelle_gym.py` deleted; importers repointed (`session.py`, `session_bigstep.py`, `eval_smallstep_isabellegym.py`); `pyproject.toml` now packages `server*` instead of `server_gym*`. Server/client/MCP packages have no `local_gym` dependency (verified). Also: `.gitignore` now ignores `.vscode/` and `.clinerules/` unconditionally (the old exception rules kept re-admitting editor files). | internal notes |

| 2026-09-27 | **Consolidate benchmark runs** | 42 raw run files (22 MB) + 4 orphaned `mcp_bench_results*.json` + derived `analysis_exports/` replaced by `evaluation/results/benchmark_runs.json` (2.3 MB, 46 runs, 1,525 theory rows, per-step timing vectors kept, command text dropped) + `.csv` + README; generator `evaluation/scripts/consolidate_runs.py --check` (157 recorded values recomputed, drift 7.6e-8); `runs_analysis.ipynb` repointed at the summary. Inspection outcome: server families are still regenerable; local-gym and qIsabelle baselines are not (archived in the summary only); the MCP bench script no longer exists. | internal notes |

| 2026-09-27 | **Repo restructure (steps 1–4)** | `archive/` (previous-works, the 2.0 in-process gym + its baseline scripts, install.sh); `deploy/` (Dockerfiles, setup.sh, RC0 scripts, monitoring; compose stays at root with `dockerfile: deploy/Dockerfile`); `demo/` → `examples/`; deleted stale `repl/python/`, the committed `server.log.1`, `repl/temp.txt`. Client independence: `client/pyproject.toml` (httpx only), LSP MCP header parsing now via `POST /parse_theory_header` instead of importing server code, `tests/test_dependency_rules.py` import-direction contract. `.gitignore` typo `ClAUDE.md` fixed and `AGENTS.md`/`CLAUDE.md` tracked again; `*.log.*` ignored. Root loose files 17 → 12. | internal notes |

| 2026-09-27 | **Repo restructure (step 5): MCP package merge + devnote split** | `mcp_lsp_server/` + `mcp_stepwise_server/` → `mcp_servers/{lsp,stepwise,common}/` (shared env helpers + `GymClientMixin`); the old packages stay as deprecated launch/import shims so `python -m mcp_lsp_server.app` (humanize) keeps working. `docs/devnote.md` split into three dated files under `docs/experiments/` with an index left behind. `repl/` intentionally left at the top level (component registration + Dockerfiles + RC0 build script hard-code `/app/repl`). | internal notes |

| 2026-09-27 | **Repo restructure (step 6): `repl/` → `server/repl/`** | Owner decision. `git mv`; Python imports `server.repl.src.python.*`; the gateway now locates its Scala source relative to its own file (it hard-coded `/app/repl` before) and `isabelle` via `ISABELLE_HOME`; `Admin/init` re-rooted and now REMOVES stale `/app/repl` + `/app/repl/Admin` component registrations so existing `isabelle_user_data` volumes migrate on the next container start (`container_init.sh` treats a stale `/app/repl` entry as "needs init"); Dockerfiles, compose entrypoint, RC0 build script, `.gitignore`, packaging (`server*` only) and docs updated. `demo_repl.py` archived. **Deployed containers must be recreated from a rebuilt image (or the working tree synced) — the running RC0 image still has `/app/repl`.** | internal notes |

| 2026-09-27 | **Container verification of the restructure + client fixes** | RC0 container: `Admin/init` migration removed the stale `/app/repl` + `/app/repl/Admin` registrations and registered `/app/server/repl` on the live volume; suite 260 passed (1 pre-existing caplog failure); server restarted onto the new layout with the gateway alive; route smoke green. The MCP stdio smoke caught a **pre-existing client bug**: `parse_theory_header` posted to `/api/v1/sessions/parse_theory_header` (matched `/sessions/{session_id}` → 405) — fixed to `/api/v1/parse_theory_header`; new `tests/test_client_paths.py` checks every client route against the server's OpenAPI. The size gate then flagged `client/async_client.py` (650 > 647), so the planned client split landed: `client/{_base,sessions,execution,inspection,heaps}.py` mixins composed into the unchanged `IsabelleGymAsyncClient`; allow-list entry removed. MCP smoke green through `mcp_servers.lsp.app` and the deprecated shim. | internal notes |

| 2026-09-27 | **Isabelle download mirror order + slow-mirror bail-out** | `docker compose build` sat on `dist.isabelle.cit.tum.de` at ~150 KB/s (2.5 h ETA) because a slow mirror never "fails" the wget fallback chain. Measured: Cambridge ~8–16 MB/s, Proofcraft ~0.9 MB/s, TUM refuses/trickles, Clarkson ~170 KB/s. `deploy/Dockerfile` now tries Cambridge → Proofcraft → TUM → Clarkson with `curl --speed-limit 1000000 --speed-time 30` so any mirror under 1 MB/s for 30 s is abandoned. Rebuild: tarball in ~73 s. | `deploy/Dockerfile` |

| 2026-09-30 | **RC2 image fix + findings tracker** | `deploy/Dockerfile`: `fontconfig fonts-dejavu-core` added — Isabelle 2026 `Build.build_store` calls `Isabelle_Fonts.init()` on every session start, and the slim `--no-install-recommends` rewrite had dropped the packages, so every session died with "Fontconfig head is null". Rebuilt `isabellegym-isabelle-gym:2026rc2`, cold-tested standalone (healthz, fresh-session step proofs). Docker disk moved to `D:`; 2025-2 image, hand-assembled `2026rc0`, scratch `isabelle-rc2-base` removed; `2026rc0-clean` kept until the Isabelle2026 release. Added the [Open Findings Tracker](#open-findings-tracker) section for the remaining audit P0 items. | internal notes |

| 2026-09-30 | **Bug 14 / REPL-1: state probes → PIDE overlay queries** | Owner decision (Option B): instead of patching `with_probe_settle`, the five stepwise state probes were migrated from `ML_val` insert-and-discard to overlay Query_Operations hosted on the document's last command (same machinery as the LSP `sledgehammer_at`, which now shares the operation). `repl_ml_communication.scala` + `Scala_Functions` service + the ML channel senders deleted (net −300 lines); `node_ends_with_end` made non-blocking; `probe_transient` is the only insertion probe left. Verified live on the RC2 image (27/27 + bounded-wait smoke), unit suite green. DESIGN_CHOICES.md gained 1.14 and was reviewed for stale claims. | internal notes |

| 2026-09-30 | **Bug 15 / REPL-2: wall-bounded waits, small-step rollback on timeout** | `stable_node_snapshot`'s unbounded consolidation loop replaced by `node_snapshot` (no evaluation wait; used by every syntactic/read-only query) + `settled_node_snapshot(budget_ms)`. `step` and `probe_transient` now take the request timeout as a JVM wall budget (Py4J protocol change; Python future timeout = budget + `ISABELLE_TIMEOUT_BACKEND_GRACE`); on expiry `step` rolls the command back (owner: Option A). `rollback`/`vector_step` bounded by `ISABELLE_REPL_SETTLE_TIMEOUT`. Verified live (runaway returns at 3.2 s, next command 0.2 s), all smokes + unit suite green. DESIGN_CHOICES.md 1.15. | internal notes (Part 2) |

| 2026-09-30 | **Bug 16 / SRV-1: cleanup sweep off the event loop** | `cleanup_idle_sessions` → loop over a new `cleanup_once()`; `close_session`, `_relieve_memory_pressure`, the liveness probe and `_ensure_gateway` run in `asyncio.to_thread`; `/` and `/readyz` probe off-loop. `/metrics` was already safe (sync endpoint → threadpool). New `tests/test_cleanup_offloop.py` (loop-gap ticker); live: 245 `/healthz` probes during a sweep, worst 4 ms. DESIGN_CHOICES 1.9 gap note closed. | internal notes (Part 3) |

| 2026-09-30 | **Bug 17 / MCP-1: LSP scratch-slot leak** | `LspPool.scratch_session` async-context bracket (release / drop-on-404 / drop-on-cancellation, always in `finally`), bounded `acquire_scratch` (`ISABELLE_MCP_LSP_SCRATCH_WAIT_TIMEOUT`), deferred close of busy dropped sessions (`_retire_later`); `multi_attempt` and `run_code` rewritten on the bracket. +3 unit tests; live cancel-then-reuse check green with `SCRATCH_POOL_SIZE=1`. Owner: findings are fixed one per commit from here on (MCP-2, MCP-3 next). | internal notes (Part 4) |

| 2026-09-30 | **Bug 18 / SYNC-1: diff→edit offset corruption (sync + checkpoint restore)** | Found while verifying Bug 17 (reused scratch sessions verified garbage). spliff ops are a simultaneous base-coordinate script (inserts may sit inside a preceding deletion = "at its start"); the two cumulative-shift converters placed them one deletion-width early. New shared `Edit_Utils.diff_edits` (correct evolving-text mapping) backs `text_diff_edits` and `Thy_Status.difference_edits`. 10,003-case Scala round-trip property check + live sync repro + checkpoint back-and-forth all green. | internal notes (Part 5) |

| 2026-09-30 | **Bug 19 / MCP-2: bogus path no longer pins a session** | `_create_binding` validates the path before acquiring; test rewritten to the new contract; live check: three tools on a missing path raise, zero sessions leased, real file opens. | internal notes (Part 6) |

| 2026-09-30 | **Bug 20 / MCP-3: single-flight binding creation** | `get_binding` lookup under the lock + per-path in-flight future; +2 tests; live: three concurrent tools on a new file lease one session. With MCP-1..3 closed, the LSP MCP P0 group is done; MCP-4 (`reuse_dirty`) remains. | internal notes (Part 7) |

| 2026-09-30 | **Bug 21 / MCP-4: clean-only reuse for LSP bindings** | `reuse_dirty=False` on the binding acquire; fake client honours the dirty rule; live: released dirty session skipped, fresh one for the next file. All four LSP MCP audit findings (MCP-1..4) now closed. | internal notes (Part 8) |

| 2026-09-30 | **Bug 22 / REPL-4: checkpoint restore soundness** | Issued-ids-only validation, `Option`-returning `Thy_Info.restore_state` + all-or-nothing `Repl_Session.restore_state`, Python honours the result and drops rejected ids, route reports the reason. +3 unit tests, live soundness smoke. Remaining open: TEST-1, SEC-1 (deferred), ENV-1, RC2-1/2. | internal notes (Part 9) |

| 2026-09-30 | **TEST-1: MCP tools smoke + HTTP security-input suites (Bugs 23, 24 found and fixed)** | `tests/test_mcp_tools_smoke.py` (both servers in-process: tool sets, descriptions, prompt, shims) and `tests/test_security_inputs.py` (78 HTTP-layer payloads via TestClient + stub manager/heap-pool overrides: ML in 5 spellings on 4 text endpoints, diagnostic denylist, import/theory-name injection, traversal, admin token). Found: theory-name injection via `enter_theory` path + two model fields (Bug 23, fixed); 13 LSP tools with empty descriptions (Bug 24, fixed). `test_mcp_comparison_fixes.py` now skips without `openai` — the suite runs with no flags: 376 passed / 2 skipped. | internal notes (Part 10) |

| 2026-10-16 | **Research: memory management + MCP memory awareness** | Read the whole memory path (cgroup gate, LRU, ML heap cap, MCP pools); found 9 gaps (G1–G9: create-time-only gate, no attribution, builds bypass the gate, MCP hoards leased sessions, 503 body lost in tools, expiry invisible, native hosts blind, no external-pressure signal, dead env keys); rebuilt the Docker image (`isabelle-pool-server:local`, Isabelle2026-RC2 — the local image was the July 2025-2 one). Owner decisions: advice MCP-side, attribution from Scala/ML, leased sessions never evicted. | `claude-work/2026-10-16-research-memory-mcp-awareness/REPORT.md` |
| 2026-10-17 | **Memory awareness implemented (builder/reviewer loop, 13 commits)** | `a1f992e` batch 1 bug fixes (G7 native fallback, G3 build gate + `ISABELLE_MAX_CONCURRENT_BIGSTEP_BUILDS`, capacity warnings, env hygiene); `663e83e` ML-side probe (`isabelle_pool_server_mem`: pid from `/proc/self`, Poly/ML stats); `d3eba17` tiers / structured 503s / `X-Memory-State` / build admit hook; `e2aaa9d` attribution + `discard_dead` + Scala `session_phase`; `41d9d9d` `/api/v1/memory`, `error_kind`, request-path death check; `87273bf` + `e422745` MCP registry, advice rules, reminders, `list_my_sessions` / `memory_status` / `release_idle`, scratch TTL, fan-out translation; `a23cd1a` + `8b3c116` + `8ba2b24` budget admission (reservation at the decision), size-aware eviction, tiered sweep, sledgehammer refused at critical, external pressure; `530e271` + `79a4f61` + `7390927` live verification and write-up. Live gates on the RC2 container incl. a lock-protected forced-pressure run through the fan-out tools with the release-then-retry recovery loop (batch 4, run 12). | plan + reviews: `claude-work/2026-10-17-plan-memory-mcp-awareness/`; builder notes + live scripts: `claude-work/2026-10-17-impl-memory-mcp-awareness/` |

| 2026-10-18 | **fill_sorries: server-side sledgehammer on the `sorry`s of a proof (builder/reviewer loop)** | `d1fc90e` stage 1a: `isabelle_pool_server_sledgehammer_fill` op (max_proofs, provers, `subgoals=N`), hosting by command index, `SessionFillMixin`, one `classify_sledgehammer` — **behaviour change:** `/sledgehammer` `success` and `/sledgehammer_at` `found` are true only for a `Try this:` line ("No proof found" was reported as success); both carry `outcome`/`method`. Stage 1b: per-session job (`POST/GET/DELETE …/fill_sorries`), tier-aware `SledgehammerSlots` (2/1/parked) replacing the fixed semaphore, patch builder, ETA, client methods. `260d78d` stage 1c: ATP attribution — the solvers hang off the gateway JVM in their OWN sessions (`bash_process` → `bash` → solver), so the sampler walks the gateway's descendants by parent link (not the process group), skips plumbing/poly subtrees, and reports `sledgehammer_mb` / `_peak_mb` / `_shared_with` per session; cleared when the solvers end (also on cancel). `aa96c6d` stage 2: LSP MCP tools `isabelle_fill_sorries` (sync, submit in document mode, poll, `continuing: true` after `ISABELLE_MCP_LSP_FILL_PATIENCE_S`) / `isabelle_fill_status` / `isabelle_fill_cancel` (never open a session; `no_fill_job` top-level 404). Stage 3: harness `--prompt-variant` (`prompts.<name>` in config.yaml, results under `runs/<system>/<name>/`, patience capped at tool timeout − 60) + the forced-pressure campaign (`live_fill.py`, `serverB_fill.sh` in the internal working notes). Open: a server-side `mode="current"` to avoid the double load per MCP call (review aa96c6d S4). |
| 2026-10-18 | **Zombie solver processes under pid 1** (review ed2d751 S2) | The container's pid 1 was `python -m server.app.main` (the entrypoint `exec`s it) and never reaps the solvers sledgehammer leaves reparented to it: every run adds `eprover`/`vampire`/`timeout`/`sh` zombies (`Z`, RSS 0; 77 after one campaign). They cost pids, not memory. Fix: `init: true` on the compose service (tini as pid 1) — recreate the container (`docker compose up -d isabelle-pool-server`), a `docker restart` keeps the old pid 1. Native installs: run under a reaping init or `systemd`. | `docker-compose.yml`; campaign script reports the zombie count | plan, reviews and research: internal working notes |
| 2026-10-20 | **Memory gate blind to a native cgroup limit** (memory-scaling plan review B1) | `MemoryMonitor` read `memory.current`/`memory.max` at the cgroup ROOT; on a native cgroup-v2 host those files do not exist there, so the gate fell back to `/proc/meminfo` + `MemTotal` and ignored a `systemd-run --scope -p MemoryMax=…` or unit `MemoryMax` limit (it would admit until the kernel killed a prover). Fix `62c7262`: the monitor resolves its own cgroup from `/proc/self/cgroup` and reads the v2 files there when present; the root/container case is unchanged. Native deployments (`deploy/native_setup.sh --systemd`) now get the gate for real. | `server/app/services/memory_monitor.py`; `tests/test_memory_batch3.py` |
| 2026-10-20 | **A document reset discarded the session as "prover died"** (memory-scaling smoke) | `load_document` on the reset path (`header_changed` decline of the incremental sync, or a theory-name change) restarts the session's ML process; the memory hook then compared the stale `ml_pid` and discarded a healthy session with `error_kind=prover_died`. Fix: `_reset_bookkeeping` → `forget_prover()` (pid, start time, probe time cleared; the death check falls back to the session phase and the next probe records the new pid). `backend.raw.reset()` is called in exactly one place (this path); rollback and checkpoint restore are edits on the same prover, so nothing else needs it. Also found: an edit in the whitespace right after `begin` counts as a header edit (`header_changed`) — the LSP MCP's sync falls back to a reset for it; one paragraph later the same edit takes the incremental path. | `server/app/services/session.py`, `session_memory.py`; `tests/test_memory_reset_pid.py`; `evaluation/memory_scaling/memscale/workload.py` (P5 placement) |
| 2026-10-21 | **Merge `impl-de-sorry` (fill_sorries stages 1a-3) into `Impl-memory-robustness` (builder/reviewer loop)** | `4a64f5c`: true `--no-ff` merge of `207ec06` (7 commits kept) onto `1b66ca5`. Two additive conflicts: `session_memory.py` (`forget_prover` from `ee20c6f` next to `note_sledgehammer_rss`/`clear_sledgehammer_rss`; both kept) and this work log's tail (rows re-ordered by date, one *Last updated*). `server/repl` byte-identical to `impl-de-sorry`. Checked interplay: a reset session's new poly is absent from the ATP sampler's exclude list until its next probe but is skipped by comm (`poly` in `ATP_EXCLUDED_COMMS`) — nothing to change. Verified: in-container suite 613 passed; host 634 passed + 7 Windows-only failures (incl. a `>` vs `>=` `max_total_s` boundary in `_run_fill`, owner item); `isabelle scala_build`; container recreated with `init: true` (tini pid 1) and a live smoke on the merged tree (fresh HOL session, `/sledgehammer` on a false goal -> `success=false`, fill job 1/2 sorries patched and re-verified, `sledgehammer_mb` attributed, header-change reset keeps the session, 0 zombies), reproduced by the reviewer. Docs: CLAUDE.md (fill routes/services/client methods, `ISABELLE_FILL_*` and `ISABELLE_MCP_LSP_FILL_*` knobs, tier-aware slot cap), `.env.example`. | plan, reviews, notes, live script: internal working notes |
| 2026-10-27 | **Training-metrics results (T1–T6); `axiomatization` denylisted; the report source leaves the tree** | T1–T6 ran on the server 2026-10-25..27 (census: 93 HOL-Analysis theories, 11,984 blocks, all proved; T4 890 of 890 verdicts agree with `isabelle build`; T3 restore 4 ms against 8–93 s of replay; T5 every looping step returned at its budget; T1 census work rate 8.3 at W = 32, verify-only 3,181 chunks/min). Owner decisions D16–D19 (`evaluation/training_metrics/README.md`): the splitter cuts outside every proof and before `private`/`qualified`, `census --keep-unchanged`, the PIDE MCP row runs a session per file as IPS does, no speedup across W. `axiomatization` added to the command denylist (`diagnostic_guard._DANGEROUS_RE`, test in `tests/test_input_guards.py`): a chunk that postulates an axiom and uses it was proved in-session and cold alike. Raw data: `training-metrics/` in the results repository. The technical report's source left the tree the same day (09a8801; the owner keeps it on Overleaf, last in-tree copy f6cae10); its own change history is kept with the source, not in this log. | `evaluation/training_metrics/`, `tests/test_input_guards.py` |
| 2026-10-27 | **`fill_sorries` text bypassed the command guard** (found by the report recheck, `claude-work/2026-10-27-review-report-v2/RECHECK_f6cae10.md` 3B.2) | `FillSorriesRequest.text` had no validator and `routes/fill.py` hands the text to `session.load_document` / `session.verify_chunk` directly, so with `ISABELLE_ALLOW_ML_COMMANDS` off the fill route still accepted `ML ‹…›` and `axiomatization` while every other text-taking model rejected them (confirmed by constructing the models). Fix: the same `reject_code_execution` validator on the fill model (`schemas/fill_models.py`); tests `test_fill_request_rejects_ml_and_axiomatization` and the policy-switch case in `tests/test_input_guards.py`. | `server/app/api/v1/schemas/fill_models.py`, `tests/test_input_guards.py` |
| 2026-10-27 | **The feature inventory and the mechanism prose move into the repository** | The feature inventory (until then an appendix of the report) is `docs/FEATURE_INVENTORY.md` (named `docs/experiments/2026-10-21-feature-inventory-ips-vs-pide-mcp.md` until the third review, 2026-10-27); the mechanism prose ("How the server works") is section 3 of `docs/DESIGN_CHOICES.md`; the report points to both. The E1/E3 raw tables and the E2/E4 records are `results/report-raw-tables.md` in the results repository. No endpoint named `check_batch` exists: the batch feature is the stepwise MCP's `verify_batch`. | `docs/DESIGN_CHOICES.md`, `docs/FEATURE_INVENTORY.md` |
| 2026-10-27 | **The E3 record corrected; the perspective cost reproduced on IPS; the qIsabelle lineage claim dropped** | Record correction from the E3 per-second samples: the PIDE MCP B provers that left the count at the 12th and 13th sessions were reclaimed (alive at 16 MB resident, `oom_kill` 0), and the four kills during the 14th load took four JVMs with their four provers (the table said "3 provers", the log said "began killing"). Perspective check on the local container (Isabelle2026, 16 GB, four HOL sessions on the E1 theory, proof-state output off): IPS provers 816–819 MB PSS with the empty visible range, 1,007–1,176 and 665–1,251 MB over two repeats with the full range; the Scala patch was reverted and the pool probed. qIsabelle is a `Toplevel` REPL (last commit 2024-06, Isabelle2024) and PIDE MCP's repository and preprint never mention it, so the qIsabelle-to-PIDE-MCP lineage claim was dropped; the branch-cost comparison has no external baseline. Artefacts: `claude-work/2026-10-27-review-report-v2/`, results repo `results/perspective-check/`. | `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md`, `docs/FEATURE_INVENTORY.md` |
| 2026-10-28 | **Corrections after the fourth review: the T1 figure without the CPU axis, 690 builds, medians, the PSS fit's intercept, per-prover memory, the crossover** | Figure code: `trainmetrics.analyze` draws the T1 figure without the CPU axis and names its lines; `analyze_t4.build_times` counted the three blocks with no fact name as builds (690, not 693; the README and the snippet corrected). Data facts: the T1 series and figure are medians over three repeats, not means; the host-used intercepts of the memory fits sit 0.55–0.68 GB below the PSS intercepts in all nine series while the slopes agree within 1 %, so PIDE MCP B's negative "fixed cost" was a measurement offset, not a property of the tool (sessions per 100 GB on PSS: IPS 111/91/77, PIDE MCP A 78/66/67, B 38/33/28); the per-prover figures recomputed from the per-second samples (cell medians: PIDE MCP A 963–1,284 MB on HOL, IPS 803–809); the PIDE MCP A / IPS crossover is at two to four sessions, not eight; the metis incident of the July comparison was Isabelle/Q's (`docs/experiments/2026-07-16-mcp-comparison-mathd276.md`), not IPS's. Code facts restated: `release_session` keeps the prover and returns the session to the pool, only `close_session` discards it; PISA and qIsabelle keep named states, so a branch from a saved state is not a replay there. Artefacts: `claude-work/2026-10-27-review-report-v2/prover_pss2.py`, results repo `training-metrics/plots/t1_throughput.png`, `REPORT_SNIPPET.md`. | `evaluation/training_metrics/README.md`, `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md` |
| 2026-10-29 | **The PIDE MCP row paired with IPS's blocks for the per-call comparison (D20); the driver's closing `end` fails a block that opens an instantiation** | The PIDE MCP row of T1 replayed the same blocks as IPS at the same 0.1 s resolution, so the two pair by block hash: at one worker 2,985 blocks in both rows, 2,966 proved under both (1 timed out, 18 proved under IPS only), 1,479 s under IPS against 1,443 s under PIDE MCP (within 3 %, 12 ms per call against the 0.1 s poll; medians 0.41 against 0.37 s; 57 % of the pairs within one poll; a single command 0.34 against 0.31 s on average); at eight workers IPS 11 % shorter, with eight workers sharing one PIDE MCP process. `analyze_pide.pair_latency` writes `t1_pide_latency.csv` (W 1 and 8, by chunk size) and, from 2026-10-30, `latency_quantiles` writes the quantiles of both tools' times to `t1_pide_latency_quantiles.csv` (the data of the report's distribution figure); both regenerate with `20_analyze.sh`. The 18 blocks proved under IPS only: three that name their own theory (D13), two that open an `instantiation`, which the driver's loop fails because it closes the text with `end` after every edit, so the instance proof is missing (the bad count falls again at the block that brings it, also counted as failed), and 13 after those in the same theories; at eight workers 86 of 24,702 verified blocks are instantiation-related. Effect: PIDE MCP's throughput understated by about 0.4 %, the paired comparison unaffected (such blocks are excluded), the memory experiments untouched (the memscale driver loads whole theories). Stated in the driver's docstring, the generated snippet and D20. | `evaluation/training_metrics/trainmetrics/analyze_pide.py`, `drive_pide_chunks.py`, `README.md` (D20); results repo `training-metrics/tables/t1_pide_latency*.csv`, `REPORT_SNIPPET.md` |
| 2026-11-04 | **The LSP MCP can start on the session image a file's imports name (`ISABELLE_MCP_LSP_FIELD_FROM_IMPORTS`); the server's `suggested_field` picks the largest distribution image** | Owner's change, reviewed and tested in `claude-work/2026-11-04-review-field-from-imports/` (live on the local container: `HOL-Library.Multiset` bound in 6 s instead of 51, `HOL-Computational_Algebra.Primes` in 5 s instead of 74; an image the server lacks is built inside the acquire, HOL-Combinatorics in 59 s). The preference order (HOL-Probability > HOL-Analysis > HOL-Number_Theory > HOL-Algebra > HOL-Computational_Algebra > HOL-Combinatorics > HOL-Library, else the first dotted import's session; path imports excluded) lives in `theory_parsing.suggested_field`, so `POST /parse_theory_header` is the one definition; the LSP reads it from the parse response for bindings and for both scratch paths (`isabelle_multi_attempt`, `isabelle_run_code`), and with the knob off sends `ISABELLE_MCP_LSP_FIELD` (`HOL`), which until now was never applied to bindings. |

*Last updated: 2026-11-04.*
