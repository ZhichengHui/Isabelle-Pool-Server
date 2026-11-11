"""Gateway JVM leak probe: create and end session backends over and over in one
gateway JVM and measure what the JVM keeps afterwards.

Runs against any checkout (also one without the fixes), so the same script gives
the before/after comparison:

    ISABELLE_HOME=... python -m tests.gateway_leak_probe --cycles 10 --out report.json

It starts its OWN gateway JVM (never the pool server's). Scenarios, one backend
per cycle each:

  normal      start a HOL session with a small theory, then exit() it
  start_fail  Session_Start fails (undefined session)
  load_fail   the session starts, its initial use_theories fails (missing file)
  abandon     the backend is created and the caller gives up on it, as the server
              does after a creation timeout: on a gateway with tagged backends it
              calls close_backend(tag), on an older one it just drops it
  prover_killed  the session's poly is killed (SIGKILL), then the backend exit()s:
              Session.stop completes, Session_Stop reports the nonzero return code
              as an error; this must not count as a failed stop
  stop_fail   the session's Isabelle.Session.manager thread is stopped first (what
              an OutOfMemoryError in that thread does), then the backend exit()s

After every cycle: jcmd GC.class_histogram (a full GC) for the live instances of
the classes that make up a session, GC.heap_info for the heap in use, the JVM's
thread count, and the poly processes under the gateway.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCENARIOS = ("normal", "start_fail", "load_fail", "abandon", "prover_killed", "stop_fail")

# Classes whose live instance counts show a retained session.
TRACKED = {
    "isabelle.Headless$Session": "sessions",
    "isabelle.Server": "servers",
    "repl.Session_Manager": "managers",
    "repl.ReplBackend": "backends",
    "isabelle.Prover": "provers",
    "isabelle.Command": "commands",
}

PROBE_THEORY = "Leak_Probe"


def _write_probe_theory(directory: Path, lemmas: int) -> Path:
    body = "\n".join(
        f'lemma probe_{i}: "rev (rev (xs :: nat list)) @ [{i}] = xs @ [{i}]" by simp'
        for i in range(lemmas)
    )
    path = directory / f"{PROBE_THEORY}.thy"
    path.write_text(f"theory {PROBE_THEORY}\n  imports Main\nbegin\n\n{body}\n\nend\n")
    return path


# ---------------------------------------------------------------------------
# JVM measurements


def _children(pid: int) -> List[int]:
    out: List[int] = []
    for task in Path(f"/proc/{pid}/task").glob("*"):
        try:
            out += [int(c) for c in (task / "children").read_text().split()]
        except OSError:
            pass
    return out


def _descendants(pid: int) -> List[int]:
    todo, seen = [pid], []
    while todo:
        p = todo.pop()
        for c in _children(p):
            if c not in seen:
                seen.append(c)
                todo.append(c)
    return seen


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def find_java_pid(root: int) -> int:
    for p in _descendants(root):
        if _comm(p) == "java":
            return p
    raise RuntimeError(f"no java process under {root}")


def count_poly(root: int) -> int:
    return sum(1 for p in _descendants(root) if _comm(p) == "poly")


def _rss_mb(pid: int) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


class Jcmd:
    def __init__(self, isabelle: Path, pid: int):
        jdk = subprocess.run(
            [str(isabelle), "getenv", "-b", "ISABELLE_JDK_HOME"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.jcmd = str(Path(jdk) / "bin" / "jcmd")
        self.pid = pid

    def run(self, *args: str) -> str:
        return subprocess.run(
            [self.jcmd, str(self.pid), *args],
            capture_output=True, text=True, check=True, timeout=300,
        ).stdout

    def histogram(self) -> Dict[str, int]:
        """Live instances (GC.class_histogram runs a full GC first)."""
        counts = {k: 0 for k in TRACKED.values()}
        total = 0
        for line in self.run("GC.class_histogram").splitlines():
            m = re.match(r"\s*\d+:\s+(\d+)\s+(\d+)\s+(\S+)", line)
            if m:
                name = m.group(3)
                if name in TRACKED:
                    counts[TRACKED[name]] = int(m.group(1))
            m = re.match(r"Total\s+(\d+)\s+(\d+)", line.strip())
            if m:
                total = int(m.group(2))
        counts["live_mb"] = round(total / 2**20, 1)
        return counts

    def heap_used_mb(self) -> float:
        text = self.run("GC.heap_info")
        m = re.search(r"used\s+(\d+)([KMG])", text)
        if not m:
            return -1.0
        scale = {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]
        return round(int(m.group(1)) * scale, 1)

    def threads(self) -> int:
        return sum(1 for line in self.run("Thread.print").splitlines() if line.startswith('"'))


# ---------------------------------------------------------------------------
# Scenarios


class Probe:
    def __init__(self, theory_dir: Path):
        from server.repl.src.python import repl_backend_gateway as gw_mod

        self.gw_mod = gw_mod
        self.gw = gw_mod.ReplBackendGatewayProcess()
        self.jvm = self.gw.gateway.jvm
        self.java_pid = find_java_pid(self.gw.process.pid)
        self.jcmd = Jcmd(gw_mod.isabelle_executable, self.java_pid)
        self.theory = str(theory_dir / PROBE_THEORY)
        self.tagged = self._has("get_repl_backend_tagged")
        self.seq = 0
        self.jvm_log = gw_mod._gateway_log_dir() / "gateway-jvm.log"
        self.jvm_log_offset = self.jvm_log.stat().st_size if self.jvm_log.exists() else 0

    def _has(self, method: str) -> bool:
        cls = self.jvm.java.lang.Class.forName("repl.ReplBackendGateway")
        return any(m.getName() == method for m in cls.getMethods())

    def _jlist(self, items: List[str]):
        lst = self.jvm.java.util.ArrayList()
        for i in items:
            lst.add(i)
        return lst

    def _create(self, theories: List[str], field: str = "HOL", tag: Optional[str] = None):
        args = (True, False, 10, self._jlist(theories), field, self._jlist([]))
        if tag is not None:
            return self.jvm.repl.ReplBackendGateway.get_repl_backend_tagged(tag, *args)
        return self.jvm.repl.ReplBackendGateway.get_repl_backend_with_initial_theories(*args)

    def _expect_failure(self, theories: List[str], field: str) -> str:
        try:
            backend = self._create(theories, field)
        except Exception as e:  # Py4JJavaError
            return type(e).__name__
        backend.exit()
        raise AssertionError("backend creation was expected to fail")

    def _field(self, obj, cls_name: str, name: str):
        # Scala may compile a private member under a mangled name (pkg$Cls$$name).
        fields = self.jvm.java.lang.Class.forName(cls_name).getDeclaredFields()
        for f in fields:
            if f.getName() == name or f.getName().endswith("$$" + name):
                f.setAccessible(True)
                return f.get(obj)
        raise AttributeError(f"{cls_name} has no field {name}")

    def _kill_manager(self, backend) -> None:
        """Stop Isabelle.Session.manager of the backend's session: afterwards
        Session.stop() fails with "Consumer thread not active", as after an
        OutOfMemoryError in that thread."""
        repl_session = self._field(backend, "repl.ReplBackend", "repl_session")
        session_data = self._field(repl_session, "repl.Repl_Session", "session_data")
        session = session_data.session()
        manager = self._field(session, "isabelle.Session", "isabelle$Session$$manager")
        manager.shutdown()

    def cycle(self, scenario: str) -> Dict[str, Any]:
        self.seq += 1
        note = ""
        if scenario == "normal":
            backend = self._create([self.theory])
            backend.exit()
            del backend
        elif scenario == "start_fail":
            note = self._expect_failure([self.theory], "No_Such_Session_Leak_Probe")
        elif scenario == "load_fail":
            note = self._expect_failure(["/nonexistent/leak_probe/Missing_Theory"], "HOL")
        elif scenario == "abandon":
            if self.tagged:
                tag = f"leak-probe-{self.seq}"
                backend = self._create([self.theory], tag=tag)
                del backend
                note = f"close_backend={self.jvm.repl.ReplBackendGateway.close_backend(tag)}"
            else:
                backend = self._create([self.theory])
                del backend
                note = "dropped"
        elif scenario == "prover_killed":
            backend = self._create([self.theory])
            for pid in _descendants(self.java_pid):
                if _comm(pid) == "poly":
                    os.kill(pid, signal.SIGKILL)
            time.sleep(3.0)
            t0 = time.time()
            try:
                backend.exit()
                note = f"exit ok {time.time() - t0:.1f}s"
            except Exception as e:
                note = f"exit raised {type(e).__name__} {time.time() - t0:.1f}s"
            del backend
        elif scenario == "stop_fail":
            backend = self._create([self.theory])
            self._kill_manager(backend)
            try:
                backend.exit()
                note = "exit ok"
            except Exception as e:
                note = f"exit raised {type(e).__name__}"
            del backend
        else:
            raise ValueError(scenario)
        return {"note": note}

    def measure(self) -> Dict[str, Any]:
        gc.collect()  # drop Python proxies, so Py4J releases the Java objects
        time.sleep(2.0)  # Py4J dereference + asynchronous session removals
        m: Dict[str, Any] = self.jcmd.histogram()
        m["heap_used_mb"] = self.jcmd.heap_used_mb()
        m["threads"] = self.jcmd.threads()
        m["poly"] = count_poly(self.gw.process.pid)
        m["rss_mb"] = round(_rss_mb(self.java_pid), 1)
        m["forced_teardowns"] = self._new_log().count("forced teardown")
        return m

    def _new_log(self) -> str:
        """gateway-jvm.log text written since the previous call."""
        try:
            with open(self.jvm_log, "rb") as f:
                f.seek(self.jvm_log_offset)
                data = f.read()
        except OSError:
            return ""
        self.jvm_log_offset += len(data)
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        self.gw.terminate()


def run(cycles: int, scenarios: List[str], lemmas: int, settle: float) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="leak_probe_") as d:
        _write_probe_theory(Path(d), lemmas)
        probe = Probe(Path(d))
        report: Dict[str, Any] = {
            "tagged_gateway": probe.tagged, "java_pid": probe.java_pid,
            "cycles": cycles, "lemmas": lemmas, "steps": [],
        }
        try:
            probe.cycle("normal")  # warm up: classes loaded, HOL heap mapped once
            time.sleep(settle)
            report["baseline"] = probe.measure()
            print("baseline", json.dumps(report["baseline"]), flush=True)
            for scenario in scenarios:
                for i in range(cycles):
                    t0 = time.time()
                    step = probe.cycle(scenario)
                    time.sleep(settle)
                    step.update(scenario=scenario, cycle=i + 1,
                                seconds=round(time.time() - t0, 1), **probe.measure())
                    report["steps"].append(step)
                    print(json.dumps(step), flush=True)
            report["fatal_error"] = (
                probe.jvm.repl.ReplBackendGateway.fatal_error()
                if probe._has("fatal_error") else None
            )
            report["alive"] = bool(probe.jvm.repl.ReplBackendGateway.alive())
        finally:
            probe.close()
        return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--lemmas", type=int, default=200)
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds to wait after a cycle before measuring")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    report = run(args.cycles, args.scenarios.split(","), args.lemmas, args.settle)
    text = json.dumps(report, indent=1)
    if args.out:
        args.out.write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
