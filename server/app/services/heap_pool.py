"""Heap pool (Stage 3): verified per-project Isabelle heaps built with
``isabelle build -b``, shareable by every REPL session of the owning task group.

Design (import-sync plan, internal working notes):
- Registry keyed by ``(task_group, project_dir)``; entries carry session_name,
  root_dir, fingerprint (sha256 over the ROOT text + sorted theory-file
  contents), status (building/ready/stale/failed), build_log_tail, built_at,
  built_by.
- One persisted JSON manifest per heap under ``Heap.STATE_DIR``
  (``<state_dir>/<group>/<project_hash>.json``) recording the ROOT text and the
  full theory-file list with per-file sha256+mtime; the registry is rebuilt from
  manifests at startup. A heap left in ``building`` by a crash comes back as
  ``failed`` (interrupted).
- ROOT strategy: a user-provided ROOT in the project dir wins (session name
  parsed from it unless overridden); otherwise a scratch ROOT is generated under
  ``<state_dir>/_roots/<group>/<project_hash>/``.
- Static imports: sources are re-hashed at session-creation time; a mismatch
  flips the entry to ``stale`` and the caller is told to rebuild explicitly.

NOTE: the HTTP API has no authentication — task groups are namespace isolation
for workflow organization and accident-proofing, NOT a security boundary.
Input hardening (2026-09-22): ``task_group`` / ``session_name`` / image
``session`` / ``platform`` must be path-segment-safe identifiers, ``project``
must resolve under ``Heap.ALLOWED_ROOTS`` (constructor ``allowed_roots``
overrides, for tests), manifest and image paths are containment-checked before
any write/delete, and project sources are scanned for code-executing Isar
commands unless ``ISABELLE_ALLOW_ML_COMMANDS`` is set (``isabelle build``
executes them as the container user). See ``server.app.core.input_guards``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server.app.core.config import Heap, Server
from server.app.core import metrics
from server.app.services import build_process
from server.app.core.input_guards import (
    assert_within,
    find_code_execution,
    validate_project_path,
    validate_safe_name,
)
from server.app.core.logging import get_logger

logger = get_logger(__name__)

def default_isabelle() -> str:
    """`isabelle` launcher, resolved like the gateway: $ISABELLE_HOME (native) else /opt/isabelle."""
    return str(Path(os.environ.get("ISABELLE_HOME", "/opt/isabelle")) / "bin" / "isabelle")

_STATUS_BUILDING = "building"
_STATUS_READY = "ready"
_STATUS_STALE = "stale"
_STATUS_FAILED = "failed"

_SESSION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ROOT_SESSION_RE = re.compile(r"(?m)^\s*session\s+\"?([A-Za-z][A-Za-z0-9_]*)\"?")


class HeapPoolError(Exception):
    """Base error for heap-pool operations."""
    status_code = 500


class HeapNotFound(HeapPoolError):
    status_code = 404


class HeapCrossGroup(HeapPoolError):
    """The named heap exists, but in another task group (isolation rule)."""
    status_code = 403


class HeapBuildInProgress(HeapPoolError):
    status_code = 409


class HeapNotReady(HeapPoolError):
    """Entry exists but is building/failed/stale — not usable for sessions."""
    status_code = 422


class HeapBuildFailed(HeapPoolError):
    status_code = 500


class HeapRejected(HeapPoolError):
    """Input refused by policy: unsafe name, project outside the allowed
    roots, path escaping its container, or code-executing Isar in the
    project sources (ISABELLE_ALLOW_ML_COMMANDS=false)."""

    status_code = 422


def _project_hash(project: str) -> str:
    return hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _theory_files(project: Path) -> List[Path]:
    """Top-level .thy files of the project (v1: non-recursive)."""
    return sorted(project.glob("*.thy"), key=lambda p: p.name)


def _default_session_name(project: Path) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", project.name)
    if not base or not base[0].isalpha():
        base = "Heap_" + base
    return base


class HeapPool:
    """In-memory registry over persisted manifests; see module docstring."""

    def __init__(
        self,
        state_dir: Optional[str] = None,
        isabelle: Optional[str] = None,
        allowed_roots: Optional[List[str]] = None,
    ):
        self.state_dir = Path(state_dir or Heap.STATE_DIR)
        self.isabelle = isabelle or default_isabelle()
        # None → Heap.ALLOWED_ROOTS at check time (so env/monkeypatch applies).
        self.allowed_roots: Optional[List[str]] = list(allowed_roots) if allowed_roots else None
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._home_user: Optional[Any] = None  # None = unresolved; False = unavailable
        self._home: Optional[Any] = None  # same, for ISABELLE_HOME
        self._load_manifests()

    # ---------------------------------------------------------- persistence

    def _manifest_path(self, task_group: str, project: str) -> Path:
        # task_group is a path segment: validate + contain (defense in depth
        # behind the request-model validator — manifests are also loaded from
        # disk, and other callers may bypass the API layer).
        try:
            validate_safe_name(task_group, "task_group")
            path = self.state_dir / task_group / f"{_project_hash(project)}.json"
            self.state_dir.mkdir(parents=True, exist_ok=True)
            assert_within(path, self.state_dir, "manifest path")
        except ValueError as e:
            raise HeapRejected(str(e)) from e
        return path

    def _check_inputs(self, task_group: str, project: str, session_name: Optional[str]) -> None:
        """Policy checks shared by build(): safe names + project under roots."""
        try:
            validate_safe_name(task_group, "task_group")
            if session_name is not None:
                validate_safe_name(session_name, "session_name")
            validate_project_path(project, self.allowed_roots)
        except ValueError as e:
            raise HeapRejected(str(e)) from e

    @staticmethod
    def _scan_for_code_execution(project: Path) -> None:
        """Refuse a project whose theories (recursively — a user ROOT may pull
        in subdirectories) contain ML / setup / file-IO commands, unless the
        server policy allows ML. `isabelle build` would execute them."""
        if Server.ALLOW_ML_COMMANDS:
            return
        for thy in sorted(project.rglob("*.thy")):
            try:
                text = thy.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            kw = find_code_execution(text)
            if kw is not None:
                raise HeapRejected(
                    f"{thy}: contains the code-executing command '{kw}'; heap builds "
                    "of such projects are disabled (ISABELLE_ALLOW_ML_COMMANDS=false)"
                )

    def _persist(self, entry: Dict[str, Any]) -> None:
        path = self._manifest_path(entry["task_group"], entry["project"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=2), encoding="utf-8")

    def _load_manifests(self) -> None:
        if not self.state_dir.exists():
            return
        for path in sorted(self.state_dir.glob("*/*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                # A heap left in "building" by a crash/restart is interrupted.
                if entry.get("status") == _STATUS_BUILDING:
                    entry["status"] = _STATUS_FAILED
                    entry["build_log_tail"] = (
                        entry.get("build_log_tail", "")
                        + "\n[heap_pool] build interrupted by server restart"
                    ).strip()
                key = (entry["task_group"], entry["project"])
                self._entries[key] = entry
            except Exception:
                logger.exception("ignoring unreadable heap manifest %s", path)

    # ---------------------------------------------------------- read access

    def get(self, task_group: str, project: str) -> Optional[Dict[str, Any]]:
        return self._entries.get((task_group, project))

    def find_by_session(self, task_group: str, session_name: str) -> Optional[Dict[str, Any]]:
        matches = [
            e for (g, _), e in self._entries.items()
            if g == task_group and e.get("session_name") == session_name
        ]
        return matches[0] if len(matches) == 1 else None

    def exists_elsewhere(self, task_group: str, session_name: Optional[str], project: Optional[str]) -> bool:
        """True if the named heap exists in a DIFFERENT group (403 rule)."""
        for (g, p), e in self._entries.items():
            if g == task_group:
                continue
            if project is not None and p == project:
                return True
            if session_name is not None and e.get("session_name") == session_name:
                return True
        return False

    def list(self, task_group: Optional[str] = None) -> List[Dict[str, Any]]:
        entries = [
            e for (g, _), e in self._entries.items()
            if task_group is None or g == task_group
        ]
        return sorted(entries, key=lambda e: (e["task_group"], e["project"]))

    def groups(self) -> List[Dict[str, Any]]:
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for (g, _), e in self._entries.items():
            by_group.setdefault(g, []).append(e)
        return [
            {
                "task_group": g,
                "heap_count": len(entries),
                "ready": sum(1 for e in entries if e.get("status") == _STATUS_READY),
            }
            for g, entries in sorted(by_group.items())
        ]

    # ---------------------------------------------------------- build

    def _root_and_session(
        self, task_group: str, project: Path, session_name: Optional[str]
    ) -> Tuple[str, str, str]:
        """Returns (root_dir, session_name, root_text)."""
        user_root = project / "ROOT"
        if user_root.is_file():
            root_text = user_root.read_text(encoding="utf-8")
            name = session_name
            if not name:
                m = _ROOT_SESSION_RE.search(root_text)
                if not m:
                    raise HeapPoolError(
                        f"{user_root}: could not parse session name; pass session_name"
                    )
                name = m.group(1)
            return str(project), name, root_text
        name = session_name or _default_session_name(project)
        if not _SESSION_NAME_RE.match(name):
            raise HeapPoolError(f"invalid session name: {name!r}")
        theories = [p.stem for p in _theory_files(project)]
        if not theories:
            raise HeapPoolError(f"{project}: no .thy files found")
        root_text = (
            f"session {name} = HOL +\n"
            f"  directories \"{project}\"\n"
            f"  theories\n"
            + "".join(f"    {t}\n" for t in theories)
        )
        root_dir = self.state_dir / "_roots" / task_group / _project_hash(str(project))
        return str(root_dir), name, root_text

    def _fingerprint(self, root_text: str, files: List[Dict[str, Any]]) -> str:
        h = hashlib.sha256()
        h.update(root_text.encode("utf-8"))
        for f in files:
            h.update(f["path"].encode("utf-8"))
            h.update(f["sha256"].encode("utf-8"))
        return h.hexdigest()

    def _scan_sources(self, project: Path) -> List[Dict[str, Any]]:
        return [
            {
                "path": str(p),
                "sha256": _sha256_file(p),
                "mtime": p.stat().st_mtime,
            }
            for p in _theory_files(project)
        ]

    async def build(
        self,
        task_group: str,
        project: str,
        session_name: Optional[str] = None,
        built_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build (or rebuild) the heap for (task_group, project).

        Raises HeapBuildInProgress (409) if a build for this key is running,
        HeapBuildFailed on a failed isabelle build (entry recorded as failed).
        """
        self._check_inputs(task_group, project, session_name)
        key = (task_group, project)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise HeapBuildInProgress(
                f"build already in progress for {task_group}:{project}"
            )
        async with lock:
            async with build_process.build_slots():
                return await self._build_locked(task_group, project, session_name, built_by)

    async def _build_locked(
        self,
        task_group: str,
        project: str,
        session_name: Optional[str],
        built_by: Optional[str],
    ) -> Dict[str, Any]:
        project_path = Path(project)
        if not project_path.is_dir():
            raise HeapNotFound(f"project dir not found: {project}")
        self._scan_for_code_execution(project_path)

        # Rebuild without an explicit session_name keeps the existing entry's
        # name — re-deriving it from the project dir would rename the heap and
        # orphan the old sessions' references.
        if session_name is None:
            existing = self._entries.get((task_group, project))
            if existing is not None:
                session_name = existing.get("session_name")

        root_dir, name, root_text = self._root_and_session(task_group, project_path, session_name)
        if root_dir != str(project_path):
            # scratch ROOT: place it in the state dir
            scratch = Path(root_dir)
            scratch.mkdir(parents=True, exist_ok=True)
            (scratch / "ROOT").write_text(root_text, encoding="utf-8")

        files = self._scan_sources(project_path)
        fingerprint = self._fingerprint(root_text, files)

        entry: Dict[str, Any] = {
            "task_group": task_group,
            "project": project,
            "session_name": name,
            "root_dir": root_dir,
            "root_text": root_text,
            "theory_files": files,
            "fingerprint": fingerprint,
            "status": _STATUS_BUILDING,
            "build_log_tail": "",
            "built_at": None,
            "built_by": built_by,
        }
        self._entries[(task_group, project)] = entry
        self._persist(entry)

        logger.info("heap build started group=%s project=%s session=%s", task_group, project, name)
        started = time.time()
        try:
            # A process group ended as a whole on timeout (build_process.py): the timeout used to
            # mark the entry failed and leave `isabelle build` and its poly processes running.
            log_path = self.state_dir / "_logs" / f"{task_group}-{name}.log"
            log_path.unlink(missing_ok=True)
            rc, timed_out = await build_process.run(
                [self.isabelle, "build", "-b", "-d", root_dir, name], log_path,
                timeout_s=Heap.BUILD_TIMEOUT_S)
            log = build_process.tail(log_path)
            if timed_out:
                log += f"\nisabelle build timed out after {Heap.BUILD_TIMEOUT_S}s and was stopped"
            ok = rc == 0 and not timed_out
        except Exception as e:
            ok = False
            log = f"failed to run isabelle build: {e}"

        entry["build_log_tail"] = log[-4000:]
        entry["built_at"] = time.time() if ok else None
        entry["status"] = _STATUS_READY if ok else _STATUS_FAILED
        self._persist(entry)
        elapsed = time.time() - started
        metrics.heap_build_seconds.labels(task_group).observe(elapsed)
        logger.info(
            "heap build %s group=%s project=%s session=%s elapsed=%.1fs",
            entry["status"], task_group, project, name, elapsed,
        )
        if not ok:
            raise HeapBuildFailed(
                f"isabelle build failed for {task_group}:{project} "
                f"(session {name}): {entry['build_log_tail'][-1000:]}"
            )
        return entry

    # ---------------------------------------------------------- staleness

    def check_stale(self, entry: Dict[str, Any]) -> bool:
        """Re-hash the project sources and compare with the build fingerprint.
        Only meaningful for a ``ready`` entry; a mismatch flips it to ``stale``
        (persisted) and returns True."""
        if entry.get("status") != _STATUS_READY:
            return False
        project_path = Path(entry["project"])
        try:
            current = self._fingerprint(entry["root_text"], self._scan_sources(project_path))
        except OSError:
            current = "<unreadable>"
        if current != entry["fingerprint"]:
            entry["status"] = _STATUS_STALE
            self._persist(entry)
            logger.info("heap marked stale group=%s project=%s",
                        entry["task_group"], entry["project"])
            return True
        return False

    # ---------------------------------------------------------- session gate

    def resolve_for_session(
        self,
        task_group: str,
        heap_session: Optional[str] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve and gate a heap for session creation.

        Raises: HeapCrossGroup (403), HeapNotFound (404), HeapNotReady (422).
        Returns the entry (status ready, sources fresh).
        """
        entry = None
        if project is not None:
            entry = self.get(task_group, project)
        if entry is None and heap_session is not None:
            entry = self.find_by_session(task_group, heap_session)
        if entry is None:
            if self.exists_elsewhere(task_group, heap_session, project):
                raise HeapCrossGroup(
                    f"heap {heap_session or project} belongs to another task group; "
                    f"sessions may only use their own group's heaps"
                )
            raise HeapNotFound(
                f"no heap for group {task_group!r} "
                f"(heap_session={heap_session!r}, project={project!r}); "
                f"build it first with POST /api/v1/heaps/build"
            )
        status = entry.get("status")
        if status == _STATUS_BUILDING:
            raise HeapNotReady(f"heap {entry['session_name']} is still building")
        if status == _STATUS_FAILED:
            raise HeapNotReady(
                f"heap {entry['session_name']} failed to build: "
                f"{entry.get('build_log_tail', '')[-500:]}"
            )
        if status == _STATUS_STALE or self.check_stale(entry):
            raise HeapNotReady(
                f"heap {entry['session_name']} is stale (sources changed since build); "
                f"rebuild with POST /api/v1/heaps/build "
                f"{{task_group={task_group!r}, project={entry['project']!r}}}"
            )
        return entry

    # ----------------------------------------------------- available images

    def delete_heap_image(self, session: str, platform: Optional[str] = None) -> Dict[str, Any]:
        """Delete a base heap image from the USER heaps dir (admin action).

        Hard boundary: only ISABELLE_HOME_USER is ever touched — distribution
        images (/opt/isabelle, e.g. HOL/Pure) can never be deleted through
        this path. Matching build logs are removed too. Live sessions that
        already loaded the image are unaffected (their heap is in memory).

        Returns {"deleted", "platform", "freed_mb"}; raises HeapNotFound.
        """
        try:
            validate_safe_name(session, "session")
            if platform is not None:
                validate_safe_name(platform, "platform")
        except ValueError as e:
            raise HeapRejected(str(e)) from e
        if session == "log" or platform == "log":
            raise HeapRejected("'log' is not a heap image")
        home = self._isabelle_home_user()
        if home is None:
            raise HeapNotFound("ISABELLE_HOME_USER unavailable")
        heaps = home / "heaps"
        patterns = [f"{platform}/{session}"] if platform else [f"*/{session}"]
        freed = 0
        hit_platform: Optional[str] = None
        for pattern in patterns:
            for img in heaps.glob(pattern):
                if not (img.is_file() or img.is_dir()):
                    continue
                # Containment: the image must be exactly heaps/<platform>/<session>.
                try:
                    assert_within(img, heaps, "heap image")
                except ValueError as e:
                    raise HeapRejected(str(e)) from e
                if img.parent.parent.resolve() != heaps.resolve():
                    continue
                hit_platform = img.parent.name
                if img.is_dir():
                    freed += sum(f.stat().st_size for f in img.rglob("*") if f.is_file())
                    shutil.rmtree(img, ignore_errors=True)
                else:
                    freed += img.stat().st_size
                    img.unlink(missing_ok=True)
                for logf in heaps.glob(f"{img.parent.name}/log/{session}.*"):
                    logf.unlink(missing_ok=True)
        if hit_platform is None:
            raise HeapNotFound(f"no user heap image for session {session!r}")
        logger.info("deleted user heap image %s (%s), freed %.1f MB",
                    session, hit_platform, freed / (1024 * 1024))
        return {
            "deleted": session,
            "platform": hit_platform,
            "freed_mb": round(freed / (1024 * 1024), 1),
        }

    # ---------------------------------------------------------- deletion

    def _isabelle_home_user(self) -> Optional[Path]:
        """ISABELLE_HOME_USER (e.g. /root/.isabelle/Isabelle2026), resolved
        lazily via `isabelle getenv` and cached. None on failure (GC skipped)."""
        if self._home_user is None:
            try:
                out = subprocess.run(
                    [self.isabelle, "getenv", "-b", "ISABELLE_HOME_USER"],
                    capture_output=True, text=True, timeout=30,
                )
                value = out.stdout.strip()
                self._home_user = Path(value) if value else False
            except Exception:
                logger.exception("failed to resolve ISABELLE_HOME_USER; heap image GC disabled")
                self._home_user = False
        return self._home_user or None

    def _isabelle_home(self) -> Optional[Path]:
        """ISABELLE_HOME (the distribution root), same lazy resolution."""
        if self._home is None:
            try:
                out = subprocess.run(
                    [self.isabelle, "getenv", "-b", "ISABELLE_HOME"],
                    capture_output=True, text=True, timeout=30,
                )
                value = out.stdout.strip()
                self._home = Path(value) if value else False
            except Exception:
                logger.exception("failed to resolve ISABELLE_HOME; distribution heaps hidden")
                self._home = False
        return self._home or None

    # ----------------------------------------------------- available images

    def list_available_heaps(self) -> List[Dict[str, Any]]:
        """Every heap image on disk, for the admin surface.

        Base session images (user-built, e.g. HOL-Analysis, and distribution
        ones) live as single files at ``<home>/heaps/<platform>/<session>``;
        pool-built images live in the same layout and are tagged ``pool`` via
        the registry so the admin can tell them apart. A session present in
        both homes is listed per location (Isabelle's resolution order is
        user home first).
        """
        pool_sessions = {e.get("session_name") for e in self._entries.values()}
        found: List[Dict[str, Any]] = []
        for home, origin in (
            (self._isabelle_home_user(), "user"),
            (self._isabelle_home(), "distribution"),
        ):
            if home is None:
                continue
            heaps = home / "heaps"
            if not heaps.is_dir():
                continue
            for platform_dir in sorted(heaps.iterdir()):
                if not platform_dir.is_dir() or platform_dir.name == "log":
                    continue
                for img in sorted(platform_dir.iterdir()):
                    if img.name == "log":
                        continue
                    if img.is_file():
                        size = img.stat().st_size
                    elif img.is_dir():
                        size = sum(
                            f.stat().st_size for f in img.rglob("*") if f.is_file()
                        )
                    else:
                        continue
                    session = img.name
                    found.append(
                        {
                            "session": session,
                            "platform": platform_dir.name,
                            "size_mb": round(size / (1024 * 1024), 1),
                            "modified": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(img.stat().st_mtime)
                            ),
                            "origin": "pool" if session in pool_sessions else origin,
                            "path": str(img),
                        }
                    )
        return found

    def _gc_heap_image(self, session_name: str) -> None:
        """Remove the on-disk heap image + build logs for session_name, but only
        when no remaining pool entry references it (two groups can share a
        session name). Distribution/session heaps (HOL etc.) are never matched
        because they are never pool entries."""
        if not Heap.GC_IMAGES:
            return
        if any(e.get("session_name") == session_name for e in self._entries.values()):
            logger.info("heap image %s still referenced by another entry — kept", session_name)
            return
        home = self._isabelle_home_user()
        if home is None:
            return
        heaps = home / "heaps"
        for img in heaps.glob(f"*/{session_name}"):
            # Poly/ML heap images are single FILES at heaps/<platform>/<session>
            # (older layouts may use a directory — handle both).
            if img.is_dir():
                shutil.rmtree(img, ignore_errors=True)
                logger.info("GC'd heap image dir %s", img)
            elif img.is_file():
                img.unlink(missing_ok=True)
                logger.info("GC'd heap image %s", img)
        for logf in heaps.glob(f"*/log/{session_name}.*"):
            logf.unlink(missing_ok=True)

    def delete(self, task_group: str, project: str) -> bool:
        key = (task_group, project)
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        try:
            self._manifest_path(task_group, project).unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to delete manifest for %s:%s", task_group, project)
        root_dir = Path(entry.get("root_dir", ""))
        if root_dir != Path(project) and root_dir.is_dir():
            try:
                (root_dir / "ROOT").unlink(missing_ok=True)
                root_dir.rmdir()
            except OSError:
                logger.exception("failed to remove scratch ROOT %s", root_dir)
        self._gc_heap_image(entry.get("session_name", ""))
        return True

    def delete_group(self, task_group: str) -> int:
        """Delete all of a group's heap records. Live sessions keep their loaded
        heaps; new creation against the group is blocked (entries are gone)."""
        projects = [p for (g, p) in list(self._entries) if g == task_group]
        for p in projects:
            self.delete(task_group, p)
        return len(projects)
