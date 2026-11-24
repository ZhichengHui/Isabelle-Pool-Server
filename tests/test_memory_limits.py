"""Stage D memory limits: the per-session memory guard (soft limit), cgroup scopes for session
provers and builds, and the one shared bound on heap builds."""
import asyncio
import json
import logging
import os
import stat
import types

import pytest

from server.app.core.config import Heap, Memory
from server.app.services import build_process, cgroup_scope, memory_guard as mg
from server.app.services import heap_pool as hp, import_heap as ih
from server.app.services.memory_guard import MemoryGuard, annotate_report, memory_kind_since

GB = 2**30


class FakeRaw:
    def __init__(self, heap_text, running=()):
        self.heap_text = heap_text
        self.running = list(running)
        self.cancel_running_calls = 0

    def ml_heap(self):
        return self.heap_text

    def cancel_running_commands(self):
        self.cancel_running_calls += 1
        out, self.running = self.running, []
        return json.dumps(out)


class FakeSession:
    def __init__(self, raw, attempts=0):
        self.session_id = "s1"
        self.status = "ready"
        self.backend = types.SimpleNamespace(raw=raw)
        self.attempts = attempts
        self.cancel_reasons = []

    def cancel_attempts(self, reason):
        self.cancel_reasons.append(reason)
        n, self.attempts = self.attempts, 0
        return n


def stats(in_use_gb, gcs=1, reserved_gb=15):
    size = int(reserved_gb * GB)
    return "\n".join([f"size_heap={size}", f"size_heap_free_last_GC={size - int(in_use_gb * GB)}",
                      f"full_GCs=0", f"partial_GCs={gcs}", "age_ms=300"])


def guard(*sessions, maxheap_mb=16384):
    sm = types.SimpleNamespace(_lru={i: s for i, s in enumerate(sessions)})
    return MemoryGuard(sm, maxheap_mb, fraction=0.75, interval_s=5)


def test_heap_in_use_is_reserved_minus_free_after_the_last_gc():
    s = mg.parse_heap(stats(3.0) + "\nbogus\nnow=1.5")
    assert mg.heap_in_use(s) == 3 * GB and s["now"] == 1
    assert mg.heap_in_use({}) is None
    # a large reserved heap with little in use is NOT over the limit (it does not shrink)
    sess = FakeSession(FakeRaw(stats(2.0, reserved_gb=15.9)), attempts=3)
    assert guard(sess).tick() == [] and sess.cancel_reasons == []


def test_over_the_limit_running_candidates_are_cancelled_with_reason_memory():
    raw = FakeRaw(stats(12.5), running=[{"line": 9, "kind": "by"}])
    sess = FakeSession(raw, attempts=3)
    g = guard(sess)
    [action] = g.tick()
    assert sess.cancel_reasons == ["memory"]
    assert action["candidates_cancelled"] == 3 and action["commands_cancelled"] == []
    assert raw.cancel_running_calls == 0  # the file's commands are left alone
    # no GC since the cancel: the figures are still the cancelled work's -> nothing again
    assert g.tick() == [] and sess.cancel_reasons == ["memory"]
    # after a GC, still over the limit and no attempt running: the running command goes
    raw.heap_text = stats(12.5, gcs=2)
    [action] = g.tick()
    assert action["commands_cancelled"] == [{"line": 9, "kind": "by"}]
    assert sess.memory_cancelled_lines == {9}


def test_under_the_limit_nothing_is_cancelled():
    raw = FakeRaw(stats(12.0 - 0.01), running=[{"line": 9, "kind": "by"}])
    sess = FakeSession(raw, attempts=2)
    assert guard(sess).tick() == []
    assert sess.cancel_reasons == [] and raw.cancel_running_calls == 0
    assert sess.guard_heap["size_heap"] == 15 * GB  # the reading is kept for the listings


def test_nothing_running_means_no_action_and_no_hold():
    raw = FakeRaw(stats(13.0))
    sess = FakeSession(raw)
    g = guard(sess)
    assert g.tick() == [] and getattr(sess, "guard_hold_gc", None) is None
    raw.running = [{"line": 4, "kind": "value"}]
    assert g.tick()[0]["commands_cancelled"] == [{"line": 4, "kind": "value"}]


def test_a_broken_session_does_not_stop_the_check_of_the_others():
    class Broken(FakeRaw):
        def ml_heap(self):
            raise RuntimeError("gateway gone")
    bad = FakeSession(Broken(""))
    good = FakeSession(FakeRaw(stats(13.0)), attempts=1)
    good.session_id = "s2"
    assert [a["session_id"] for a in guard(bad, good).tick()] == ["s2"]


def _report(status="failed", text="Interrupt"):
    return {"commands": [
        {"node_line": 5, "kind": "lemma", "status": "ok", "messages": []},
        {"node_line": 9, "kind": "value", "status": status,
         "messages": [{"sev": "error", "text": text}] if text else []}]}


def test_the_next_report_marks_the_cancelled_command_with_error_kind_memory():
    sess = types.SimpleNamespace(memory_cancelled_lines={9}, memory_cancel_at=100.0)
    rep = _report()
    assert annotate_report(sess, rep, started_at=200.0)
    assert rep["error_kind"] == "memory"
    texts = [m["text"] for m in rep["commands"][1]["messages"]]
    assert texts[0] == "Interrupt" and "memory guard" in texts[1]
    assert annotate_report(sess, rep, started_at=300.0)  # idempotent: one guard message
    assert len(rep["commands"][1]["messages"]) == 2
    # a report taken before the cancel took effect (still running) keeps the mark
    assert not annotate_report(sess, _report(status="running", text=None), started_at=350.0)
    assert sess.memory_cancelled_lines == {9}
    # a cancelled command has no message of its own: the guard's message is its only one
    bare = _report(text=None)
    assert annotate_report(sess, bare, started_at=360.0)
    assert [m["text"] for m in bare["commands"][1]["messages"]] == [mg._text()]
    # the command was changed and runs fine now: the mark is gone
    ok = _report(status="ok", text=None)
    assert not annotate_report(sess, ok, started_at=400.0) and "error_kind" not in ok
    assert sess.memory_cancelled_lines == set()


def test_a_cancel_during_the_request_marks_interrupted_commands():
    sess = types.SimpleNamespace(memory_cancelled_lines=set(), memory_cancel_at=150.0)
    rep = _report()
    assert annotate_report(sess, rep, started_at=100.0) and rep["error_kind"] == "memory"
    other = _report(text="Failed to apply proof method")  # an ordinary failure is not marked
    sess2 = types.SimpleNamespace(memory_cancelled_lines=set(), memory_cancel_at=150.0)
    assert not annotate_report(sess2, other, started_at=100.0)
    assert memory_kind_since(sess, 100.0) == "memory" and memory_kind_since(sess, 151.0) is None
    assert memory_kind_since(types.SimpleNamespace(), 0.0) is None


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_effective_maxheap_is_read_from_the_ml_options(tmp_path):
    isa = _script(tmp_path, "isabelle",
                  'echo \'ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap 16384"\'')
    assert mg.effective_maxheap_mb(isa, 9216) == 16384
    isa_g = _script(tmp_path, "isabelle_g", "echo 'ML_OPTIONS=--maxheap 12G'")
    assert mg.effective_maxheap_mb(isa_g, 9216) == 12288
    none = _script(tmp_path, "isabelle_n", "echo 'ML_OPTIONS=--minheap 500'")
    assert mg.effective_maxheap_mb(none, 9216) == 9216
    assert mg.effective_maxheap_mb(str(tmp_path / "missing"), 9216) == 9216


def test_maxheap_default_is_16384():
    assert Memory.ML_MAXHEAP_MB == 16384 or os.getenv("ISABELLE_ML_MAXHEAP_MB")
    text = open("server/repl/Admin/ensure_settings.sh", encoding="utf-8").read()
    assert 'ISABELLE_ML_MAXHEAP_MB:-16384' in text


@pytest.fixture
def scope_env(monkeypatch):
    monkeypatch.setattr(cgroup_scope, "_state", {"checked": False, "available": False,
                                                 "reason": "not checked"})
    monkeypatch.delenv(cgroup_scope.POLICY_ENV, raising=False)
    monkeypatch.setattr(Memory, "CGROUP_SCOPES", "auto")
    yield
    os.environ.pop(cgroup_scope.POLICY_ENV, None)


def test_without_systemd_run_the_server_runs_unscoped_with_a_warning(tmp_path, monkeypatch,
                                                                     scope_env, caplog):
    fail = _script(tmp_path, "systemd-run", 'echo "Failed to connect to bus" >&2; exit 1')
    monkeypatch.setattr(cgroup_scope, "SYSTEMD_RUN", fail)
    caplog.set_level(logging.WARNING)
    assert cgroup_scope.init() is False
    assert cgroup_scope.POLICY_ENV not in os.environ
    assert cgroup_scope.build_prefix() == [] and build_process.scoped(["isabelle"]) == ["isabelle"]
    assert cgroup_scope.slice_used_bytes() == 0
    assert any("systemd-run --user is not usable" in r.getMessage()
               and "Failed to connect to bus" in r.getMessage() for r in caplog.records)
    # a missing binary is the same case
    monkeypatch.setattr(cgroup_scope, "SYSTEMD_RUN", str(tmp_path / "absent"))
    assert cgroup_scope.init() is False


def test_with_systemd_run_sessions_and_builds_get_their_scopes(tmp_path, monkeypatch, scope_env):
    log = tmp_path / "calls"
    ok = _script(tmp_path, "systemd-run", f'echo "$@" >> {log}; shift 9; exec "$@"')
    monkeypatch.setattr(cgroup_scope, "SYSTEMD_RUN", ok)
    assert cgroup_scope.init() is True
    policy = os.environ[cgroup_scope.POLICY_ENV]
    assert policy.startswith(f"{ok} --user --scope") and "-p MemoryMax=24G" in policy
    assert "-p MemorySwapMax=0" in policy
    assert policy.endswith("--unit={unit}")
    assert f"--slice=ips_pool_" in policy
    prefix = cgroup_scope.build_prefix()
    assert prefix[:3] == [ok, "--user", "--scope"] and "MemoryMax=32G" in prefix
    assert prefix[-2:] == ["-p", "MemorySwapMax=0"]
    # a build runs through the prefix (systemd-run execs it: same pid, same process group)
    rc, timed_out = asyncio.run(build_process.run(["bash", "-c", "echo built"], tmp_path / "b.log"))
    assert (rc, timed_out) == (0, False) and "built" in build_process.tail(tmp_path / "b.log")
    assert "MemoryMax=32G" in log.read_text().splitlines()[-1]


def test_off_means_no_scope_even_when_systemd_run_works(tmp_path, monkeypatch, scope_env):
    monkeypatch.setattr(cgroup_scope, "SYSTEMD_RUN", _script(tmp_path, "systemd-run", "exit 0"))
    monkeypatch.setattr(Memory, "CGROUP_SCOPES", "off")
    assert cgroup_scope.init() is False and cgroup_scope.POLICY_ENV not in os.environ


def test_a_policy_set_by_hand_is_kept(tmp_path, monkeypatch, scope_env):
    monkeypatch.setattr(cgroup_scope, "SYSTEMD_RUN", _script(tmp_path, "systemd-run", "exit 0"))
    monkeypatch.setenv(cgroup_scope.POLICY_ENV, "systemd-run --user --scope -p MemoryMax=3G")
    assert cgroup_scope.init() is True
    assert os.environ[cgroup_scope.POLICY_ENV] == "systemd-run --user --scope -p MemoryMax=3G"


def test_the_slice_usage_counts_for_the_admission_gate(tmp_path, monkeypatch, scope_env):
    d = tmp_path / "slice"
    d.mkdir()
    (d / "memory.current").write_text(str(5 * GB))
    (d / "memory.stat").write_text(f"anon 1\ninactive_file {GB}\n")
    monkeypatch.setattr(cgroup_scope, "slice_dir", lambda: str(d))
    assert cgroup_scope.slice_used_bytes() == 4 * GB
    from server.app.services import memory_monitor as mm
    own = tmp_path / "own"
    own.mkdir()
    (own / "memory.current").write_text(str(2 * GB))
    (own / "memory.stat").write_text("inactive_file 0\n")
    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(own / "memory.current"))
    monkeypatch.setattr(mm, "_CG_V2_STAT", str(own / "memory.stat"))
    mon = mm.MemoryMonitor()
    mon.cgroup_dir = str(own)  # the server in its own (non-root) cgroup
    assert mon._read_used() == 6 * GB
    mon.cgroup_dir = None  # root cgroup (container): no scopes there
    assert mon._read_used() == 2 * GB


def test_heap_pool_and_import_heap_builds_share_one_bound(tmp_path, monkeypatch):
    """6 builds asked at once (3 heap-pool, 3 import-heap) with the bound 4: at most 4 run."""
    monkeypatch.setattr(Heap, "MAX_CONCURRENT_BUILDS", 4)
    monkeypatch.setattr(Heap, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ih, "BUILDER", ih.Builder(isabelle="isabelle"))
    running, peak = [0], [0]

    async def fake_run(cmd, log_path, timeout_s=None, env=None, on_start=None):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.3)
        running[0] -= 1
        return 1, False

    monkeypatch.setattr(build_process, "run", fake_run)
    pool = hp.HeapPool(state_dir=str(tmp_path / "heap_state"), allowed_roots=[str(tmp_path)])
    projects, plans = [], []
    for i in range(3):
        proj = tmp_path / f"p{i}"
        proj.mkdir()
        (proj / f"T{i}.thy").write_text(f"theory T{i} imports Main begin end\n")
        projects.append(str(proj))
        plans.append(ih.plan([str(proj / f"T{i}")], "HOL", []))

    async def run():
        heap_builds = [pool.build("g", p, session_name=f"S_t{i}") for i, p in enumerate(projects)]
        for p in plans:
            ih.BUILDER._start(p)
        await asyncio.gather(*heap_builds, return_exceptions=True)
        await asyncio.gather(*(j.task for j in ih.BUILDER._jobs.values()), return_exceptions=True)

    asyncio.run(run())
    assert peak[0] == 4


def test_the_build_bound_defaults_to_4():
    assert Heap.MAX_CONCURRENT_BUILDS == 4 or os.getenv("ISABELLE_MAX_CONCURRENT_BUILDS")


def test_a_candidate_cancelled_for_memory_is_not_reported_as_timed_out():
    from server.app.services.session_attempt import attempt_reports
    raw = {"prefix": {"all_ok": True, "settled": True}, "candidates": [
        {"commands": [{"line": 1, "kind": "value", "status": "running"}], "timed_out": True,
         "finished": False, "cancelled": "memory", "elapsed_ms": 21244},
        {"commands": [{"line": 1, "kind": "lemma", "status": "ok"}], "finished": True,
         "elapsed_ms": 4}]}
    hog, light = attempt_reports(raw, 7, ["value ...", "lemma ..."])["candidates"]
    assert hog["cancelled"] == "memory" and hog["report"]["timed_out"] is False
    assert not hog["report"]["success"]
    assert light["cancelled"] is None and light["report"]["success"]
