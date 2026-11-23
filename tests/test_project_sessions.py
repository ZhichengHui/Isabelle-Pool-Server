"""project_sessions: session-qualified imports of a file's own project."""
import os

from mcp_servers.lsp import project_sessions as ps


def _project(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for d, text in {
        "urust": 'session "Sat" = "AutoCorrode" +\n  theories Sat_Funs\n',
        "spec": 'session "Sat_Spec" = "Sat" +\n  theories Sat_Spec\n',
        "proof": 'session "Sat_Proof" = "Sat_Spec" +\n  theories Sat_Proof\n\n'
                 'session "Sat_Top" in "top" = "Sat_Proof" +\n  theories Sat_Top\n',
        "dev": 'session "Ignored" = "HOL" +\n',
        "target/x": 'session "AlsoIgnored" = "HOL" +\n',
    }.items():
        (root / d).mkdir(parents=True)
        (root / d / "ROOT").write_text(text)
    (root / "proof" / "top").mkdir()
    thy = root / "proof" / "top" / "Sat_Top.thy"
    thy.write_text('theory Sat_Top imports Sat_Proof.Sat_Proof begin end\n')
    return root, thy


def test_root_dirs_and_parents(tmp_path):
    root, thy = _project(tmp_path)
    assert ps.find_project_root(str(thy)) == str(root)
    dirs = ps.project_session_dirs(str(root))
    assert dirs == sorted(str(root / d) for d in ("proof", "spec", "urust"))
    parents = ps.session_parents(dirs)
    assert parents == {"Sat": "AutoCorrode", "Sat_Spec": "Sat", "Sat_Proof": "Sat_Spec",
                       "Sat_Top": "Sat_Proof"}


def test_context_for_project_import(tmp_path):
    root, thy = _project(tmp_path)
    field, dirs = ps.project_context(["Sat_Proof.Sat_Proof"], str(thy))
    assert field == "AutoCorrode"
    assert dirs == tuple(sorted(str(root / d) for d in ("proof", "spec", "urust")))


def test_context_without_project_imports(tmp_path):
    _, thy = _project(tmp_path)
    assert ps.project_context(["Main", "HOL-Library.Word"], str(thy)) == (None, ())


def test_context_outside_a_repository(tmp_path):
    thy = tmp_path / "A.thy"
    thy.write_text("theory A imports Main begin end\n")
    assert ps.project_context(["Sat_Proof.Sat_Proof"], str(thy)) == (None, ())


def test_cycle_gives_no_field():
    assert ps.outside_ancestor("A", {"A": "B", "B": "A"}) is None


def test_registered_sessions_keep_old_behaviour(tmp_path, monkeypatch):
    root, thy = _project(tmp_path)
    home = tmp_path / "home"
    (home / ".isabelle" / "Isabelle2026").mkdir(parents=True)
    (home / ".isabelle" / "Isabelle2026" / "ROOTS").write_text(str(root / "urust") + "\n")
    monkeypatch.setenv("HOME", str(home))
    assert ps.registered_sessions() == {"Sat"}
    # Sat_Proof is not registered: still handled; an import of the registered Sat alone is not.
    assert ps.project_context(["Sat_Proof.Sat_Proof"], str(thy))[0] == "AutoCorrode"
    assert ps.project_context(["Sat.Sat_Funs"], str(thy)) == (None, ())


def test_file_context_asks_the_server_for_the_start_image(tmp_path):
    import asyncio

    from mcp_servers.lsp.header_context import file_context

    class Client:
        def __init__(self, answer):
            self.answer = answer

        async def parse_theory_header(self, text):
            return {"imports": ["Lib.A"], "suggested_field": "Lib"}

        async def select_image(self, imports, field, dirs):
            if self.answer is None:
                raise RuntimeError("404")  # an older server
            return {"field": self.answer}

    f = str(tmp_path / "T.thy")
    assert asyncio.run(file_context(Client("Big"), "", f))[1] == "Big"
    assert asyncio.run(file_context(Client(None), "", f))[1] in ("Lib", "HOL")
    assert asyncio.run(file_context(Client("Big"), "", f, heap_session="H"))[1] != "Big"
