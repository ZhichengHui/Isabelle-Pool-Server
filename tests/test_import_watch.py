"""An open file follows edits of the project theories it imports: the watched files are found
through the imports, a change of one reopens the file on a new session, and scratch sessions
loaded with the old version are not reused."""
import asyncio
import os
import re

from mcp_servers.lsp import import_watch as iw


class _Parser:
    """parse_theory_header as the server answers it, for `imports a "b" begin` headers."""

    async def parse_theory_header(self, text):
        m = re.search(r"imports\s+(.*?)\s+begin", text, re.S)
        return {"imports": [t.strip('"') for t in m.group(1).split()] if m else []}


def _thy(path, imports):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = path.stem
    path.write_text(f"theory {name} imports {' '.join(imports)} begin end")
    return str(path)


def _project(tmp_path):
    proj = tmp_path / "proj"
    a = _thy(proj / "A.thy", ["B", '"S.T"', '"HOL-Library.Multiset"'])
    b = _thy(proj / "B.thy", ["C"])
    c = _thy(proj / "C.thy", ["Main"])
    (proj / "lib").mkdir()
    (proj / "lib" / "ROOT").write_text("session S = HOL + theories T")
    t = _thy(proj / "lib" / "sub" / "T.thy", ["Main"])
    return a, b, c, t, str(proj / "lib")


def test_the_watched_files_follow_the_imports(tmp_path):
    a, b, c, t, lib = _project(tmp_path)
    imports = [b[:-4], "S.T", "HOL-Library.Multiset"]  # as file_context gives them
    files = asyncio.run(iw.import_files(_Parser(), imports, a, [lib]))
    assert files == [b, t, c, os.path.join(lib, "ROOT")]  # heap/distribution imports: not watched


def test_the_file_itself_and_cycles_are_not_watched_twice(tmp_path):
    proj = tmp_path / "p"
    a = _thy(proj / "A.thy", ["B"])
    b = _thy(proj / "B.thy", ["A", "B"])
    assert asyncio.run(iw.import_files(_Parser(), [b[:-4]], a, [])) == [b]


def test_state_changed_and_digest(tmp_path):
    f = tmp_path / "B.thy"
    f.write_text("x")
    before = iw.state([str(f)])
    assert iw.digest(before) and iw.digest(()) == ""
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    after = iw.state([str(f)])
    assert iw.changed(before, after) == ["B.thy"] and iw.digest(after) != iw.digest(before)
    f.unlink()
    assert iw.changed(after, iw.state([str(f)])) == ["B.thy"]


# ----- the pool: a change reopens the file -----

def _pool(monkeypatch, tmp_path):
    from mcp_servers.lsp import pool as pool_mod

    proj = tmp_path / "w"
    a = _thy(proj / "A.thy", ["B"])
    b = _thy(proj / "B.thy", ["Main"])
    log = {"acquired": [], "closed": [], "loaded": []}

    class Client(_Parser):
        async def acquire_session(self, **kw):
            sid = f"s{len(log['acquired']) + 1}"
            log["acquired"].append(sid)
            return {"session_id": sid, "lease_id": "L" + sid}

        async def close_session(self, sid, lease_id=None):
            log["closed"].append(sid)

        async def release_session(self, sid, lease_id=None):
            pass

        async def load_document(self, sid, text, **kw):
            log["loaded"].append(sid)
            return {"report": {"success": True, "commands": []}}

    async def ctx(c, text, canon, heap_session):
        return [b[:-4]], "HOL", ()

    p = pool_mod.LspPool()

    async def client():
        return Client()

    monkeypatch.setattr(p, "client", client)
    monkeypatch.setattr(pool_mod, "file_context", ctx)
    monkeypatch.setattr(pool_mod.lease_renewal, "ensure", lambda pool: None)
    monkeypatch.setattr(pool_mod.idle_release, "ensure", lambda pool: None)
    return p, a, b, log


def _edit(path):
    st = os.stat(path)
    with open(path, "a") as f:
        f.write("\n(* edited *)")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_an_edited_import_reopens_the_file_on_a_new_session(monkeypatch, tmp_path):
    p, a, b, log = _pool(monkeypatch, tmp_path)

    async def run():
        binding = await p.get_binding(a)
        assert binding.import_files == [b] and binding.import_digest
        await p.sync(binding)
        assert await p.sync(binding) is False  # nothing changed: no reload, no new session
        assert log["acquired"] == ["s1"]
        old_digest = binding.import_digest
        _edit(b)
        assert await p.sync(binding) is True  # reloaded on the new session
        assert binding.session_id == "s2" and log["closed"] == ["s1"]
        assert log["loaded"] == ["s1", "s2"]
        assert binding.import_digest != old_digest and old_digest in p._stale_generations
        assert "Imported theories changed since this file was opened (B.thy)" in binding.heap_note
        assert not binding.heap_note_shown
        assert p.peek_binding(a) is binding  # callers keep the same object
        assert await p.sync(binding) is False  # the new state is the baseline now

    asyncio.run(run())


def test_scratch_sessions_of_an_old_import_version_are_not_reused(monkeypatch, tmp_path):
    p, *_ = _pool(monkeypatch, tmp_path)
    dropped = []

    async def drop(key, sid, lease):
        dropped.append(sid)

    monkeypatch.setattr(p, "drop_scratch", drop)
    old = p.scratch_key("g", None, ["X"], "HOL", (), "v1")
    new = p.scratch_key("g", None, ["X"], "HOL", (), "v2")
    assert old != new

    async def run():
        for key, sid in ((old, "idle-old"), (new, "idle-new")):
            p._scratch[key] = asyncio.Queue()
            await p.release_scratch(key, sid, "L")
        assert await p.retire_idle_scratch(0.0, generation="v1") == 1
        assert dropped == ["idle-old"] and p._scratch[new].qsize() == 1
        p._stale_generations.add("v1")
        await p.release_scratch(old, "busy-old", "L")  # returned after the edit: closed
        assert dropped == ["idle-old", "busy-old"] and p._scratch[old].qsize() == 0

    asyncio.run(run())
