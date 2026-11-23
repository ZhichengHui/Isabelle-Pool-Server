"""Security input guards (FINDINGS 2026-09-21 SEC-2 / SEC-3), no Isabelle needed.

SEC-2: client Isar text reaching the prover (commands, verify_chunk,
PUT /document, bigstep, heap project sources) must reject code-executing
commands unless ISABELLE_ALLOW_ML_COMMANDS=true.
SEC-3: heap `task_group` / `session_name` / image `session` / `platform` are
path-segment-safe; `project` must live under Heap.ALLOWED_ROOTS; manifest and
image paths are containment-checked; destructive heap endpoints need the admin
token.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from server.app.api.v1.schemas.API_models import (
    BigStepTheoryRequest,
    ChunkVerifyRequest,
    CommandRequest,
    DocumentLoadRequest,
    EnterTheoryRequest,
    HeapBuildRequest,
    SessionAcquireRequest,
    SessionCreateRequest,
)
from server.app.api.v1.schemas.fill_models import FillSorriesRequest
from server.app.core import input_guards
from server.app.core.config import Heap, Server
from server.app.core.input_guards import (
    command_skeleton,
    find_code_execution,
    reject_code_execution,
    validate_import_name,
    validate_project_path,
    validate_safe_name,
)
from server.app.services.heap_pool import HeapPool, HeapRejected


# =========================================================== SEC-2: ML guard

GOOD_PROOF = '''
theory Scratch imports Main "HOL-Library.Multiset" begin
(* a comment that mentions ML and setup, harmless *)
lemma foo: "rev (rev xs) = xs"
proof (induct xs)
  case Nil then show ?case by simp
next
  case (Cons a xs)
  have "setup = setup" by simp   (* the word setup inside a string literal, not a command *)
  then show ?case using Cons by (simp add: \\<open>ML\\<close>_def)
qed
text \\<open>ML ‹OS.Process.system "rm -rf /"› inside a text cartouche is not a command\\<close>
definition oracle_name :: "string" where "oracle_name = ''ML''"
end
'''


@pytest.mark.parametrize("text,kw", [
    ('ML ‹OS.Process.system "id"›', "ML"),
    ('ML\\<open>writeln "x"\\<close>', "ML"),
    ('lemma x: True\n  ML_val ‹1›\n  by simp', "ML_val"),
    ('ML_file "evil.ML"', "ML_file"),
    ('ML_file_debug "evil.ML"', "ML_file_debug"),
    ('setup ‹fn thy => thy›', "setup"),
    ('local_setup ‹I›', "local_setup"),
    ('method_setup m = ‹Scan.succeed (K no_tac)›', "method_setup"),
    ('simproc_setup s ("x") = ‹K (K NONE)›', "simproc_setup"),
    ('oracle bad = ‹fn _ => @{cprop True}›', "oracle"),
    ('declaration ‹K I›', "declaration"),
    ('external_file "x"', "external_file"),
    ('compile_generated_files _ (in Foo) where ‹fn _ => ()›', "compile_generated_files"),
    ('SML_file "x.sml"', "SML_file"),
    ('theory T imports Main begin\nML ‹1›\nend', "ML"),
    # keyword right after a string literal / at end of text
    ('lemma "x = x" by simp ML', "ML"),
    # an axiom postulated inside a candidate proof is "proved" everywhere
    # (report review 2026-10-27)
    ('axiomatization where ax: "False"\nlemma x: "P" by (simp add: ax)',
     "axiomatization"),
])
def test_find_code_execution_detects_commands(text, kw):
    assert find_code_execution(text) == kw


@pytest.mark.parametrize("text", [
    GOOD_PROOF,
    'lemma "ML = ML" by simp',                       # inside a string literal
    '(* ML ‹x› commented out *) lemma True by simp',   # comment
    '(* outer (* nested ML *) still comment *) lemma True by simp',
    'text ‹setup and ML_file are words in prose›',      # cartouche body
    'thm print_ML_antiquotations',                       # underscore-joined identifier
    'lemma MLfoo: True by simp',                         # not a standalone token
    'lemma x: "\\<forall>x. setup_ok x" by (simp add: setup_ok_def)',
    'find_theorems name: "ML"',                          # quoted
])
def test_find_code_execution_ignores_literals_and_comments(text):
    assert find_code_execution(text) is None


def test_command_skeleton_preserves_newlines_and_delimiters():
    text = 'lemma "a\nb" ML ‹x\ny› (* c\nd *)'
    sk = command_skeleton(text)
    assert sk.count("\n") == text.count("\n")
    # literal bodies are blanked (newlines kept), delimiters and the keyword survive
    assert '"\n"' in sk and "‹\n›" in sk and "ML" in sk
    assert "a" not in sk.replace("lemma", "") and "x" not in sk and "c" not in sk


def test_unterminated_literal_blanks_to_end():
    # Nothing after an unterminated quote can smuggle a command past the guard.
    assert find_code_execution('lemma "oops ML ‹x›') is None
    assert find_code_execution('ML ‹unterminated') == "ML"


def test_reject_code_execution_policy_switch(monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    with pytest.raises(ValueError, match="ML"):
        reject_code_execution("ML ‹1›")
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", True)
    assert reject_code_execution("ML ‹1›") == "ML ‹1›"


# ---------------------------------------------------- request models → 422

@pytest.fixture(autouse=True)
def _ml_forbidden(monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    yield


def test_command_request_rejects_ml():
    with pytest.raises(ValidationError, match="code-executing"):
        CommandRequest(command='ML ‹OS.Process.system "id"›')
    assert CommandRequest(command='lemma True by simp').command


def test_chunk_request_rejects_ml_but_keeps_empty_check():
    with pytest.raises(ValidationError, match="code-executing"):
        ChunkVerifyRequest(chunk='have "1 = 1" by simp\nML_val ‹1›')
    with pytest.raises(ValidationError, match="at least one"):
        ChunkVerifyRequest(chunk="   ")
    assert ChunkVerifyRequest(chunk=GOOD_PROOF).chunk


def test_document_request_rejects_ml():
    with pytest.raises(ValidationError, match="code-executing"):
        DocumentLoadRequest(text='theory X imports Main begin\nsetup ‹I›\nend')
    assert DocumentLoadRequest(text=GOOD_PROOF).text


def test_bigstep_request_rejects_ml():
    # the lease-free endpoint — the most exposed path
    with pytest.raises(ValidationError, match="code-executing"):
        BigStepTheoryRequest(
            theory_name="X",
            theory='theory X imports Main begin\nML ‹OS.Process.system "id"›\nend',
        )
    ok = BigStepTheoryRequest(theory_name="Scratch", theory=GOOD_PROOF)
    assert ok.theory


def test_fill_request_rejects_ml_and_axiomatization():
    # routes/fill.py loads `text` into the session itself, so the other models'
    # validators never see it (report recheck 2026-10-27, 3B.2)
    with pytest.raises(ValidationError, match="code-executing"):
        FillSorriesRequest(text='lemma x: True\n  sorry\nML ‹OS.Process.system "id"›')
    with pytest.raises(ValidationError, match="code-executing"):
        FillSorriesRequest(
            text='axiomatization where ax: "False"\nlemma x: "P" by (simp add: ax)'
        )
    assert FillSorriesRequest(text=GOOD_PROOF).text


def test_models_allow_ml_when_policy_permits(monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", True)
    assert CommandRequest(command="ML ‹1›").command == "ML ‹1›"
    assert BigStepTheoryRequest(theory_name="X", theory="theory X imports Main begin ML ‹1› end")
    assert FillSorriesRequest(text="ML ‹1›").text == "ML ‹1›"


# --------------------------------------------------- import-name injection

@pytest.mark.parametrize("name", [
    "Main", "HOL-Analysis.Derivative", "HOL-Library.Multiset", "~~/src/HOL/List",
    "$AFP/thys/Foo/Bar", "Foo_Bar'", "Complex_Main",
])
def test_import_names_accepted(name):
    assert validate_import_name(name) == name


@pytest.mark.parametrize("name", [
    'Main" begin ML ‹x› theory Y imports "Main',   # quote break-out
    "Main begin", "Ma in", "Main\n", "", "Main;", "Main‹x›",
])
def test_import_names_rejected(name):
    with pytest.raises(ValueError):
        validate_import_name(name)


def test_import_lists_validated_on_models():
    bad = ['Main" begin ML ‹x› (*']
    for build in (
        lambda: EnterTheoryRequest(imports=bad),
        lambda: DocumentLoadRequest(text="lemma True by simp", thy_name="T", imports=bad),
        lambda: SessionCreateRequest(theories=bad),
        lambda: SessionAcquireRequest(theories=bad),
        lambda: BigStepTheoryRequest(theory_name="X", theory="theory X imports Main begin end",
                                     dependencies=bad),
    ):
        with pytest.raises(ValidationError, match="invalid theory/import name"):
            build()


# ====================================================== SEC-3: heap inputs

@pytest.mark.parametrize("value", ["alpha", "task-1", "a.b_c", "0x", "A" * 64])
def test_safe_name_accepts(value):
    assert validate_safe_name(value) == value


@pytest.mark.parametrize("value", [
    "../x", "..", ".", "a/b", "a\\b", ".hidden", "", " ", "a b", "A" * 65, "%2F", "a;b", "$x",
])
def test_safe_name_rejects(value):
    with pytest.raises(ValueError):
        validate_safe_name(value)


def test_project_path_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(Heap, "ALLOWED_ROOTS", [str(tmp_path / "ok")])
    good = tmp_path / "ok" / "proj"
    assert validate_project_path(str(good)) == str(good)
    with pytest.raises(ValueError, match="outside the allowed heap roots"):
        validate_project_path(str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="outside the allowed heap roots"):
        validate_project_path(str(tmp_path / "ok" / ".." / "elsewhere"))  # traversal via ..
    with pytest.raises(ValueError, match="absolute"):
        validate_project_path("relative/dir")
    with pytest.raises(ValueError):
        validate_project_path("")


def test_default_allowed_roots_are_repo_and_isabelle_user_dir():
    """Host-aware defaults: this checkout + ~/.isabelle, which inside the container
    are exactly /app and /root/.isabelle (no .env override needed either way)."""
    import os
    env = os.getenv("ISABELLE_HEAP_POOL_ALLOWED_ROOTS") or os.getenv("ISABELLE_HEAP_ALLOWED_ROOTS")
    if env:
        assert Heap.ALLOWED_ROOTS == [r for r in env.split(":") if r]
    else:
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        assert Heap.ALLOWED_ROOTS == [repo, os.path.join(os.path.expanduser("~"), ".isabelle")]
        assert Heap.STATE_DIR == os.path.join(os.path.expanduser("~"), ".isabelle", "heap_pool")


def test_heap_build_request_validates_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(Heap, "ALLOWED_ROOTS", [str(tmp_path)])
    proj = str(tmp_path / "p")
    assert HeapBuildRequest(task_group="alpha", project=proj, session_name="Hp1")
    with pytest.raises(ValidationError, match="invalid task_group"):
        HeapBuildRequest(task_group="../../tmp/x", project=proj)
    with pytest.raises(ValidationError, match="invalid session_name"):
        HeapBuildRequest(task_group="alpha", project=proj, session_name="Hp1; rm -rf /")
    with pytest.raises(ValidationError, match="outside the allowed heap roots"):
        HeapBuildRequest(task_group="alpha", project="/etc")


def test_session_requests_validate_group_and_project(tmp_path, monkeypatch):
    monkeypatch.setattr(Heap, "ALLOWED_ROOTS", [str(tmp_path)])
    with pytest.raises(ValidationError, match="invalid task_group"):
        SessionCreateRequest(task_group="../x")
    with pytest.raises(ValidationError, match="invalid heap_session"):
        SessionAcquireRequest(heap_session="a/b")
    with pytest.raises(ValidationError, match="outside the allowed heap roots"):
        SessionAcquireRequest(project="/etc")
    assert SessionAcquireRequest(task_group="alpha", project=str(tmp_path / "p"))


# --------------------------------------------------------- HeapPool itself

class _FakeProc:
    """`isabelle build` as build_process.start runs it (output to the given log file)."""
    returncode = None
    pid = -1

    async def wait(self):
        self.returncode = 0
        return 0


def _fake_exec(monkeypatch):
    async def fake(*args, **kwargs):
        kwargs["stdout"].write(b"Build OK\n")
        return _FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


def _project(tmp_path, body="lemma bar_lemma: True by simp"):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "Bar.thy").write_text(f"theory Bar imports Main begin\n{body}\nend\n")
    return proj


def test_pool_manifest_path_contained(tmp_path, monkeypatch):
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path)])
    with pytest.raises(HeapRejected):
        pool._manifest_path("../../escape", "/x")
    ok = pool._manifest_path("alpha", "/x")
    assert ok.parent == (tmp_path / "state" / "alpha")


def test_pool_build_rejects_bad_group_and_outside_project(tmp_path, monkeypatch):
    _fake_exec(monkeypatch)
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path / "roots")])
    proj = _project(tmp_path)  # NOT under tmp_path/roots
    with pytest.raises(HeapRejected, match="outside the allowed heap roots"):
        asyncio.run(pool.build("alpha", str(proj)))
    inside = tmp_path / "roots" / "proj"
    inside.mkdir(parents=True)
    (inside / "Bar.thy").write_text("theory Bar imports Main begin\nlemma b: True by simp\nend\n")
    with pytest.raises(HeapRejected, match="invalid task_group"):
        asyncio.run(pool.build("../alpha", str(inside)))
    entry = asyncio.run(pool.build("alpha", str(inside), session_name="Hp1"))
    assert entry["status"] == "ready"
    # nothing was written outside the state dir
    assert not (tmp_path / "alpha").exists()


def test_pool_build_rejects_ml_in_project_sources(tmp_path, monkeypatch):
    _fake_exec(monkeypatch)
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path)])
    proj = _project(tmp_path, body='ML ‹OS.Process.system "id"›')
    with pytest.raises(HeapRejected, match="code-executing"):
        asyncio.run(pool.build("alpha", str(proj), session_name="Hp1"))
    # sub-directory theories are scanned too (a user ROOT may reference them)
    proj2 = _project(tmp_path)
    (proj2 / "sub").mkdir()
    (proj2 / "sub" / "Evil.thy").write_text("theory Evil imports Main begin\nsetup ‹I›\nend\n")
    with pytest.raises(HeapRejected, match="Evil.thy"):
        asyncio.run(pool.build("alpha", str(proj2), session_name="Hp2"))
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", True)
    assert asyncio.run(pool.build("alpha", str(proj2), session_name="Hp2"))["status"] == "ready"


def _fake_home(tmp_path, names=("Hp1",)):
    home = tmp_path / "home_user"
    for name in names:
        img = home / "heaps" / "polyml-test_platform" / name
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_text("heap")
    (home / "heaps" / "polyml-test_platform" / "log").mkdir(exist_ok=True)
    return home


def test_delete_heap_image_rejects_traversal(tmp_path, monkeypatch):
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path)])
    home = _fake_home(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    monkeypatch.setattr(pool, "_home_user", home)
    for session, platform in (
        ("../../victim.txt", None),
        ("victim.txt", "../.."),
        ("*", None),                # glob metachar
        ("log", "polyml-test_platform"),
        ("", None),
    ):
        with pytest.raises(HeapRejected):
            pool.delete_heap_image(session, platform)
    assert victim.exists()
    # the legitimate path still works
    out = pool.delete_heap_image("Hp1", "polyml-test_platform")
    assert out["deleted"] == "Hp1"


# ------------------------------------------------ router admin gate (heaps)

def _router_deps():
    pytest.importorskip("fastapi")
    from server.app.api.v1.routes import heaps as r
    return r


def test_destructive_heap_endpoints_require_admin_token(tmp_path, monkeypatch):
    r = _router_deps()
    from fastapi import HTTPException
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path)])

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "")
    for call in (
        lambda tok: r.delete_heap_image("Hp1", None, tok, pool),
        lambda tok: r.delete_heap("alpha", "/x", tok, pool),
        lambda tok: r.delete_heap_group("alpha", tok, pool),
    ):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(call("anything"))
        assert ei.value.status_code == 403  # token not configured → always 403

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(r.delete_heap_group("alpha", "wrong", pool))
    assert ei.value.status_code == 403
    # correct token: passes the gate (group empty → 0 removed)
    out = asyncio.run(r.delete_heap_group("alpha", "secret", pool))
    assert out == {"deleted": 0, "task_group": "alpha"}
    # correct token but unsafe segment → 422
    with pytest.raises(HTTPException) as ei:
        asyncio.run(r.delete_heap_group("../alpha", "secret", pool))
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        asyncio.run(r.delete_heap_image("../x", None, "secret", pool))
    assert ei.value.status_code == 422


def test_build_endpoint_stays_open_without_token(tmp_path, monkeypatch):
    """Owner decision 2026-09-22: build is not token-gated (MCP isabelle_build_heap
    keeps working); it is protected by path/name validation instead."""
    _fake_exec(monkeypatch)
    r = _router_deps()
    monkeypatch.setattr(Heap, "ALLOWED_ROOTS", [str(tmp_path)])
    pool = HeapPool(state_dir=str(tmp_path / "state"), allowed_roots=[str(tmp_path)])
    proj = _project(tmp_path)
    req = HeapBuildRequest(task_group="alpha", project=str(proj), session_name="Hp1")

    class _NoPressure:  # the memory gate (batch 1) admits; tested in test_memory_policy.py
        def require_memory_for_build(self, kind):
            return None

    out = asyncio.run(r.build_heap(req, pool, session_manager=_NoPressure()))
    assert out.status == "ready"
