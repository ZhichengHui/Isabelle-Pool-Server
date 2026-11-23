"""Package dependency direction (owner decision 2026-09-27).

    client       -> nothing in this repo (httpx only)
    mcp_*        -> client only
    pool_cli     -> client and mcp_servers (the shell client reuses the MCP header logic)
    server       -> nothing in client / mcp_* / evaluation / archive
    evaluation   -> client only (never server / repl / archive)

The client talks to the server through HTTP endpoints, so the two stay
consistent by construction; archived code (archive/) is frozen and may not be
imported by anything live. Tests are exempt (they may import anything).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# package dir -> import roots it must never touch
_MCP_PKGS = {"mcp_servers", "mcp"}
FORBIDDEN = {
    "client": {"server", "repl", "evaluation", "archive"} | _MCP_PKGS,
    "mcp_servers": {"server", "repl", "evaluation", "archive"},
    "server": {"client", "evaluation", "archive"} | _MCP_PKGS,
    "evaluation": {"server", "repl", "archive", "mcp_servers"},
    "pool_cli": {"server", "repl", "evaluation", "archive"},
}


def _import_roots(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module.split(".")[0]


def _sources(pkg: str):
    for p in sorted((ROOT / pkg).rglob("*.py")):
        if "__pycache__" in p.parts or "runs" in p.parts:
            continue
        yield p


@pytest.mark.parametrize("pkg", sorted(FORBIDDEN))
def test_no_forbidden_imports(pkg: str):
    if not (ROOT / pkg).is_dir():
        pytest.skip(f"{pkg}/ not present")
    bad = []
    for path in _sources(pkg):
        for lineno, root in _import_roots(path):
            if root in FORBIDDEN[pkg]:
                bad.append(f"{path.relative_to(ROOT).as_posix()}:{lineno} imports {root}")
    assert not bad, f"{pkg} must not import from {sorted(FORBIDDEN[pkg])}:\n  " + "\n  ".join(bad)
