"""Batch 3 — server robustness (plan §2 batch 3):
budget admission (static per-field estimates + reservations), size-aware
eviction of UNLEASED sessions only, tiered sweep interval, sledgehammer
refused at critical (never at warning), external pressure from the host/VM.
No Isabelle: fake cgroup / meminfo files, stub sessions and managers.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from server.app.core.config import Memory
from server.app.dependencies import get_session_manager
from server.app.errors import MemoryPressure
from server.app.main import app
from server.app.services import memory_monitor as mm
from server.app.services import memory_policy as mp
from server.app.services.session_manager import SessionManager

MB = 1024**2


def _snap(used_mb, limit_mb, **kw):
    return mm.MemorySnapshot(
        used_bytes=int(used_mb * MB), limit_bytes=int(limit_mb * MB), **kw
    )


def _monitor():
    m = mm.MemoryMonitor()
    m.pressure_threshold, m.warning_threshold, m.min_available_bytes = (
        85.0,
        70.0,
        512 * MB,
    )
    return m


# ------------------------------------------------------------- estimates


def test_estimate_table_and_fallback(monkeypatch):
    assert mp._parse_estimates("HOL=2048, HOL-Analysis=4096,bad,=1,X=notanumber") == {
        "HOL": 2048,
        "HOL-Analysis": 4096,
    }
    monkeypatch.setattr(mp, "SESSION_ESTIMATES_MB", {"HOL": 2048, "HOL-Analysis": 4096})
    assert mp.estimate_session_mb("HOL-Analysis") == 4096
    assert mp.estimate_session_mb("Unknown-Session") == Memory.SESSION_ESTIMATE_MB
    assert mp.estimate_session_mb(None) == Memory.SESSION_ESTIMATE_MB


def test_can_admit_with_extra_bytes_and_external_pressure():
    m = _monitor()
    snap = _snap(10000, 14336)  # 69.8 %
    assert m.can_admit(snap) is True
    assert m.can_admit(snap, extra_bytes=2048 * MB) is True  # 84 %
    assert m.can_admit(snap, extra_bytes=4096 * MB) is False  # 98 %
    assert (
        m.can_admit(_snap(1000, 14336, host_available_bytes=100 * MB)) is False
    )  # host short
    assert m.can_admit(_snap(0, 0)) is True  # no limit known


# ---------------------------------------------------------- budget admission


def _manager(snap, enabled=True):
    sm = SessionManager()
    sm.memory = _monitor()
    sm.memory.read = lambda: snap  # type: ignore[method-assign]
    sm.memory_management_enabled = enabled
    return sm


def test_admits_session_by_field_estimate(monkeypatch):
    monkeypatch.setattr(mp, "SESSION_ESTIMATES_MB", {"HOL": 2048, "HOL-Analysis": 4096})
    sm = _manager(_snap(10000, 14336))
    assert sm.admits_session(sm.memory.read(), "HOL") is True
    assert sm.admits_session(sm.memory.read(), "HOL-Analysis") is False
    sm.creates_in_flight = 1  # another HOL-sized create is already reserved
    assert sm.admits_session(sm.memory.read(), "HOL") is False
    sm.creates_in_flight = 0
    sm.build_verifier.inflight = 1  # a build reserves BUILD_ESTIMATE_MB
    assert sm.admits_session(sm.memory.read(), "HOL") is False
    assert sm.reserved_bytes() == Memory.BUILD_ESTIMATE_MB * MB


def test_build_admission_uses_build_estimate(monkeypatch):
    monkeypatch.setattr(Memory, "BUILD_ESTIMATE_MB", 4096)
    sm = _manager(_snap(9000, 14336))  # 62.8 %; + 4096 -> 91 %
    with pytest.raises(MemoryPressure) as exc:
        sm.require_memory_for_build("bigstep")
    assert exc.value.facts["reserved_mb"] == 0.0
    sm2 = _manager(_snap(5000, 14336))  # + 4096 -> 63 %
    sm2.require_memory_for_build("bigstep")  # admitted


# ------------------------------------------------------ size-aware eviction


class _S:
    def __init__(self, rss_mb=None, leased=False, in_use=False):
        self.session_id = uuid.uuid4()
        self.rss_bytes = None if rss_mb is None else int(rss_mb * MB)
        self.leased, self.in_use = leased, in_use
        self.field, self.last_activity, self.label, self.task_group = (
            "HOL",
            0.0,
            None,
            None,
        )
        self.closed = False

    def close(self):
        self.closed = True


def test_pick_evictable_prefers_largest_unleased_then_oldest():
    sm = SessionManager()
    small, big, leased_big, busy, unknown = (
        _S(100),
        _S(900),
        _S(5000, leased=True),
        _S(7000, in_use=True),
        _S(),
    )
    for s in (small, big, leased_big, busy, unknown):
        sm._lru[s.session_id] = s
    with sm._lock:
        assert sm.pick_evictable_locked() == big.session_id
    sm._lru.pop(big.session_id)
    sm._lru.pop(small.session_id)
    with sm._lock:
        assert (
            sm.pick_evictable_locked() == unknown.session_id
        )  # no sizes: oldest idle unleased
    sm._lru.pop(unknown.session_id)
    with sm._lock:
        assert sm.pick_evictable_locked() is None  # leased / busy are never candidates


def test_relief_evicts_largest_first_and_never_leased(monkeypatch):
    monkeypatch.setattr(Memory, "EVICTION_SETTLE_S", 0.0)
    sm = SessionManager()
    verdicts = iter(
        [False, False, False, True]
    )  # pre-check, loop check, after b, after c: room
    sm.memory = type(
        "M",
        (),
        {
            "read": lambda self: _snap(13000, 14336),
            "can_admit": lambda self, snapshot=None, extra_bytes=0: next(verdicts),
            "state_for": lambda self, s: "critical",
        },
    )()
    a, b, c, l = _S(300), _S(2000), _S(800), _S(9000, leased=True)
    for s in (a, b, c, l):
        sm._lru[s.session_id] = s
    sm._relieve_memory_pressure("test")
    assert b.closed and c.closed and not a.closed and not l.closed
    assert set(sm._lru) == {a.session_id, l.session_id}


# ------------------------------------------------------------ tiered sweep


def test_sweep_interval_tiers():
    sm = SessionManager(cleanup_interval=60.0)
    sm.memory.last_state = "ok"
    assert sm.sweep_interval() == 60.0
    sm.memory.last_state = "warning"
    assert sm.sweep_interval() == Memory.WARNING_SWEEP_INTERVAL_S
    sm.memory.last_state = "critical"
    assert sm.sweep_interval() == Memory.WARNING_SWEEP_INTERVAL_S
    sm2 = SessionManager(cleanup_interval=5.0)
    sm2.memory.last_state = "critical"
    assert sm2.sweep_interval() == 5.0  # never slower than the configured interval


# -------------------------------------------- heavy work refused at critical


def test_refuse_heavy_only_at_critical():
    sm = _manager(_snap(13500, 14336))  # critical
    with pytest.raises(MemoryPressure) as exc:
        sm.refuse_heavy_if_critical("sledgehammer")
    assert exc.value.facts["state"] == "critical"
    _manager(_snap(11000, 14336)).refuse_heavy_if_critical(
        "sledgehammer"
    )  # warning: allowed
    _manager(_snap(13500, 14336), enabled=False).refuse_heavy_if_critical(
        "sledgehammer"
    )


def test_sledgehammer_route_refused_at_critical():
    class _Session:
        session_id = "11111111-1111-1111-1111-111111111111"
        field = "HOL"
        leased = True

        def sledgehammer(self, timeout_s):
            raise AssertionError("must not run at critical")

    class _Manager:
        memory = type("M", (), {"last_state": "critical"})()
        memory_management_enabled = True
        sledgehammer_sem = None

        def get_session(self, sid, lease_id=None, require_lease=True):
            return _Session()

        def refuse_heavy_if_critical(self, kind):
            raise MemoryPressure(
                "critical", facts={"state": "critical", "retry_after_s": 5}
            )

    app.dependency_overrides[get_session_manager] = lambda: _Manager()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            f"/api/v1/sessions/{_Session.session_id}/sledgehammer",
            json={"timeout_s": 5},
            headers={"X-Lease-Id": "L"},
        )
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
    assert resp.status_code == 503 and resp.json()["error"] == "memory_pressure"


# --------------------------------------------------------- external pressure


def _cgroup(tmp_path, monkeypatch, used_mb, limit_mb, host_available_mb):
    (tmp_path / "current").write_text(str(used_mb * MB))
    (tmp_path / "max").write_text(str(limit_mb * MB))
    (tmp_path / "meminfo").write_text(
        f"MemTotal: {16 * 1024 * 1024} kB\nMemAvailable: {host_available_mb * 1024} kB\n"
    )
    for name in ("_CG_V2_STAT", "_CG_V1_USAGE", "_CG_V1_LIMIT", "_CG_V1_STAT"):
        monkeypatch.setattr(mm, name, str(tmp_path / "absent"))
    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(tmp_path / "current"))
    monkeypatch.setattr(mm, "_CG_V2_MAX", str(tmp_path / "max"))
    monkeypatch.setattr(mm, "_PROC_MEMINFO", str(tmp_path / "meminfo"))


def test_external_pressure_flagged_when_host_is_short_but_container_is_not(
    tmp_path, monkeypatch
):
    _cgroup(tmp_path, monkeypatch, used_mb=4000, limit_mb=14336, host_available_mb=100)
    m = _monitor()
    snap = m.read()
    assert snap.external_pressure is True and snap.host_available_bytes == 100 * MB
    assert m.state_for(snap) == mm.STATE_CRITICAL and m.can_admit(snap) is False
    assert m.status_dict(snap)["external_pressure"] is True


def test_no_external_pressure_when_host_has_room(tmp_path, monkeypatch):
    _cgroup(tmp_path, monkeypatch, used_mb=4000, limit_mb=14336, host_available_mb=6000)
    snap = _monitor().read()
    assert snap.external_pressure is False and snap.host_available_bytes == 6000 * MB


def test_own_pressure_is_not_external(tmp_path, monkeypatch):
    _cgroup(tmp_path, monkeypatch, used_mb=13500, limit_mb=14336, host_available_mb=100)
    snap = _monitor().read()
    assert snap.external_pressure is False  # we ARE the pressure


def test_facts_carry_external_pressure_and_reservation(tmp_path, monkeypatch):
    _cgroup(tmp_path, monkeypatch, used_mb=4000, limit_mb=14336, host_available_mb=100)
    sm = SessionManager()
    sm.memory = _monitor()
    sm.creates_in_flight = 2
    facts = sm.memory_facts()
    assert facts["external_pressure"] is True and facts["host_available_mb"] == 100.0
    assert facts["reserved_mb"] == 2 * Memory.SESSION_ESTIMATE_MB


# -------------------------------------- review aa860ea B1 / S1 / S3: real monitor


class _FakeCgroup:
    """A fake cgroup whose memory.current shrinks when a session closes, so the
    relief loop sees its evictions (the verdict-iterator stub could not)."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        used_mb,
        limit_mb,
        host_total_mb=16000,
        host_avail_mb=8000,
    ):
        self.current = tmp_path / "current"
        self.current.write_text(str(used_mb * MB))
        (tmp_path / "max").write_text(str(limit_mb * MB))
        (tmp_path / "meminfo").write_text(
            f"MemTotal: {host_total_mb * 1024} kB\nMemAvailable: {host_avail_mb * 1024} kB\n"
        )
        for name in ("_CG_V2_STAT", "_CG_V1_USAGE", "_CG_V1_LIMIT", "_CG_V1_STAT"):
            monkeypatch.setattr(mm, name, str(tmp_path / "absent"))
        monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(self.current))
        monkeypatch.setattr(mm, "_CG_V2_MAX", str(tmp_path / "max"))
        monkeypatch.setattr(mm, "_PROC_MEMINFO", str(tmp_path / "meminfo"))

    def release(self, nbytes):
        self.current.write_text(str(int(self.current.read_text()) - nbytes))


class _Evictable(_S):
    def __init__(self, cg, rss_mb, **kw):
        super().__init__(rss_mb, **kw)
        self.cg = cg

    def close(self):
        super().close()
        self.cg.release(self.rss_bytes or 0)


def test_budget_refusal_evicts_largest_idle_then_admits(tmp_path, monkeypatch):
    """Plain rule admits (70 %), the HOL-Analysis budget (4096 MB) refuses, two
    sized idle unleased sessions exist: relief must evict the larger one and the
    create must then be admitted (B1)."""
    monkeypatch.setattr(Memory, "EVICTION_SETTLE_S", 0.0)
    monkeypatch.setattr(mp, "SESSION_ESTIMATES_MB", {"HOL-Analysis": 4096})
    cg = _FakeCgroup(tmp_path, monkeypatch, used_mb=10000, limit_mb=14336)
    sm = SessionManager()
    sm.memory = _monitor()
    small, big, leased = (
        _Evictable(cg, 2000),
        _Evictable(cg, 2000 + 1),
        _Evictable(cg, 3000, leased=True),
    )
    for s in (small, big, leased):
        sm._lru[s.session_id] = s
    snap = sm.memory.read()
    assert sm.memory.can_admit(snap) is True  # plain rule: fine at 70 %
    extra = sm.session_budget_bytes("HOL-Analysis")
    assert sm.memory.can_admit(snap, extra_bytes=extra) is False  # budget: 98 %
    snap = sm._relieve_memory_pressure("test", extra)
    assert big.closed and not small.closed and not leased.closed
    assert (
        sm.memory.can_admit(snap, extra_bytes=extra) is True
    )  # 10000 - 2001 + 4096 = 84 %
    assert set(sm._lru) == {small.session_id, leased.session_id}


def test_plain_relief_still_stops_at_plain_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(Memory, "EVICTION_SETTLE_S", 0.0)
    cg = _FakeCgroup(
        tmp_path, monkeypatch, used_mb=13000, limit_mb=14336
    )  # 90.7 %: refuses
    sm = SessionManager()
    sm.memory = _monitor()
    a, b = _Evictable(cg, 300), _Evictable(cg, 2000)
    for s in (a, b):
        sm._lru[s.session_id] = s
    sm._relieve_memory_pressure("sweep")  # extra_bytes=0: the sweep's plain rule
    assert b.closed and not a.closed  # largest first; 11000/14336 = 76.7 % admits again


def test_parse_estimates_strips_env_quotes():
    assert mp._parse_estimates('"HOL=2048,HOL-Analysis=4096"') == {
        "HOL": 2048,
        "HOL-Analysis": 4096,
    }
    assert mp._parse_estimates("'HOL=1'") == {"HOL": 1}


def test_external_pressure_requires_others_to_exceed_headroom(tmp_path, monkeypatch):
    # host short (100 MB) and others use 16384-100-4000 = 12284 > headroom 2150 -> external
    _cgroup(tmp_path, monkeypatch, used_mb=4000, limit_mb=14336, host_available_mb=100)
    snap = _monitor().read()
    assert snap.external_pressure is True and snap.host_others_used_bytes == 12284 * MB
    # unlimited-cgroup shape (limit = host total): the container at 83 % of the VM plus
    # ~1 GB kernel/daemons -> host short, but others (1000) <= headroom (2400): NOT external
    _cgroup(tmp_path, monkeypatch, used_mb=13300, limit_mb=16000, host_available_mb=400)
    (tmp_path / "meminfo").write_text(
        f"MemTotal: {16000 * 1024} kB\nMemAvailable: {400 * 1024} kB\n"
    )
    m = _monitor()
    snap = m.read()
    # others = 16000 - 400 - 13300 = 2300 <= headroom 2400 (and we are at 83 % ourselves)
    assert snap.host_others_used_bytes == 2300 * MB and snap.external_pressure is False
    assert m.can_admit(snap) is False  # the host is short regardless of cause
    assert m.status_dict(snap)["host_others_used_mb"] == 2300.0


# ------------------------------ reservation at the admission decision (live run 5/6)


class _FakeRaw:
    def exit(self):
        return None

    def mem_stats(self):
        return {}


class _FakeGateway:
    """Enough of ReplBackendGatewayProcess for _create_session: builds Java
    lists and returns a raw backend after a short delay (the race window)."""

    class _JVM:
        class java:  # noqa: N801
            class util:  # noqa: N801
                @staticmethod
                def ArrayList():  # noqa: N802
                    class _L(list):
                        def add(self, x):
                            self.append(x)

                    return _L()

    def __init__(self):
        self.gateway = type("G", (), {"jvm": self._JVM()})()
        self.created = 0

    def get_repl_backend_with_initial_theories(self, **kw):
        import time as _t

        _t.sleep(0.3)  # creation takes a while: concurrent creates overlap here
        self.created += 1
        return _FakeRaw()

    def get_repl_backend_tagged(self, tag, *args):  # the server's factory (backend_reaper.py)
        return self.get_repl_backend_with_initial_theories()

    def close_backend(self, tag):
        return False

    def is_alive(self):
        return True


def test_concurrent_creates_reserve_before_the_backend_exists(monkeypatch):
    """Two creates arrive together; the budget allows ONE estimate on top of
    usage. The first must reserve at its admission decision so the second is
    refused — before this fix both passed (the reservation came after awaits)."""
    monkeypatch.setattr(Memory, "ADMISSION_RETRIES", 0)
    monkeypatch.setattr(mp, "SESSION_ESTIMATES_MB", {"HOL": 2048})
    sm = SessionManager()
    sm.memory = _monitor()
    sm.memory.read = lambda: _snap(10000, 14336)  # type: ignore[method-assign]  # 70 %: one 2048 fits, two do not
    sm.gateway = _FakeGateway()
    sm._ensure_gateway = lambda: None  # type: ignore[method-assign]

    class _Thy:
        created_files = []

        def gen_file(self, name, deps):
            return type("R", (), {"data": f"{name}.thy", "err": None})()

    sm.thy_init = _Thy()

    async def both():
        results = await asyncio.gather(
            sm._create_session(["Main"], "HOL", lease_id="L1"),
            sm._create_session(["Main"], "HOL", lease_id="L2"),
            return_exceptions=True,
        )
        return results

    results = asyncio.run(both())
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["MemoryAwareSession", "MemoryPressure"], kinds
    assert sm.gateway.created == 1
    assert sm.creates_in_flight == 0
    for r in results:
        if not isinstance(r, Exception):
            r.close()


def test_monitor_reads_its_own_cgroup_when_not_root(tmp_path, monkeypatch):
    """Memory-scaling plan review B1: natively the root cgroup has no
    memory.current/max; a `systemd-run --scope -p MemoryMax=…` limit lives in
    the server's own cgroup directory, which the monitor must read."""
    import os

    cg_root = tmp_path / "cgroup"
    own = cg_root / "user.slice" / "exp.scope"
    own.mkdir(parents=True)
    (own / "memory.current").write_text(str(3 * 2**30) + "\n")
    (own / "memory.max").write_text(str(48 * 2**30) + "\n")
    (own / "memory.stat").write_text("anon 1\ninactive_file 1073741824\n")
    self_cg = tmp_path / "self_cgroup"
    self_cg.write_text("0::/user.slice/exp.scope\n")
    monkeypatch.setattr(mm, "_CG_ROOT", str(cg_root))
    monkeypatch.setattr(mm, "_PROC_SELF_CGROUP", str(self_cg))
    # the fixed root paths point at files that do NOT exist (native host)
    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(cg_root / "memory.current"))
    monkeypatch.setattr(mm, "_CG_V2_MAX", str(cg_root / "memory.max"))
    monkeypatch.setattr(mm, "_CG_V2_STAT", str(cg_root / "memory.stat"))
    for name in ("_CG_V1_USAGE", "_CG_V1_LIMIT", "_CG_V1_STAT"):
        monkeypatch.setattr(mm, name, str(tmp_path / "missing"))
    mon = mm.MemoryMonitor()
    assert mon.cgroup_dir == str(own)
    snap = mon.read()
    assert snap.source == "cgroup_v2"
    assert (
        snap.limit_bytes == 48 * 2**30 and snap.used_bytes == 2 * 2**30
    )  # current minus inactive_file
    # the root cgroup (container): nothing changes, the fixed paths are used
    self_cg.write_text("0::/\n")
    (cg_root / "memory.current").write_text(str(5 * 2**30) + "\n")
    (cg_root / "memory.max").write_text("max\n")
    mon2 = mm.MemoryMonitor()
    assert (
        mon2.cgroup_dir is None and mon2._read_used() == 5 * 2**30 - 0
    )  # no stat file at root here
    assert os.path.basename(mm._in_own_cgroup(mm._CG_V2_MAX, None)) == "memory.max"
