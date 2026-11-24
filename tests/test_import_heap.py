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


def test_any_order_of_the_same_theories_gives_one_name_and_one_root(state):
    """Opening a file and starting a scratch session pass the same theories in different orders;
    a different ROOT text under the same name made `isabelle build` rebuild the heap each time."""
    import itertools
    proj = _project(state)
    (proj / "lib2").mkdir()
    (proj / "lib2" / "ROOT").write_text("session S_ih2 = HOL +\n  theories U\n")
    dirs = [str(proj / "lib"), str(proj / "lib2")]
    theories = [str(proj / "B"), str(proj / "C"), "S_ih.T", "S_ih2.U", "HOL-Library.Multiset"]
    plans = [ih.plan(list(order), "HOL", d)
             for order in itertools.permutations(theories)
             for d in (dirs, dirs[::-1])]
    assert len({(p.name, p.root_text, p.root_dir) for p in plans}) == 1
    # a theory named twice is one theory
    twice = ih.plan(theories + [str(proj / "B")], "HOL", dirs)
    assert (twice.name, twice.root_text) == (plans[0].name, plans[0].root_text)
    # the wrapper imports in the request's order (the later import wins a shared short name)
    back = ih.plan(theories[::-1], "HOL", dirs)
    assert back.theories == [f"{back.name}.C", f"{back.name}.B",
                             "HOL-Library.Multiset", "S_ih2.U", "S_ih.T"]


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
# A test that leaves a build running ends it with BUILDER.cancel_all(), as the server's shutdown
# does (main.py), before its event loop ends. Leaving it to asyncio.run hangs (Python 3.12):
# asyncio.run cancels every task at once, also asyncio's own task that finishes starting the
# subprocess (_connect_pipes), so the build task, cancelled inside create_subprocess_exec, waits
# forever for an exit notification that stays queued behind that never-run task.

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


def test_requests_at_the_same_moment_start_one_build(state, monkeypatch):
    proj = _project(state)
    p = ih.plan([str(proj / "B")], "HOL", [])
    runs = state / "runs"
    script = _fake_isabelle(state, state / "nofail")
    wrapper = state / "counting-isabelle"
    wrapper.write_text(f'#!/bin/bash\necho x >> "{runs}"\nexec "{script}" "$@"\n')
    wrapper.chmod(0o755)
    ih.BUILDER.isabelle = str(wrapper)

    def slow_images():  # both requests are inside the up-to-date check at once
        import time as _t
        _t.sleep(0.2)
        return {}

    monkeypatch.setattr(image_select, "images", slow_images)

    async def run():
        a, b = await asyncio.gather(ih.BUILDER.ensure(p), ih.BUILDER.ensure(p))
        assert a["status"] == b["status"] == "building"
        await ih.BUILDER._jobs[p.name].task
        await asyncio.sleep(1.5)  # a second, queued build (the bug) would have run by now

    asyncio.run(run())
    assert runs.read_text().count("x") == 1


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
        await ih.BUILDER.cancel_all()

    asyncio.run(run())


def test_theories_that_run_code_are_not_built(state, monkeypatch):
    from server.app.core.config import Server

    proj = _project(state)
    (proj / "B.thy").write_text("theory B imports Main begin\nML \\<open>1\\<close>\nend")
    p = ih.plan([str(proj / "B")], "HOL", [])
    ih.BUILDER.isabelle = _fake_isabelle(state, state / "nofail")
    monkeypatch.setattr(image_select, "images", lambda: {})
    monkeypatch.setattr(image_select, "current", lambda name, imgs, dirs: False)
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    out = asyncio.run(ih.BUILDER.ensure(p))
    assert out["status"] == "failed" and "B.thy" in out["log_tail"] and "'ML'" in out["log_tail"]
    assert p.name not in ih.BUILDER._jobs  # nothing was run
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", True)

    async def allowed():
        out = await ih.BUILDER.ensure(p)
        await ih.BUILDER.cancel_all()
        return out

    assert asyncio.run(allowed())["status"] == "building"


def test_shutdown_right_after_a_build_starts_ends_it(state, monkeypatch):
    """cancel_all (server shutdown) in the same step as the build's start: it returns at once
    and no build process is left (the build may be cancelled while its process is starting)."""
    proj = _project(state)
    p = ih.plan([str(proj / "B")], "HOL", [])
    pids = state / "pids"
    script = state / "slow-isabelle"
    script.write_text(f'#!/bin/bash\necho $$ >> "{pids}"\nsleep 60\n')
    script.chmod(0o755)
    ih.BUILDER.isabelle = str(script)
    monkeypatch.setattr(image_select, "images", lambda: {})
    monkeypatch.setattr(image_select, "current", lambda name, imgs, dirs: False)

    async def run(delay):
        assert (await ih.BUILDER.ensure(p))["status"] == "building"
        await asyncio.sleep(delay)
        await asyncio.wait_for(ih.BUILDER.cancel_all(), 30)
        assert ih.BUILDER._jobs[p.name].finished_at is not None

    for delay in (0, 0.5):  # while the process starts; while it runs
        ih.BUILDER._jobs.clear()
        asyncio.run(run(delay))
    for pid in (pids.read_text().split() if pids.exists() else []):  # = the build's group id
        with pytest.raises(ProcessLookupError):
            os.killpg(int(pid), 0)


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
