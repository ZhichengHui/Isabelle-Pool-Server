"""import_heap: which image a session needs built first, and its build in the background (a fake
`isabelle` script stands in for `isabelle build -b -v`)."""
import asyncio
import os
import stat

import pytest

from server.app.core.config import Heap
from server.app.services import image_select, import_heap as ih


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(Heap, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Heap, "MAX_CONCURRENT_BUILDS", 1)
    monkeypatch.setattr(ih, "BUILDER", ih.Builder())
    return tmp_path


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "lib").mkdir(parents=True)
    (proj / "lib" / "ROOT").write_text("session S_ih = HOL +\n  theories T\n")
    (proj / "B.thy").write_text("theory B imports Main begin end")
    (proj / "C.thy").write_text("theory C imports Main begin end")
    return proj


def test_siblings_get_a_generated_session_on_the_field(state, monkeypatch):
    proj = _project(state)
    p = ih.plan([str(proj / "B"), str(proj / "C"), "HOL-Library.Multiset"], "HOL", [])
    assert p.name.startswith("IPS_") and len(p.name) == 16
    assert p.root_text == (f'session {p.name} in "{proj}" = "HOL" +\n  theories\n    B\n    C\n')
    assert p.dirs[0] == p.root_dir and p.root_dir.endswith("_imports/" + p.name)
    assert p.theories == [f"{p.name}.B", f"{p.name}.C", "HOL-Library.Multiset"]
    assert p.watch == sorted([str(proj / "B.thy"), str(proj / "C.thy")])
    # the same inputs give the same name: one build for every request that needs it
    assert ih.plan([str(proj / "C"), str(proj / "B"), "HOL-Library.Multiset"], "HOL", []).name == p.name


def test_siblings_and_a_project_session_build_on_that_session(state):
    proj = _project(state)
    lib = str(proj / "lib")
    p = ih.plan([str(proj / "B"), "S_ih.T"], "HOL", [lib])
    assert f'= "S_ih" +' in p.root_text and "sessions" not in p.root_text
    assert p.dirs == [p.root_dir, lib] and p.theories == [f"{p.name}.B", "S_ih.T"]


def test_a_project_session_alone_is_built_itself(state):
    proj = _project(state)
    p = ih.plan(["S_ih.T"], "HOL", [str(proj / "lib")])
    assert (p.name, p.root_dir, p.theories) == ("S_ih", None, ["S_ih.T"])


def test_an_out_of_date_field_is_rebuilt_otherwise_nothing(state, monkeypatch):
    monkeypatch.setattr(image_select, "images", lambda: {})
    monkeypatch.setattr(image_select, "current", lambda name, imgs, dirs: name != "Stale")
    assert ih.plan(["Stale.X"], "Stale", []).name == "Stale"
    assert ih.plan(["HOL-Library.Multiset"], "HOL", []) is None
    assert ih.plan([], "Stale", []) is None  # a plain session: nothing to build for it


def test_siblings_in_two_directories_are_left_as_they_are(state):
    proj = _project(state)
    (state / "other").mkdir()
    (state / "other" / "D.thy").write_text("theory D imports Main begin end")
    assert ih.plan([str(proj / "B"), str(state / "other" / "D")], "HOL", []) is None


# ----- the builder -----

def _fake_isabelle(tmp_path, fail_marker):
    """`isabelle build -b -v -d … NAME`: prints like the real one, fails while `fail_marker`
    exists, else creates the image NAME in tmp_path/heaps."""
    script = tmp_path / "isabelle"
    script.write_text(f"""#!/bin/bash
name="${{@: -1}}"
echo "Building $name ..."
sleep 0.3
if [ -e "{fail_marker}" ]; then echo "*** Undefined fact: b2"; echo "FAILED $name"; exit 1; fi
mkdir -p "{tmp_path}/heaps"; touch "{tmp_path}/heaps/$name"
echo "Finished $name (0:00:01 elapsed time)"
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_build_then_ready(state, monkeypatch):
    proj = _project(state)
    p = ih.plan([str(proj / "B")], "HOL", [])
    ih.BUILDER.isabelle = _fake_isabelle(state, state / "nofail")
    heaps = state / "heaps"
    monkeypatch.setattr(image_select, "images",
                        lambda: {f.name: f for f in heaps.iterdir()} if heaps.is_dir() else {})
    monkeypatch.setattr(image_select, "current", lambda name, imgs, dirs: name in imgs)

    async def run():
        first = await ih.BUILDER.ensure(p)
        assert first["status"] == "building" and first["session"] == p.name
        again = await ih.BUILDER.ensure(p)  # a second request joins the same build
        assert again["status"] == "building"
        await ih.BUILDER._jobs[p.name].task
        done = await ih.BUILDER.ensure(p)
        assert done == {"status": "ready", "session": p.name}
        assert (state / "state" / "_imports" / p.name / "ROOT").read_text() == p.root_text

    asyncio.run(run())


def test_a_failure_is_reported_until_a_source_changes(state, monkeypatch):
    proj = _project(state)
    p = ih.plan([str(proj / "B")], "HOL", [])
    marker = state / "fail"
    marker.touch()
    ih.BUILDER.isabelle = _fake_isabelle(state, marker)
    monkeypatch.setattr(image_select, "images", lambda: {})
    monkeypatch.setattr(image_select, "current", lambda name, imgs, dirs: False)

    async def run():
        await ih.BUILDER.ensure(p)
        await ih.BUILDER._jobs[p.name].task
        failed = await ih.BUILDER.ensure(p)
        assert failed["status"] == "failed" and "Undefined fact: b2" in failed["log_tail"]
        assert (await ih.BUILDER.ensure(p))["status"] == "failed"  # no rebuild loop
        st = os.stat(proj / "B.thy")
        os.utime(proj / "B.thy", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # the fix
        assert (await ih.BUILDER.ensure(p))["status"] == "building"

    asyncio.run(run())


def test_prepare_answers_building_then_the_built_inputs(state, monkeypatch):
    proj = _project(state)
    calls = []

    async def ensure(p):
        calls.append(p.name)
        return {"status": "building", "session": p.name} if len(calls) == 1 else {
            "status": "ready", "session": p.name}

    monkeypatch.setattr(ih.BUILDER, "ensure", ensure)

    async def run():
        with pytest.raises(ih.ImportHeapBuilding):
            await ih.prepare([str(proj / "B")], "HOL", None, None)
        theories, field, dirs, extra = await ih.prepare([str(proj / "B")], "HOL", None, "x")
        assert field == calls[0] and theories == [f"{field}.B"]
        assert dirs[0].endswith(field) and extra == f"x|img:{field}"
        # nothing to build: unchanged
        monkeypatch.setattr(image_select, "current", lambda *a: True)
        monkeypatch.setattr(image_select, "images", lambda: {})
        assert await ih.prepare(["HOL-Library.Multiset"], "HOL", None, None) == (
            ["HOL-Library.Multiset"], "HOL", None, None)

    asyncio.run(run())


def test_unused_generated_heaps_are_removed(state, monkeypatch):
    base = state / "state" / "_imports"
    (base / "IPS_old").mkdir(parents=True)
    (base / "IPS_old" / "ROOT").write_text("x")
    (base / "IPS_new").mkdir()
    (base / "IPS_new" / "last_used").touch()
    old = base / "IPS_old" / "last_used"
    old.touch()
    os.utime(old, (0, 0))
    heaps = state / "heaps"
    (heaps / "log").mkdir(parents=True)
    (heaps / "IPS_old").touch()
    (heaps / "log" / "IPS_old.db").touch()
    monkeypatch.setattr(image_select, "images", lambda: {"IPS_old": heaps / "IPS_old"})
    assert ih.gc_generated() == ["IPS_old"]
    assert not (heaps / "IPS_old").exists() and not (heaps / "log" / "IPS_old.db").exists()
    assert (base / "IPS_new").is_dir() and not (base / "IPS_old").exists()
