"""Choose the session image a session starts on, so that theories already built into an image are
not checked again from source.

A file's qualified imports (`Micro_Rust_Std_Lib.StdLib_All`) name theories; an image covers them
when they are among the theories built into it or into the images it was built on (the build
database `log/<session>.db` of each image records both: the exports of its theories and its input
heaps). The proposed field is kept when it covers the imports and is up to date; otherwise the
smallest up-to-date image that covers them is chosen; when none does, the proposed field stays.
Up to date means `isabelle build -n -b` (no build) succeeds for it, with the session directories
the session will use. That check takes seconds (6 s for HOL, 20 s for AutoCorrode), so its result is
cached, and the cache key is every input the check reads that can change under a running server:
the image files of the session and of its input heaps, every source file their build databases list
(`isabelle_session_info.sources`), ROOT and ROOTS in every directory above a source (a ROOT edit only
shows in the `<meta_info>` digest; the session's own ROOT is among them), ROOT and ROOTS of the `-d`
directories, and ROOTS, etc/settings and etc/preferences of the user home, each as (mtime_ns, size)
or None when missing. Path variables other than `~~` and `~` are resolved with `isabelle getenv`. A change to any of them runs the check again; Isabelle stays the judge of
"up to date". When an input cannot be located (no build database, an unknown path variable, an input
heap that is not an image here, a relative path), nothing is cached and the check runs on every call. A key on the
image file alone (before 2026-11-23) missed edits to the sources: the stale image was chosen for up
to 10 minutes and the session start failed with image_not_current.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from server.app.services.heap_pool import default_isabelle as _isabelle

_QUALIFIED = re.compile(r'^"?([A-Za-z0-9_+\-]+\.[A-Za-z0-9_\'.]+)"?$')
_lock = threading.Lock()
_env: Optional[Dict[str, str]] = None
_theories: Dict[Tuple[str, float], Set[str]] = {}
_ENV_VARS = ("ISABELLE_HEAPS", "ISABELLE_HEAPS_SYSTEM", "ML_SYSTEM",
             "ISABELLE_HOME", "ISABELLE_HOME_USER", "USER_HOME")
_USER_FILES = ("ROOTS", "etc/settings", "etc/preferences")
Stat = Optional[Tuple[int, int]]
_current: Dict[Tuple[str, Tuple[str, ...]], Tuple[tuple, bool]] = {}


def _getenv() -> Dict[str, str]:
    global _env
    if _env is None:
        out = subprocess.run(
            [_isabelle(), "getenv", "-b", *_ENV_VARS],
            capture_output=True, text=True, timeout=60, check=True).stdout
        _env = dict(zip(_ENV_VARS, out.splitlines()))
    return _env


def images() -> Dict[str, Path]:
    """Session name -> image file, user images before system ones."""
    env = _getenv()
    found: Dict[str, Path] = {}
    ml = env.get("ML_SYSTEM", "")
    for base in (env.get("ISABELLE_HEAPS", ""), env.get("ISABELLE_HEAPS_SYSTEM", "")):
        # <heaps>/<ML_SYSTEM>_<platform>/<session> (ML_PLATFORM is not visible to getenv)
        dirs = sorted(Path(base).glob(f"{ml}_*")) if base and ml else []
        for f in (f for d in dirs for f in sorted(d.iterdir())):
            if f.is_file() and f.name not in found:
                found[f.name] = f
    return found


def _db_theories(image: Path) -> Tuple[Set[str], List[str]]:
    """Theories built into `image` and the names of its input heaps, from its build database."""
    db = image.parent / "log" / f"{image.name}.db"
    if not db.is_file():
        return set(), []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = {r[0] for r in con.execute("select distinct theory_name from isabelle_exports")}
        row = con.execute("select input_heaps from isabelle_session_info").fetchone()
    finally:
        con.close()
    inputs = [ln.split()[-1] for ln in (row[0] if row and row[0] else "").splitlines() if ln.split()]
    return names, inputs


def theories(name: str, imgs: Dict[str, Path]) -> Set[str]:
    """Theories available on image `name`: its own and those of the images it was built on."""
    image = imgs[name]
    key = (name, image.stat().st_mtime)
    if key not in _theories:
        own, inputs = _db_theories(image)
        for parent in inputs:
            if parent in imgs and parent != name:
                own = own | theories(parent, imgs)
        _theories[key] = own
    return _theories[key]


def _stat(path: Path) -> Stat:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _db_inputs(image: Path) -> Optional[Tuple[List[str], List[str]]]:
    """Source paths (as Isabelle writes them) and input heap names from the build database."""
    db = image.parent / "log" / f"{image.name}.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("select sources, input_heaps from isabelle_session_info").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    text = [v.decode() if isinstance(v, bytes) else (v or "") for v in row]
    sources = [ln.split(" ", 1)[1] for ln in text[0].splitlines() if " " in ln]
    inputs = [ln.split()[-1] for ln in text[1].splitlines() if ln.split()]
    return sources, inputs


_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _getvar(var: str, env: Dict[str, str]) -> str:
    """An Isabelle setting (`$AFP`, a component's variable, ...): from `env`, else asked once from
    `isabelle getenv` and kept in `env` ("" when unset)."""
    if var not in env:
        try:
            out = subprocess.run([_isabelle(), "getenv", "-b", var],
                                 capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        env[var] = (out or "").strip()
    return env[var]


def _resolve(source: str, env: Dict[str, str]) -> Optional[Path]:
    """A source path of a build database as a file (`~~`, `~`, `$VAR`, absolute); None if unknown."""
    if source.startswith("~~/"):
        base, rest = env.get("ISABELLE_HOME"), source[3:]
    elif source.startswith("~/"):
        base, rest = env.get("USER_HOME"), source[2:]
    elif source.startswith("$"):
        var, _, rest = source[1:].partition("/")
        base = _getvar(var, env) if _VAR.fullmatch(var) else None
    elif source.startswith("/"):
        return Path(source)
    else:
        return None
    return Path(base) / rest if base else None


def _catalog_files(dirs: Set[Path]) -> List[Path]:
    """ROOT and ROOTS of each of `dirs` and of all their ancestors, existing or not (a ROOT that
    appears changes the key too). The session's own ROOT is among them wherever its theories lie,
    also when a subdirectory holds a ROOT of another session."""
    above: Set[Path] = set()
    for d in dirs:
        above.update((d, *d.parents))
    return [a / f for a in sorted(above) for f in ("ROOT", "ROOTS")]


def _inputs_key(name: str, imgs: Dict[str, Path], env: Dict[str, str],
                seen: Set[str]) -> Optional[tuple]:
    """The state of every input of the up-to-date check of `name` (module docstring); None when
    one of them cannot be located."""
    db = _db_inputs(imgs[name])
    if db is None:
        return None
    sources, inputs = db
    parts: List[tuple] = [(str(imgs[name]), _stat(imgs[name]))]
    source_dirs: Set[Path] = set()
    for source in sources:
        if source.startswith("<"):  # <meta_info>, <build_prefs:...>: digests, no file
            continue
        path = _resolve(source, env)
        if path is None:
            return None
        parts.append((str(path), _stat(path)))
        source_dirs.add(path.parent)
    parts.extend((str(f), _stat(f)) for f in _catalog_files(source_dirs))
    for parent in inputs:
        if parent.startswith("<"):  # Pure lists `<meta_info>` among its input heaps
            continue
        if parent not in imgs:
            return None
        if parent in seen:
            continue
        seen.add(parent)
        sub = _inputs_key(parent, imgs, env, seen)
        if sub is None:
            return None
        parts.append(sub)
    return tuple(parts)


def current(name: str, imgs: Dict[str, Path], dirs: Tuple[str, ...]) -> bool:
    """`isabelle build -n -b` succeeds for `name`; cached while its inputs are unchanged."""
    env = _getenv()
    key = _inputs_key(name, imgs, env, {name})
    home_user = env.get("ISABELLE_HOME_USER")
    if key is not None and home_user:
        key += tuple((f, _stat(Path(home_user) / f)) for f in _USER_FILES)
    if key is not None:  # the -d directories: the sessions they define and catalog
        key += tuple((str(f), _stat(f)) for d in dirs for f in (Path(d) / "ROOT", Path(d) / "ROOTS"))
    hit = _current.get((name, dirs))
    if key is not None and hit and hit[0] == key:
        return hit[1]
    cmd = [_isabelle(), "build", "-n", "-b"] + [a for d in dirs for a in ("-d", d)] + [name]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    if key is not None:
        _current[(name, dirs)] = (key, ok)
    return ok


def selection(imports: List[str], field: Optional[str], dirs: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """The image to start on for `imports` (see the module docstring), as {field, from_source,
    stale}: `from_source` are the qualified imports the chosen image does not contain (the session
    loads them from source), `stale` the images that contain all of them but are out of date (a
    source changed after the build; empty when one is chosen), `field_stale` whether the proposed
    field is one of them: then the session cannot start at all, and `from_source` is every import."""
    wanted = {m.group(1) for m in (_QUALIFIED.match(i) for i in imports) if m}
    if not wanted:
        return {"field": field, "from_source": [], "stale": [], "field_stale": False}
    stale: List[str] = []
    with _lock:
        imgs = images()
        covering = [s for s in imgs if wanted <= theories(s, imgs)]
        candidates = ([field] if field in covering else []) + [
            s for s in sorted(covering, key=lambda s: len(theories(s, imgs))) if s != field]
        for s in candidates:
            if current(s, imgs, dirs):
                return {"field": s, "from_source": [], "stale": [], "field_stale": False}
            stale.append(s)
        have = theories(field, imgs) if field in imgs else set()
    if field in stale:  # the session start itself fails (image_not_current): nothing is usable
        return {"field": field, "from_source": sorted(wanted), "stale": stale, "field_stale": True}
    return {"field": field, "from_source": sorted(wanted - have), "stale": stale,
            "field_stale": False}


def select_field(imports: List[str], field: Optional[str], dirs: Tuple[str, ...] = ()) -> Optional[str]:
    """The image to start on for `imports` (see the module docstring)."""
    return selection(imports, field, dirs)["field"]
