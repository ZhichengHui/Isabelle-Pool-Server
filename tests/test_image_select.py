"""server/app/services/image_select.py: the start image is one that already contains the imported
theories and is up to date (build databases faked in a temporary heaps directory)."""
import os
import sqlite3
import subprocess

import pytest

from server.app.services import image_select as m


def _image(d, name, theories, inputs=(), sources=()):
    (d / name).write_bytes(b"heap")
    con = sqlite3.connect(d / "log" / f"{name}.db")
    con.execute("create table isabelle_exports (session_name, theory_name)")
    con.execute("create table isabelle_session_info (sources, input_heaps)")
    con.executemany("insert into isabelle_exports values (?, ?)", [(name, t) for t in theories])
    con.execute("insert into isabelle_session_info values (?, ?)",
                ("\n".join(f"sha {p}" for p in ("<meta_info>", *sources)),
                 "\n".join(f"sha {p}" for p in inputs)))
    con.commit()
    con.close()


@pytest.fixture
def heaps(tmp_path, monkeypatch):
    user = tmp_path / "user" / "polyml-5.9.2_x86_64_32-linux"
    system = tmp_path / "system" / "polyml-5.9.2_x86_64_32-linux"
    for d in (user, system):
        (d / "log").mkdir(parents=True)
    _image(system, "Pure", ["Pure"])
    _image(system, "HOL", ["Main", "HOL.List"], ["Pure"])
    _image(user, "Lib", ["Lib.A"], ["Pure", "HOL"])
    _image(user, "Big", ["Lib.A", "Big.B", "HOL-Library.Multiset"], ["Pure", "HOL"])
    monkeypatch.setattr(m, "_env", {"ISABELLE_HEAPS": str(tmp_path / "user"),
                                    "ISABELLE_HEAPS_SYSTEM": str(tmp_path / "system"),
                                    "ML_SYSTEM": "polyml-5.9.2"})
    monkeypatch.setattr(m, "_theories", {})
    stale = set()
    monkeypatch.setattr(m, "current", lambda name, imgs, dirs: name not in stale)
    return stale


def test_images_and_inherited_theories(heaps):
    imgs = m.images()
    assert sorted(imgs) == ["Big", "HOL", "Lib", "Pure"]
    assert {"Lib.A", "Main", "Pure"} <= m.theories("Lib", imgs)


def test_keeps_a_current_covering_field(heaps):
    assert m.select_field(["Lib.A"], "Lib") == "Lib"


def test_switches_to_the_smallest_current_covering_image(heaps):
    assert m.select_field(["Lib.A"], "HOL") == "Lib"
    heaps.add("Lib")  # stale: the next covering image
    assert m.select_field(["Lib.A"], "Lib") == "Big"


def _sel(field, from_source=(), stale=(), field_stale=False):
    return {"field": field, "from_source": list(from_source), "stale": list(stale),
            "field_stale": field_stale}


def test_selection_reports_what_loads_from_source_and_the_stale_images(heaps):
    assert m.selection(["Lib.A"], "HOL") == _sel("Lib")
    heaps.update({"Lib", "Big"})  # both images that hold Lib.A are out of date
    assert m.selection(["Lib.A", "Main"], "HOL") == _sel("HOL", ["Lib.A"], ["Lib", "Big"])
    assert m.selection(["Own.T"], "HOL") == _sel("HOL", ["Own.T"])
    assert m.selection(["Main"], "HOL") == _sel("HOL")


def test_selection_when_the_proposed_field_itself_is_out_of_date(heaps):
    heaps.update({"Lib", "Big"})
    # the session would start on Lib, which holds every import but is stale: it cannot start
    assert m.selection(["Lib.A"], "Lib") == _sel("Lib", ["Lib.A"], ["Lib", "Big"], True)


def test_keeps_the_field_when_nothing_covers(heaps):
    assert m.select_field(["Main"], "HOL") == "HOL"  # unqualified: no selection
    assert m.select_field(["Own.T"], "Big") == "Big"  # a theory in no image
    heaps.update({"Lib", "Big"})
    assert m.select_field(["Lib.A"], "Lib") == "Lib"


# ----- the up-to-date cache: kept while the inputs are unchanged, dropped when one changes -----

def _touch(path, text=None):
    """Change `path` (new content when given) and move its mtime forward."""
    if text is not None:
        path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Real `current` on faked images; `isabelle build -n` is counted, not run."""
    dist, home, home_user = tmp_path / "dist", tmp_path / "home", tmp_path / "home_user"
    (dist / "src" / "HOL").mkdir(parents=True)
    (dist / "src" / "HOL" / "ROOT").write_text("session HOL")
    (dist / "src" / "HOL" / "Main.thy").write_text("theory Main")
    (home / "proj" / "sub").mkdir(parents=True)
    (home / "proj" / "ROOT").write_text("session Lib = HOL + theories B")
    (home / "proj" / "sub" / "B.thy").write_text("theory B")
    (home_user / "etc").mkdir(parents=True)
    (home_user / "ROOTS").write_text(str(home / "proj"))
    heaps_dir = tmp_path / "user" / "polyml-5.9.2_x86_64_32-linux"
    (heaps_dir / "log").mkdir(parents=True)
    # inputs as a real database of a root image lists them: a `<meta_info>` line, no heap
    _image(heaps_dir, "HOL", ["Main"], ["<meta_info>"], sources=["~~/src/HOL/Main.thy"])
    _image(heaps_dir, "Lib", ["Lib.B"], ["HOL"], sources=["~/proj/sub/B.thy"])
    monkeypatch.setattr(m, "_env", {
        "ISABELLE_HEAPS": str(tmp_path / "user"), "ISABELLE_HEAPS_SYSTEM": "",
        "ML_SYSTEM": "polyml-5.9.2", "ISABELLE_HOME": str(dist),
        "USER_HOME": str(home), "ISABELLE_HOME_USER": str(home_user)})
    monkeypatch.setattr(m, "_theories", {})
    monkeypatch.setattr(m, "_current", {})
    calls = []
    getenv = {}  # what `isabelle getenv -b VAR` answers

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "getenv":
            return subprocess.CompletedProcess(cmd, 0, stdout=getenv.get(cmd[-1], "") + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(m.subprocess, "run", run)

    def check(name="Lib", dirs=()):
        """Run the check; the number of `isabelle build -n` runs so far."""
        assert m.current(name, m.images(), dirs)
        return sum(1 for c in calls if c[1] == "build")

    return check, {"dist": dist, "home": home, "home_user": home_user, "heaps": heaps_dir,
                   "getenv": getenv, "calls": calls}


def test_unchanged_inputs_reuse_the_result(cache):
    check, _ = cache
    assert check() == 1
    assert check() == 1


@pytest.mark.parametrize("changed", [
    "home/proj/sub/B.thy",        # a source theory of the image
    "home/proj/ROOT",             # its session ROOT (only in the <meta_info> digest)
    "dist/src/HOL/Main.thy",      # a source of an input heap
    "heaps/Lib",                  # the image itself was rebuilt
    "heaps/HOL",                  # an input heap was rebuilt
    "home_user/ROOTS",            # the registered session directories
])
def test_a_changed_input_runs_the_check_again(cache, changed):
    check, d = cache
    assert check() == 1
    root, rel = changed.split("/", 1)
    _touch(d[root] / rel)
    assert check() == 2
    assert check() == 2


def test_an_edit_that_keeps_the_mtime_but_not_the_size_is_seen(cache):
    check, d = cache
    b = d["home"] / "proj" / "sub" / "B.thy"
    assert check() == 1
    st = b.stat()
    b.write_text("theory B begin lemma x: True by simp end")
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert check() == 2


def test_a_deleted_source_runs_the_check_again(cache):
    check, d = cache
    assert check() == 1
    (d["home"] / "proj" / "sub" / "B.thy").unlink()
    assert check() == 2


def test_other_session_dirs_are_cached_separately(cache):
    check, _ = cache
    assert check(dirs=()) == 1
    assert check(dirs=("/x",)) == 2
    assert check(dirs=("/x",)) == 2


def test_no_cache_when_a_source_cannot_be_located(cache):
    check, d = cache
    _image(d["heaps"], "Odd", ["Odd.T"], ["HOL"], sources=["$UNKNOWN_VAR/T.thy"])
    assert check("Odd") == 1
    assert check("Odd") == 2


def test_no_cache_without_a_build_database(cache):
    check, d = cache
    (d["heaps"] / "Bare").write_bytes(b"heap")
    assert check("Bare") == 1
    assert check("Bare") == 2


def test_a_root_of_another_session_nearer_to_a_source_does_not_hide_the_own_root(cache):
    check, d = cache
    (d["home"] / "proj" / "sub" / "ROOT").write_text("session Other = HOL")
    assert check() == 1
    _touch(d["home"] / "proj" / "ROOT", "session Lib = HOL (options) + theories B")
    assert check() == 2


def test_a_root_that_appears_runs_the_check_again(cache):
    check, d = cache
    assert check() == 1
    (d["home"] / "proj" / "sub" / "ROOT").write_text("session Other = HOL")
    assert check() == 2


def test_the_root_of_a_session_directory_is_part_of_the_key(cache, tmp_path):
    check, _ = cache
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "ROOT").write_text("session X = HOL")
    assert check(dirs=(str(extra),)) == 1
    assert check(dirs=(str(extra),)) == 1
    _touch(extra / "ROOT", "session X = HOL + theories Y")
    assert check(dirs=(str(extra),)) == 2


def test_other_path_variables_are_resolved_with_getenv_and_cached(cache, tmp_path):
    check, d = cache
    afp = tmp_path / "afp"
    (afp / "Word_Lib").mkdir(parents=True)
    (afp / "Word_Lib" / "W.thy").write_text("theory W")
    d["getenv"]["AFP"] = str(afp)
    _image(d["heaps"], "Afp", ["Afp.W"], ["HOL"], sources=["$AFP/Word_Lib/W.thy"])
    assert check("Afp") == 1
    assert check("Afp") == 1  # cached: the source was located
    assert sum(1 for c in d["calls"] if c[1] == "getenv") == 1  # asked once
    _touch(afp / "Word_Lib" / "W.thy", "theory W begin end")
    assert check("Afp") == 2
