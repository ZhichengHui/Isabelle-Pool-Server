"""A document reset gives the session a new ML process: the memory hook must
not read the stale pid as "prover died" and discard the session (seen live in
the memory-scaling smoke, 2026-10-20)."""

from __future__ import annotations

import uuid

from server.app.api.v1.memory_hooks import run_with_memory_hooks
from server.app.services.session_memory import MemoryAwareSession
from server.app.services.session_reset import reset_for_load


class _Raw:
    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def session_phase(self):
        return "Ready"

    def mem_stats(self):
        return {"pid": "4242", "size_heap": "1000"}

    def exit(self):
        return None


class _Backend:
    def __init__(self):
        self.raw = _Raw()

    def submit(self, fn):
        import concurrent.futures

        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        return None


class _Mgr:
    def __init__(self):
        self.discarded = []

    def discard_dead(self, sid, reason="prover_died"):
        self.discarded.append(sid)
        return True


def _session():
    return MemoryAwareSession(
        session_id=uuid.uuid4(),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(),
        task_group="default",
    )


def test_reset_forgets_the_old_pid_and_the_hook_keeps_the_session():
    s = _session()
    s.ml_pid, s.ml_starttime = (
        999_999_999,
        1,
    )  # a pid that does not exist: "dead" by /proc
    mgr = _Mgr()
    _, override = run_with_memory_hooks(lambda: "x", s, mgr)
    assert override == "prover_died" and mgr.discarded == [
        s.session_id
    ]  # the stale pid rule, unchanged
    s2 = _session()
    s2.ml_pid, s2.ml_starttime = 999_999_999, 1
    s2.backend_used = True  # a used backend: the reset path really resets
    reset_for_load(s2, 5.0)  # what load_document's reset path calls
    assert s2.ml_pid is None and s2.mem_sampled_at == 0.0
    mgr2 = _Mgr()
    result, override = run_with_memory_hooks(lambda: "ok", s2, mgr2)
    assert result == "ok" and override is None and mgr2.discarded == []
    assert s2.ml_pid == 4242  # the probe after the call recorded the NEW pid


def test_a_pristine_backend_is_not_reset():
    """A session that has done nothing since it was created is already what a reset
    produces (same theories, field, dirs): the first load must not restart its prover."""
    s = _session()
    assert reset_for_load(s, 5.0) is False and s.backend.raw.resets == 0


def test_a_used_backend_is_reset():
    s = _session()
    s._call_backend(lambda: s.backend.raw.session_phase())  # any backend call at all
    assert reset_for_load(s, 5.0) is True and s.backend.raw.resets == 1


def test_a_load_that_failed_after_its_reset_still_counts_as_used():
    """The reset path clears the Python bookkeeping before it enters the theory; if the
    load then fails, the next load must reset again (live probe 2026-11-09: a failed
    header load, then a theory-name change, skipped the reset and lost the session)."""
    s = _session()
    reset_for_load(s, 5.0)  # first load: pristine, no reset
    s._call_backend(lambda: None)  # ... its enter_thy / step reached the backend, then failed
    s._reset_bookkeeping()
    assert reset_for_load(s, 5.0) is True and s.backend.raw.resets == 1


def test_a_reset_that_times_out_is_not_read_as_a_prover_death():
    """The reset stops the recorded process; if it then outlives the budget, the
    death check must not find that stale pid and discard the session."""
    s = _session()
    s.backend_used = True
    s.ml_pid, s.ml_starttime = 999_999_999, 1  # the process the reset stops

    def slow_reset():
        raise TimeoutError("Backend call timed out after 20.0s")

    s.backend.raw.reset = slow_reset
    try:
        reset_for_load(s, 5.0)
    except Exception:  # noqa: BLE001  (the timeout itself is the caller's to report)
        pass
    assert s.ml_pid is None and s.check_prover_dead() is False
