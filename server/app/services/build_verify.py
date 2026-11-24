from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from server.app.core.logging import get_logger
from server.app.core.config import RegularExp
from server.app.services.internal_models import BigStepExecuteResult

try:
    from server.app.services.unicode_normaliser import normalise_for_isabelle
except ImportError:
    normalise_for_isabelle = None

from server.app.services import build_process
from server.app.services.theory_parsing import extract_theory_name, parse_theory_header

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Safety net: strip transient ML_val commands. Until 2026-09-30 the state
# probes (subgoals / facts / sledgehammer) were `ML_val` inserts that could
# leak into a saved proof when their discard was missed; they are overlay
# queries now and never touch the document, so this only guards text that
# a client assembled from older sessions. Harmless no-ops in jEdit, but
# `isabelle build` rejects them.
# ---------------------------------------------------------------------------
_MLVAL_RE = re.compile(r'^ML_val\b.*\n?', flags=re.MULTILINE)


def _strip_mlval(text: str) -> str:
    return _MLVAL_RE.sub("", text)


def _inline_normalise(text: str) -> str:
    """Inline fallback Unicode → Isabelle \<name> normalisation."""
    _FALLBACK = {
        "\u2200": "\\<forall>", "\u2203": "\\<exists>",
        "\u2227": "\\<and>", "\u2228": "\\<or>",
        "\u00ac": "\\<not>", "\u21d2": "\\<Rightarrow>",
        "\u2261": "\\<equiv>", "\u2260": "\\<noteq>",
        "\u2208": "\\<in>", "\u2286": "\\<subseteq>",
        "\u222a": "\\<union>", "\u2229": "\\<inter>",
        "\u2264": "\\<le>", "\u2265": "\\<ge>",
        "\u03bb": "\\<lambda>",
    }
    normalised = text
    for u, esc in _FALLBACK.items():
        normalised = normalised.replace(u, esc)
    # Strip lone surrogates (U+D800–U+DFFF) which are invalid in well-formed UTF-8.
    normalised = "".join(ch for ch in normalised if not (0xD800 <= ord(ch) <= 0xDFFF))
    return normalised


def normalize_theories(theories: Optional[List[str]]) -> List[str]:
    if not theories:
        return []
    out: List[str] = []
    for t in theories:
        if t is None:
            continue
        s = str(t).strip()
        if s:
            out.append(s)
    return sorted(set(out))


def build_dependency_key(field: str, theories: Optional[List[str]]) -> str:
    deps = normalize_theories(theories)
    raw = f"{field}::{'|'.join(deps)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_theory_hash(field: str, dependency_key: str, theory_text: str) -> str:
    raw = f"{field}::{dependency_key}::{theory_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_root(
    root_path: Path,
    session_name: str,
    parent_session: str,
    theory_name: str,
) -> None:
    root_text = f'''session "{session_name}" = "{parent_session}" +
  options [document = false, browser_info = false]
  theories
    "{theory_name}"
'''
    root_path.write_text(root_text, encoding="utf-8")


class BuildVerifier:
    def __init__(
        self,
        *,
        isabelle_bin: str = "isabelle",
        max_concurrent_builds: int = 4,
        build_jobs_per_request: int = 1,
        temp_parent: Optional[Path] = None,
    ) -> None:
        self.isabelle_bin = isabelle_bin
        self.build_jobs_per_request = build_jobs_per_request
        self.temp_parent = temp_parent
        self.max_concurrent_builds = max_concurrent_builds
        self._sem = asyncio.Semaphore(max_concurrent_builds)
        # Builds currently running `isabelle build` (memory facts payload).
        self.inflight = 0
        self._cache: Dict[str, BigStepExecuteResult] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def verify(
        self,
        *,
        theory_name: str,
        theory_text: str,
        dependencies: Optional[List[str]] = None,
        field: Optional[str],
        timeout: float,
        admit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> BigStepExecuteResult:
        """Verify ``theory_text``; ``admit`` (the memory gate) is awaited only on
        a cache miss, right before a build process would start — an already
        verified theory is served from the cache even under memory pressure."""
        parent_session = (field or "HOL").strip() or "HOL"
        if dependencies:
            deps = normalize_theories(dependencies)
        else:
            deps = normalize_theories(self.extract_imports(theory_text))
        dependency_key = build_dependency_key(parent_session, deps)
        theory_hash = stable_theory_hash(parent_session, dependency_key, theory_text)

        cached = self._cache.get(theory_hash)
        if cached is not None:
            return cached.model_copy(update={"execution_time": 0.0})

        lock = self._locks.setdefault(theory_hash, asyncio.Lock())
        async with lock:
            cached = self._cache.get(theory_hash)
            if cached is not None:
                return cached.model_copy(update={"execution_time": 0.0})

            if admit is not None:
                await admit()
            async with self._sem:
                self.inflight += 1
                try:
                    result = await self._run_build(
                        theory_name=theory_name,
                        theory_text=theory_text,
                        parent_session=parent_session,
                        dependency_key=dependency_key,
                        timeout=timeout,
                    )
                finally:
                    self.inflight -= 1

            if result.success:
                self._cache[theory_hash] = result
            return result

    def extract_imports(self, text: str) -> list[str]:
        # Canonical parse (theory_parsing): comment-stripped and header-anchored,
        # so (* TASK: ... *)-style leading comments cannot pollute the imports.
        _, imports = parse_theory_header(text)
        return sorted(set(imports)) or ["Main"]

    async def _run_build(
        self,
        *,
        theory_name: str,
        theory_text: str,
        parent_session: str,
        dependency_key: str,
        timeout: float,
    ) -> BigStepExecuteResult:
        start = time.perf_counter()

        declared = extract_theory_name(theory_text)
        if declared is None:
            return BigStepExecuteResult(
                success=False,
                error="Could not extract theory name from submitted theory text.",
                execution_time=0.0,
                mode="build_strict",
                theory_verified=False,
            )
        if declared != theory_name:
            return BigStepExecuteResult(
                success=False,
                error=f"Declared theory name '{declared}' does not match request theory_name '{theory_name}'.",
                execution_time=0.0,
                mode="build_strict",
                theory_verified=False,
            )

        temp_ctx = tempfile.TemporaryDirectory(
            prefix="isabelle-build-server-",
            dir=str(self.temp_parent) if self.temp_parent else None,
        )

        try:
            temp_dir = Path(temp_ctx.name)
            # Normalise Unicode symbols → Isabelle \<name> escapes before writing to disk.
            # Strip transient ML_val commands that may have leaked from probes
            normalised = _strip_mlval(theory_text)
            try:
                if normalise_for_isabelle is not None:
                    normalised = normalise_for_isabelle(normalised)
                    if normalised != theory_text:
                        logger.info("unicode_normaliser: symbols were normalised (input=%d output=%d chars)",
                                    len(theory_text), len(normalised))
                # Inline fallback is disabled — testing symbol table only.
                # else:
                #     normalised = _inline_normalise(theory_text)
            except Exception:
                logger.exception("unicode_normaliser failed — using raw theory text")
            (temp_dir / f"{theory_name}.thy").write_text(normalised, encoding="utf-8")

            session_name = f"Build_{dependency_key[:10]}_{hashlib.sha256(theory_text.encode()).hexdigest()[:10]}"
            write_root(
                temp_dir / "ROOT",
                session_name=session_name,
                parent_session=parent_session,
                theory_name=theory_name,
            )

            cmd = [
                self.isabelle_bin,
                "build",
                "-D",
                str(temp_dir),
                "-j",
                str(self.build_jobs_per_request),
                "-o",
                "document=false",
                "-o",
                "browser_info=false",
                session_name,
            ]

            logger.info("running isabelle build session=%s parent_session=%s", session_name, parent_session)
            proc = await asyncio.create_subprocess_exec(
                *build_process.scoped(cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return BigStepExecuteResult(
                    success=False,
                    error=f"isabelle build timed out after {timeout:.1f}s",
                    execution_time=time.perf_counter() - start,
                    mode="build_strict",
                    theory_verified=False,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            ok = proc.returncode == 0

            return BigStepExecuteResult(
                success=ok,
                output=(stdout[-4000:] or None),
                error=None if ok else (
                    stderr[-4000:] or stdout[-4000:] or f"isabelle build failed with exit code {proc.returncode}"
                ),
                execution_time=time.perf_counter() - start,
                mode="build_strict",
                theory_verified=ok,
            )
        finally:
            temp_ctx.cleanup()
