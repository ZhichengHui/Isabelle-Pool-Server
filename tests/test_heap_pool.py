"""Unit tests for the heap pool (Stage 3) — no Isabelle backend needed.

The `isabelle build` subprocess is faked by monkeypatching
``asyncio.create_subprocess_exec``; state dirs are tmp_path.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from server.app.api.v1.schemas.API_models import (
    HeapBuildRequest,
    SessionAcquireRequest,
    SessionCreateRequest,
)
from server.app.services.heap_pool import (
    HeapBuildFailed,
    HeapBuildInProgress,
    HeapCrossGroup,
    HeapNotFound,
    HeapNotReady,
    HeapPool,
)


class _FakeProc:
    """`isabelle build` as build_process.start runs it: output goes to the log file it is given."""

    def __init__(self, returncode=0, delay=0.0):
        self.returncode = None
        self._rc = returncode
        self._delay = delay
        self.pid = -1

    async def wait(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        self.returncode = self._rc
        return self._rc


def _fake_exec(monkeypatch, returncode=0, delay=0.0, out=b"Build OK\n"):
    async def fake(*args, **kwargs):
        kwargs["stdout"].write(out)
        return _FakeProc(returncode=returncode, delay=delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def _project(tmp_path, name="proj"):
    proj = tmp_path / name
    proj.mkdir()
    (proj / "Bar.thy").write_text(
        "theory Bar imports Main begin\nlemma bar_lemma: True by simp\nend\n"
    )
    (proj / "Baz.thy").write_text(
        "theory Baz imports Bar begin\nlemma baz_lemma: True by (simp add: bar_lemma)\nend\n"
    )
    return proj


def _pool(tmp_path, monkeypatch, **fake_kwargs):
    _fake_exec(monkeypatch, **fake_kwargs)
    return HeapPool(state_dir=str(tmp_path / "heap_state"), allowed_roots=[str(tmp_path)])


# ------------------------------------------- list_available_heaps (admin)


def _fake_homes(tmp_path):
    """A user home with a HOL-Analysis image and a distribution home with HOL."""
    user_heaps = tmp_path / "user" / "heaps" / "polyml_x86_64"
    user_heaps.mkdir(parents=True)
    (user_heaps / "HOL-Analysis").write_bytes(b"\x00" * 1024)
    (user_heaps / "log").mkdir()  # log dir must be skipped
    dist_heaps = tmp_path / "dist" / "heaps" / "polyml_x86_64"
    dist_heaps.mkdir(parents=True)
    (dist_heaps / "HOL").write_bytes(b"\x00" * (2 * 1024 * 1024))
    return tmp_path / "user", tmp_path / "dist"


def test_isabelle_binary_follows_isabelle_home(tmp_path, monkeypatch):
    """Native installs put Isabelle anywhere; the heap pool must resolve the
    launcher from ISABELLE_HOME exactly like the gateway does, with the
    container path as the fallback (2026-10-05, native-install README)."""
    monkeypatch.setenv("ISABELLE_HOME", "/srv/Isabelle2026")
    assert HeapPool(state_dir=str(tmp_path)).isabelle == "/srv/Isabelle2026/bin/isabelle"
    monkeypatch.delenv("ISABELLE_HOME")
    assert HeapPool(state_dir=str(tmp_path)).isabelle == "/opt/isabelle/bin/isabelle"
    assert HeapPool(state_dir=str(tmp_path), isabelle="/x/isabelle").isabelle == "/x/isabelle"


def test_heap_env_prefers_new_name_and_falls_back_to_old(monkeypatch):
    """ISABELLE_HEAP_* → ISABELLE_HEAP_POOL_* (2026-10-05, so nothing shadows Isabelle's
    own ISABELLE_HEAPS family); the old name keeps working for one release."""
    import warnings
    from server.app.core.config import _heap_env

    monkeypatch.delenv("ISABELLE_HEAP_POOL_GC_IMAGES", raising=False)
    monkeypatch.delenv("ISABELLE_HEAP_GC_IMAGES", raising=False)
    assert _heap_env("GC_IMAGES", "dflt") == "dflt"
    monkeypatch.setenv("ISABELLE_HEAP_GC_IMAGES", "old")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert _heap_env("GC_IMAGES", "dflt") == "old"
    assert any(issubclass(x.category, DeprecationWarning) for x in w)
    monkeypatch.setenv("ISABELLE_HEAP_POOL_GC_IMAGES", "new")
    assert _heap_env("GC_IMAGES", "dflt") == "new"


def test_available_heaps_lists_base_images(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    user, dist = _fake_homes(tmp_path)
    pool._home_user, pool._home = user, dist
    found = pool.list_available_heaps()
    by_session = {h["session"]: h for h in found}
    assert set(by_session) == {"HOL-Analysis", "HOL"}
    assert by_session["HOL-Analysis"]["origin"] == "user"
    assert by_session["HOL"]["origin"] == "distribution"
    assert by_session["HOL"]["size_mb"] > 0
    assert all(h["platform"] == "polyml_x86_64" for h in found)


def test_available_heaps_tags_pool_images(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    user, dist = _fake_homes(tmp_path)
    (user / "heaps" / "polyml_x86_64" / "Hp1").write_bytes(b"\x00" * 512)
    pool._home_user, pool._home = user, dist
    by_session = {h["session"]: h for h in pool.list_available_heaps()}
    assert by_session["Hp1"]["origin"] == "pool"
    assert by_session["HOL-Analysis"]["origin"] == "user"


def test_available_heaps_missing_homes(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    pool._home_user, pool._home = False, False  # getenv failed
    assert pool.list_available_heaps() == []


# ------------------------------------------------- delete_heap_image (admin)


def test_delete_heap_image_removes_file_and_logs(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    user, dist = _fake_homes(tmp_path)
    logdir = user / "heaps" / "polyml_x86_64" / "log"
    (logdir / "HOL-Analysis.gz").write_bytes(b"log")
    pool._home_user, pool._home = user, dist
    out = pool.delete_heap_image("HOL-Analysis")
    assert out["deleted"] == "HOL-Analysis"
    assert out["platform"] == "polyml_x86_64"
    assert out["freed_mb"] >= 0
    assert not (user / "heaps" / "polyml_x86_64" / "HOL-Analysis").exists()
    assert not (logdir / "HOL-Analysis.gz").exists()
    # distribution image untouched
    assert (dist / "heaps" / "polyml_x86_64" / "HOL").exists()


def test_delete_heap_image_never_touches_distribution(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    user, dist = _fake_homes(tmp_path)
    pool._home_user, pool._home = user, dist
    # HOL exists only in the distribution home: not deletable through this path
    with pytest.raises(HeapNotFound):
        pool.delete_heap_image("HOL")
    assert (dist / "heaps" / "polyml_x86_64" / "HOL").exists()


def test_delete_heap_image_missing_raises(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    user, dist = _fake_homes(tmp_path)
    pool._home_user, pool._home = user, dist
    with pytest.raises(HeapNotFound):
        pool.delete_heap_image("NoSuchSession")


def test_build_ready_and_manifest_roundtrip(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    entry = asyncio.run(pool.build("alpha", str(proj), session_name="Hp1", built_by="alpha"))
    assert entry["status"] == "ready"
    assert entry["session_name"] == "Hp1"
    assert entry["built_at"] is not None
    # scratch ROOT generated (no user ROOT in the project)
    assert "_roots" in entry["root_dir"]
    assert 'session Hp1 = HOL +' in entry["root_text"]
    assert 'directories "' in entry["root_text"]

    # registry rebuilt from the persisted manifest
    pool2 = HeapPool(state_dir=str(tmp_path / "heap_state"))
    loaded = pool2.get("alpha", str(proj))
    assert loaded is not None
    assert loaded["status"] == "ready"
    assert loaded["fingerprint"] == entry["fingerprint"]
    files = {f["path"].replace("\\", "/").split("/")[-1]: f for f in loaded["theory_files"]}
    assert set(files) == {"Bar.thy", "Baz.thy"}
    assert all(len(f["sha256"]) == 64 and f["mtime"] > 0 for f in files.values())


def test_user_root_preferred(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    (proj / "ROOT").write_text("session MyProj = HOL +\n  theories\n    Bar\n    Baz\n")
    entry = asyncio.run(pool.build("alpha", str(proj)))
    assert entry["root_dir"] == str(proj)  # user ROOT wins
    assert entry["session_name"] == "MyProj"  # parsed from ROOT


def test_staleness_gate(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    entry = pool.resolve_for_session("alpha", heap_session="Hp1")
    assert entry["status"] == "ready"

    (proj / "Bar.thy").write_text("theory Bar imports Main begin\nlemma bar_lemma: True by simp\nlemma b2: True by simp\nend\n")
    with pytest.raises(HeapNotReady) as exc_info:
        pool.resolve_for_session("alpha", heap_session="Hp1")
    assert "stale" in str(exc_info.value)
    assert exc_info.value.status_code == 422
    assert pool.get("alpha", str(proj))["status"] == "stale"


def test_build_lock_per_key(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch, delay=0.3)
    proj = _project(tmp_path)

    async def run_two():
        first = asyncio.create_task(pool.build("alpha", str(proj), session_name="Hp1"))
        await asyncio.sleep(0.05)  # let the first build take the lock
        with pytest.raises(HeapBuildInProgress) as exc_info:
            await pool.build("alpha", str(proj), session_name="Hp1")
        assert exc_info.value.status_code == 409
        return await first

    entry = asyncio.run(run_two())
    assert entry["status"] == "ready"


def test_group_isolation(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))

    entry = pool.resolve_for_session("alpha", heap_session="Hp1")
    assert entry["session_name"] == "Hp1"

    with pytest.raises(HeapCrossGroup) as exc_info:
        pool.resolve_for_session("beta", heap_session="Hp1")
    assert exc_info.value.status_code == 403

    with pytest.raises(HeapNotFound) as exc_info:
        pool.resolve_for_session("beta", heap_session="Nope")
    assert exc_info.value.status_code == 404


def test_failed_build_recorded(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch, returncode=1)
    proj = _project(tmp_path)
    with pytest.raises(HeapBuildFailed):
        asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    entry = pool.get("alpha", str(proj))
    assert entry["status"] == "failed"
    with pytest.raises(HeapNotReady):
        pool.resolve_for_session("alpha", heap_session="Hp1")


def test_interrupted_build_comes_back_failed(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    # simulate a crash mid-build: rewrite the manifest as "building"
    path = pool._manifest_path("alpha", str(proj))
    data = json.loads(path.read_text())
    data["status"] = "building"
    path.write_text(json.dumps(data))
    pool2 = HeapPool(state_dir=str(tmp_path / "heap_state"))
    assert pool2.get("alpha", str(proj))["status"] == "failed"


def test_delete_heap_and_group(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    assert pool.delete("alpha", str(proj)) is True
    assert pool.get("alpha", str(proj)) is None
    assert pool.delete("alpha", str(proj)) is False

    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    assert pool.groups()[0]["task_group"] == "alpha"
    assert pool.delete_group("alpha") == 1
    assert pool.groups() == []


def test_schema_defaults_and_validation():
    req = SessionCreateRequest()
    assert req.task_group is None and req.heap_session is None and req.project is None
    acq = SessionAcquireRequest()
    assert acq.task_group is None and acq.heap_session is None
    # a project under the first allowed root (this checkout by default; /app in the container)
    import os
    from server.app.core.config import Heap
    project = os.path.join(Heap.ALLOWED_ROOTS[0], "x")
    with pytest.raises(ValidationError):
        HeapBuildRequest(project=project)  # task_group is required
    ok = HeapBuildRequest(task_group="alpha", project=project)
    assert ok.session_name is None


def test_rebuild_without_session_name_keeps_existing_name(tmp_path, monkeypatch):
    """Regression: rebuilding without session_name must not re-derive the name
    from the project dir (that renamed the heap and orphaned sessions)."""
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    entry = asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    assert entry["session_name"] == "Hp1"
    # change a source so the rebuild isn't a no-op fingerprint-wise
    (proj / "Bar.thy").write_text(
        "theory Bar imports Main begin\nlemma bar_lemma2: True by simp\nend\n"
    )
    entry2 = asyncio.run(pool.build("alpha", str(proj)))
    assert entry2["session_name"] == "Hp1"
    assert entry2["status"] == "ready"


def _fake_home(tmp_path, session_names):
    """Fake ISABELLE_HOME_USER with heap images + logs for the given sessions.
    Poly/ML heap images are single FILES at heaps/<platform>/<session>."""
    home = tmp_path / "home_user"
    for name in session_names:
        img = home / "heaps" / "polyml-test_platform" / name
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_text("heap")
        log = home / "heaps" / "polyml-test_platform" / "log"
        log.mkdir(parents=True, exist_ok=True)
        (log / f"{name}.db").write_text("log")
    return home


def test_delete_gcs_heap_image(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    home = _fake_home(tmp_path, ["Hp1"])
    monkeypatch.setattr(pool, "_home_user", home)
    assert pool.delete("alpha", str(proj))
    assert not (home / "heaps" / "polyml-test_platform" / "Hp1").exists()
    assert not (home / "heaps" / "polyml-test_platform" / "log" / "Hp1.db").exists()


def test_delete_keeps_image_while_other_entry_references_it(tmp_path, monkeypatch):
    pool = _pool(tmp_path, monkeypatch)
    proj = _project(tmp_path)
    proj2 = _project(tmp_path, "proj2")
    asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    asyncio.run(pool.build("beta", str(proj2), session_name="Hp1"))  # same name, other group
    home = _fake_home(tmp_path, ["Hp1"])
    monkeypatch.setattr(pool, "_home_user", home)
    pool.delete("alpha", str(proj))
    # beta still references the session name — image must survive
    assert (home / "heaps" / "polyml-test_platform" / "Hp1").exists()
    pool.delete("beta", str(proj2))
    # last reference gone — GC'd
    assert not (home / "heaps" / "polyml-test_platform" / "Hp1").exists()
