"""Imports of a file that name a sibling theory (same directory), as `isabelle build` resolves them.

The server's wrapper theory lives in the server's own directory, so a bare `imports A` is looked up
there. The MCP names such a sibling by its absolute path instead (resolve_sibling_imports), and the
siblings' own imports are collected for the session image choice (sibling_imports)."""
from __future__ import annotations

import os
import re
from typing import List, Optional

_BARE_THEORY = re.compile(r"[A-Za-z0-9_']+")
# what the server accepts as an import name (input_guards.IMPORT_NAME_RE)
_SERVER_IMPORT = re.compile(r"[A-Za-z0-9_'.\-/~$]{1,200}")


def resolve_sibling_imports(imports: List[str], master_dir: Optional[str]) -> List[str]:
    """Imports as `isabelle build` resolves them: a bare name (no session
    qualifier, no path) that is a .thy file in `master_dir`, the importing
    file's directory, becomes the absolute path of that file, which the server's
    wrapper theory (it lives in the server's own directory, not in the file's)
    can import. Qualified names, paths and bare names without such a file are
    unchanged, so heap theories and the field choice behave as before. The
    server must see the same disk as this MCP (it does on the shared host).
    A path the server's name check would reject is left as the bare name."""
    if not master_dir:
        return list(imports)
    out: List[str] = []
    for name in imports:
        path = os.path.join(master_dir, name)
        if (
            _BARE_THEORY.fullmatch(name)
            and os.path.isfile(path + ".thy")
            and _SERVER_IMPORT.fullmatch(path)
        ):
            out.append(path)
        else:
            out.append(name)
    return out


def master_dir_of(file_path: str) -> str:
    """The directory of a (canonical) file: Isabelle's master directory for it."""
    return os.path.dirname(file_path)


async def sibling_imports(
    client, imports: List[str], seen: set, depth: int = 8,
) -> List[str]:
    """The imports (not themselves siblings) of the sibling files named by the
    absolute paths in `imports`, transitively through further siblings."""
    out: List[str] = []
    for name in imports:
        if not name.startswith("/") or name in seen or depth <= 0:
            continue
        seen.add(name)
        try:
            with open(name + ".thy", encoding="utf-8") as f:
                resp = await client.parse_theory_header(f.read())
        except OSError:
            continue
        theirs = resolve_sibling_imports(list(resp.get("imports") or []), os.path.dirname(name))
        out += [n for n in theirs if not n.startswith("/")]
        out += await sibling_imports(client, theirs, seen, depth - 1)
    return out
