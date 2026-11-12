"""Input guards shared by every text- and path-accepting API surface.

Three guards, one module, so the policy has a single source of truth:

1. ``reject_code_execution(text)`` — refuse Isar text that could execute ML,
   touch the filesystem, or install ML-level hooks. Before this guard existed
   only ``POST /diagnostic`` was protected (``diagnostic_guard``); ``commands``,
   ``verify_chunk``, ``PUT /document`` and the lease-free ``bigstep`` accepted
   ``ML ‹OS.Process.system "..."›`` verbatim, and the heap pool ran
   ``isabelle build`` on caller-chosen directories. The denylist is reused from
   ``diagnostic_guard`` so a new dangerous keyword is a one-line edit there.

   To avoid false positives on ordinary proofs, the text is first reduced to its
   *command skeleton*: nested ``(* *)`` comments are removed and the bodies of
   string literals (``"…"``), cartouches (``‹…›`` / ``\\<open>…\\<close>``) and
   alt-strings (``` `…` ```) are blanked. Isar command keywords always sit
   OUTSIDE those literals (``ML ‹…›``, ``ML_file "…"``, ``setup ‹…›``), so the
   keyword survives the reduction while a comment such as ``(* no ML here *)``
   or a lemma about a constant named ``setup`` inside a string does not.

   Gated by ``Server.ALLOW_ML_COMMANDS`` (env ``ISABELLE_ALLOW_ML_COMMANDS``,
   default false) for deployments that genuinely need ML in client theories.

2. ``validate_safe_name(value, what)`` — identifiers that become path segments
   or CLI arguments (``task_group``, ``session_name``, heap image ``session`` /
   ``platform``): ``^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$``, and never ``.``/``..``.
   Closes the ``task_group="../../tmp/x"`` manifest write and the
   ``DELETE /heaps/images/..%2F..`` glob-into-rmtree paths.

3. ``validate_project_path(path)`` — heap ``project`` directories must be
   absolute and resolve under one of ``Heap.ALLOWED_ROOTS`` (env
   ``ISABELLE_HEAP_POOL_ALLOWED_ROOTS``, colon-separated, default
   ``/app:/root/.isabelle``). ``isabelle build -d <project>`` executes whatever
   theories (and ROOT) live there, so the directory set must be operator-chosen.

All three raise ``ValueError`` with a client-readable message; the Pydantic
models surface that as HTTP 422, the router as ``HTTPException(422)``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from server.app.core.config import Heap, Server
from server.app.core.diagnostic_guard import _DANGEROUS_RE
from server.app.services.theory_parsing import strip_theory_comments

# --------------------------------------------------------------------------
# 1. Code-execution guard

_CARTOUCHE_OPEN = "‹"   # ‹
_CARTOUCHE_CLOSE = "›"  # ›
_ASCII_OPEN = "\\<open>"
_ASCII_CLOSE = "\\<close>"


def strip_isar_literals(text: str) -> str:
    """Blank the *contents* of Isar literals, keeping the delimiters and every
    newline (so any line-based reporting stays aligned).

    Handles: ``"…"`` with backslash escapes, nested cartouches in both the
    Unicode (``‹ ›``) and ASCII (``\\<open> \\<close>``) spellings, and
    alt-strings ``` `…` ```. An unterminated literal blanks to end of text —
    the conservative direction (nothing after it can be a hidden command).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            out.append('"')
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    if text[i + 1] == "\n":
                        out.append("\n")
                    i += 2
                    continue
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            if i < n:
                out.append('"')
                i += 1
            continue
        if ch == "`":
            out.append("`")
            i += 1
            while i < n and text[i] != "`":
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            if i < n:
                out.append("`")
                i += 1
            continue
        if ch == _CARTOUCHE_OPEN or text.startswith(_ASCII_OPEN, i):
            depth = 0
            # consume the opener
            if ch == _CARTOUCHE_OPEN:
                out.append(_CARTOUCHE_OPEN)
                i += 1
            else:
                out.append(_ASCII_OPEN)
                i += len(_ASCII_OPEN)
            depth = 1
            while i < n and depth > 0:
                if text[i] == _CARTOUCHE_OPEN:
                    depth += 1
                    i += 1
                elif text.startswith(_ASCII_OPEN, i):
                    depth += 1
                    i += len(_ASCII_OPEN)
                elif text[i] == _CARTOUCHE_CLOSE:
                    depth -= 1
                    i += 1
                elif text.startswith(_ASCII_CLOSE, i):
                    depth -= 1
                    i += len(_ASCII_CLOSE)
                else:
                    if text[i] == "\n":
                        out.append("\n")
                    i += 1
            if depth == 0:
                out.append(_CARTOUCHE_CLOSE)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def command_skeleton(text: str) -> str:
    """Comments removed, literal bodies blanked — what is left is the sequence
    of Isar keywords, names and symbols that the outer syntax will dispatch on."""
    return strip_isar_literals(strip_theory_comments(text))


def find_code_execution(text: str) -> Optional[str]:
    """Return the first denylisted keyword that appears as a standalone token in
    the command skeleton of ``text``, else None. Does NOT consult the policy
    switch — callers that want the switch use ``reject_code_execution``."""
    if not isinstance(text, str) or not text:
        return None
    m = _DANGEROUS_RE.search(command_skeleton(text))
    return m.group(0) if m else None


def reject_code_execution(text: str, *, what: str = "text") -> str:
    """Raise ``ValueError`` if ``text`` contains a code-executing / IO Isar
    command and the server policy forbids them; otherwise return ``text``
    unchanged. See the module docstring for the exact rule."""
    if Server.ALLOW_ML_COMMANDS:
        return text
    kw = find_code_execution(text)
    if kw is not None:
        raise ValueError(
            f"{what} contains the code-executing command '{kw}', which this server "
            "rejects (ML / setup / file-IO commands are disabled; set "
            "ISABELLE_ALLOW_ML_COMMANDS=true to permit them)"
        )
    return text


# --------------------------------------------------------------------------
# 2. Safe identifiers used as path segments / CLI arguments

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_safe_name(value: str, what: str = "name") -> str:
    """A single path-segment-safe identifier (no slashes, no whitespace, no
    leading dot, max 64 chars, never ``.`` or ``..``)."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    # fullmatch: `$` alone would accept a trailing newline.
    if value in {".", ".."} or not SAFE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"invalid {what} {value!r}: use letters, digits, '_', '-', '.', "
            "starting with a letter or digit (max 64 chars)"
        )
    return value


# Theory / import names that get quoted into a generated `theory … imports …`
# header (session.py::_build_theory_header, thy_init wrappers). The builder
# wraps non-identifier names in double quotes WITHOUT escaping, so a name
# containing `"` could close the quote and inject further commands. Allow the
# characters real Isabelle theory names use — identifiers, session-qualified
# `HOL-Analysis.Derivative`, path-like `~~/src/HOL/Foo`, `$AFP/thys/X` — and
# nothing that can break out of a string or span whitespace.
IMPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_'.\-/~$]{1,200}$")


def validate_import_name(value: str) -> str:
    if not isinstance(value, str) or not IMPORT_NAME_RE.fullmatch(value):
        raise ValueError(
            f"invalid theory/import name {value!r}: quotes, whitespace and control "
            "characters are not allowed"
        )
    return value


MASTER_DIR_RE = re.compile(r"/[A-Za-z0-9_.\-/+@=,]*")


def validate_master_dir(value: Optional[str]) -> Optional[str]:
    """The directory a document comes from (its Isabelle master directory): an
    absolute path of plain characters without `..` (None passes)."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not MASTER_DIR_RE.fullmatch(value)
        or ".." in value.split("/")
    ):
        raise ValueError(
            f"invalid master_dir {value!r}: an absolute path of plain characters, no '..'"
        )
    return value


def validate_import_names(values: Iterable[str]) -> list[str]:
    return [validate_import_name(v) for v in values]


# --------------------------------------------------------------------------
# 3. Heap project directories

def _allowed_roots(roots: Optional[Iterable[str]] = None) -> list[Path]:
    raw = list(roots) if roots is not None else Heap.ALLOWED_ROOTS
    return [Path(r).resolve() for r in raw if r]


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_project_path(project: str, roots: Optional[Iterable[str]] = None) -> str:
    """``project`` must be an absolute path whose resolved form lies under one
    of the allowed roots (``Heap.ALLOWED_ROOTS`` unless ``roots`` is given).
    Existence is NOT checked here (the pool does that with a 404); this is the
    authorization check only. Returns ``project`` unchanged."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be a non-empty absolute path")
    p = Path(project)
    # The server runs in a Linux container, so "/app/x" is the real shape;
    # accept POSIX-absolute on any host (Windows dev/test) as well.
    if not (p.is_absolute() or project.startswith("/")):
        raise ValueError(f"project must be an absolute path, got {project!r}")
    resolved = p.resolve()
    allowed = _allowed_roots(roots)
    if not any(is_under(resolved, r) for r in allowed):
        raise ValueError(
            f"project {project!r} is outside the allowed heap roots "
            f"({':'.join(str(r) for r in allowed) or '<none>'}); set "
            "ISABELLE_HEAP_POOL_ALLOWED_ROOTS to widen"
        )
    return project


def assert_within(path: Path, root: Path, what: str = "path") -> Path:
    """Defensive containment check for paths the server is about to write or
    delete. Raises ``ValueError`` if ``path`` resolves outside ``root``."""
    rp, rr = path.resolve(), root.resolve()
    if not is_under(rp, rr):
        raise ValueError(f"{what} {path} escapes {root}")
    return rp
