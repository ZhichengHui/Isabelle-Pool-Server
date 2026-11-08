# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Universal Workflow Rules

These rules apply to every user request:

### Rule 1: Plan or research before implementing
For every user request, start with planning, research, or investigation **unless the user explicitly says "implement"**. Do not make core code edits, file modifications, or destructive changes before a plan or research phase has been completed and communicated.

Examples:
- "Fix this bug" → investigate first, then propose a fix.
- "Add a feature" → plan first.
- "How does X work?" → research and explain.
- "Implement X" → proceed directly to implementation.

### Rule 2: Ask before choosing among options
If you present multiple approaches or options to the user, **always ask which one they want before implementing**. Do not proceed with one option on your own just because you prefer it. Wait for the user's explicit choice.

### Rule 3: Sign-off format
End every response with the exact string:

喵(ゝ∀･)⌒☆

## Claude Work Artifacts (read first)

The **testing and demonstration** artifacts Claude produces to show a task is done — ad-hoc test scripts, notebook runners, and the markdown write-up of what was verified — go under `claude-work/`, in a subfolder named for the task (one per feature or bug fix, date-prefixed, e.g. `claude-work/<date>-impl-<feature>/`). Use `impl-<feature>` for features and a short descriptive kebab-case name for bug fixes; include a brief `NOTES.md` summarizing the change and how it was verified. Keep these for reference rather than deleting them.

This is only for the demonstration/test artifacts — not every file touched during the task. Real source changes (e.g. `server/repl/src/ml/REPL.ML`, `examples/demo.ipynb`, files under `server/`, `client/`) stay in their normal locations.

## Project Overview

**Isabelle Pool Server** is a containerized system for machine learning research on formal theorem proving. It provides a RESTful API to interact with the Isabelle theorem prover (Isabelle2026, selected by the `ISABELLE_VERSION` build arg in `deploy/Dockerfile`; the 2026 release candidates also build, Isabelle2025-2 and earlier do not (the PIDE header API changed in 2026-RC1)) through a three-tier architecture:

1. **Scala/ML backend** (server/repl/) - Isabelle REPL wrapping via Scala/ML, exposing interactive proof state operations
2. **FastAPI server** (server/) - RESTful HTTP service with session pooling, resource management, and lease-based concurrency
3. **Python async client** (client/) - User-facing SDK for small-step (interactive) and big-step (batch) proof verification

**Core purpose**: Isabelle as a managed, concurrent service (the Isabelle counterpart of Kimina Lean Server). Three uses on one core: a training/evaluation environment for LLM provers (small-step with checkpoints/rollback, chunk, big-step), a verification service for single theories or batch workloads (batch loop is client/MCP-side — no server job queue), and a platform for many concurrent agents (MCP servers; agents are isolated, not coordinated; no authentication — trusted network only). Known as IsabelleGym 3.0 until 2026-10-05.

## Quick Start (Docker Recommended)

```bash
# Configure (.env from .env.example), build, start, health-check in one go:
./deploy/setup.sh            # --verify for a smoke test, --build-heaps "HOL-Library" to prebuild heaps

# Or by hand:
cp .env.example .env
docker compose build isabelle-pool-server
docker compose up -d isabelle-pool-server   # the entrypoint registers components and STARTS the server

# From host, verify server is healthy (allow 1-2 min for the gateway JVM)
curl http://localhost:8000/healthz   # {"status":"alive"}
curl http://localhost:8000/

# A shell in the running container (tests, isabelle build, ...)
docker compose exec isabelle-pool-server bash
```

Expected `GET /` response:
```json
{
  "service": "Isabelle Pool Server",
  "version": "0.1.0",
  "status": "healthy",
  "gateway_alive": true,
  "active_sessions": 0,
  "busy_sessions": 0,
  "max_pool_size": 3,
  "max_concurrent_sledgehammer": 1,
  "memory_management_enabled": true,
  "memory_used_mb": 0, "memory_limit_mb": 0, "memory_pressure_pct": 0,
  "timestamp": "..."
}
```
(`max_pool_size` echoes `ISABELLE_POOL_SIZE`; `.env.example` sets 3, the code default is 24.)

## Architecture & Key Components

### Backend Layer: server/repl/ (Scala/ML)

The REPL backend bridges Python ↔ Scala ↔ Isabelle/ML. It uses **Py4J** for Java/Python interop and exposes Isabelle's interactive proof state through a custom Scala gateway.

**Key files:**
- server/repl/src/main/scala/repl/repl_backend_gateway.scala - Entry point for Py4J gateway; exposes ReplBackendGateway object with factory methods for creating REPL backends with various configurations (caching, memory management, field selection)
- server/repl/src/main/scala/repl/repl_backend.scala - Core backend class managing Isabelle sessions, state, and proof operations
- server/repl/src/main/scala/repl/server_utils.scala - Low-level Isabelle server/session creation utilities
- server/repl/src/main/scala/repl/backend_probes.scala - Read-only state queries (subgoals, in-proof, facts, sledgehammer, proof state) run as PIDE OVERLAY queries on the document's last command via Document_Utils.overlay_query — no document edits, no ML→Scala channels (since 2026-09-30); probe_transient (POST /diagnostic) is the one insertion-based probe left
- server/repl/src/ml/REPL.ML - ML-side Query_Operation registrations (isabelle_pool_server_goals / in_proof / local_facts / global_facts / state / sledgehammer) plus the extraction functions they call (NOTE: path is src/ml/, not src/main/ml/)
- server/repl/src/main/scala/repl/thy_*.scala - Theory parsing, status tracking, and checkpoint utilities
- server/repl/src/python/repl_backend_gateway.py - Python wrapper (ReplBackendGatewayProcess) that spawns the Scala gateway as a subprocess and manages the Py4J bridge
- server/repl/etc/build.props - Isabelle component build description: `isabelle scala_build` compiles the backend into server/repl/lib/repl.jar (run automatically by `isabelle scala` at gateway start; the Dockerfile and native_setup.sh run it eagerly). server/repl/build.gradle is IDE-only.

**Build process:** Isabelle builds the component itself:
```bash
isabelle scala_build   # compiles server/repl -> lib/repl.jar; also runs implicitly at every gateway start
```

### Server Layer: server/ (FastAPI)

FastAPI application providing HTTP endpoints for session management (create, acquire, release) and proof execution (small-step command execution, big-step theory verification).

**Architecture pattern: Lease-based concurrency**
- Sessions can be held by multiple clients via leases (X-Lease-Id header)
- Automatic idle timeout and cleanup
- LRU eviction when pool is exhausted

**Key files:**
- server/app/main.py - FastAPI app setup, middleware (request logging, error handlers), lifespan (startup/shutdown with session manager)
- server/app/api/v1/routes/*.py - HTTP endpoints, one module per concern (health, sessions, execution, inspection, positional, automation, checkpoints, heaps, memory, fill — `POST/GET/DELETE /api/v1/sessions/{id}/fill_sorries`, the server-side sledgehammer job on every `sorry` of a submitted proof); server/app/api/v1/router.py aggregates them and re-exports names for compatibility; api/v1/deps.py holds the `LeasedSession` dependency (lease check + session resolution + log context) and the admin-token / path-segment guards; api/v1/serializers.py holds response-shaping helpers. Source files are capped at 600 lines by tests/test_source_limits.py.
- server/app/api/v1/schemas/API_models.py - Pydantic models for requests/responses
- server/app/services/session_manager.py - Session pool lifecycle, LRU eviction, lease management using OrderedDict for O(1) eviction
- server/app/services/session.py - Per-session state, small-step command execution, checkpoints, rollback
- server/app/services/build_verify.py - Big-step verification via isabelle build CLI
- server/app/services/threaded_backend.py - Thread pool wrapper for concurrent backend operations
- server/app/core/config.py - Configuration via environment variables (pool size, timeouts, caching, logging, field selection)

### Client Layer: client/ (Async Python)

Async HTTP client for end-users. Provides high-level workflows for session creation, command execution, and theory verification.

**Key file:**
- client/async_client.py - PoolAsyncClient class with methods:
  - Session lifecycle: create_session(), acquire_session(), release_session(), close_session()
  - Small-step: execute_command(), enter_theory(), get_proof_state()
  - Automation: sledgehammer() - runs Isabelle's sledgehammer on the current proof goal; returns success/suggestions/raw_output/execution_time plus outcome (`proved` | `none` | `timeout` | `error`) and method — `success` is true only when a proof was found ("No proof found" used to count as success; changed 2026-10-18)
  - Fill job: fill_sorries() starts the server-side sledgehammer job on every `sorry` of a text (202; per-sorry results, ETA, `patched_text`), fill_status() polls it, fill_cancel() stops it
  - Big-step: verify_bigstep_text(), verify_bigstep_file()
  - Checkpointing: checkpoint_session(), rollback_to_checkpoint()

### Evaluation & Benchmarking: evaluation/

Suite of benchmarking scripts comparing server performance against local IsabelleGym 2.0 baseline and qIsabelle.

**Scripts in evaluation/scripts/:**
- process.py - Preprocessing: normalize Analysis-style imports to fully qualified HOL-Analysis.<Theory> imports
- clean_example_dir.py - Remove documentation keywords (text, section, subsection) from theory files
- eval_smallstep_isabelle-pool-server.py - Local baseline (IsabelleGym 2.0)
- eval_smallstep_server_client_1_worker_no_reuse.py - Server small-step with fresh session per theory
- eval_smallstep_server_client_with_reuse.py - Server small-step with pooled session reuse and parallel workers
- eval_smallstep_qisabelle.py - qIsabelle comparison
- eval_bigstep_isabelle_build.py - Local isabelle build baseline
- eval_bigstep_server_client_ver.py - Server big-step verification
- eval_stats.py, theory_splitter.py - Helper modules for stats and theory splitting

## Development Workflow

### Environment Setup (Local, non-Docker)

**Local Python lives in the conda environment named `IsabelleGym`** (conda rejects spaces in
environment names, so the project-name spelling cannot exist). There is no bare `python` on
PATH on the host — activate it first (`conda activate IsabelleGym`, or run one-off commands
with `conda run -n IsabelleGym python ...`). Inside the
Docker container, Python is the image interpreter at `/usr/local/bin/python`
(invoke with `docker exec isabelle-pool-server python ...`), not a conda env.

```bash
# Create and activate venv
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install dependencies — pyproject.toml is the single list (extras: mcp, eval, dev)
pip install --upgrade pip
pip install -e ".[mcp,dev]" -e ./client

# Set PYTHONPATH for imports
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
```

**Requirements:**
- Python 3.12+ (3.10+ for local dev, Docker uses 3.12)
- No JDK: Isabelle bundles its own (a foreign `JAVA_HOME` breaks it)
- Isabelle2026 (the version the Dockerfile builds; 2025-2 and earlier do not build this backend) installed and on PATH as `isabelle`
- Scala 3 (compiled by Isabelle's bundled toolchain)

### Building & Testing

**Scala/REPL build:**
```bash
isabelle scala_build        # after changes under server/repl/src/, then restart the server
```

**Python linting & formatting (dev dependencies):**
```bash
pylint server client mcp_servers evaluation  # Lint (ignores venv, import-error, line-too-long)
black server client mcp_servers evaluation   # Format (line-length=88)
isort server client mcp_servers evaluation   # Sort imports (profile=black)
mypy server client mcp_servers evaluation    # Type check (strict=true, disable import-untyped)
```

**Tests** (`tests/`, ~380 unit tests, no Isabelle needed — they stub the backend/HTTP layer;
run from the repo root, on the host with `conda run -n IsabelleGym pytest` or inside the
container with `docker exec isabelle-pool-server python -m pytest` — the image ships the runtime
and `mcp` dependencies only, so run `docker exec isabelle-pool-server pip install pytest` once per
container first):
```bash
pytest                       # whole suite
pytest tests/test_mcp_lsp_server.py -q
```
Two of them are repository gates, not feature tests: `tests/test_dependency_rules.py`
(package import direction) and `tests/test_source_limits.py` (600-line cap on source
files, with a ratcheting allow-list). Live/integration smoke scripts against a running
server live under `claude-work/<task>/` and are not collected.

### Running the Server Locally

```bash
# Terminal 1: Start the server
python -m server.app.main
# or explicitly with Uvicorn:
uvicorn server.app.main:app --host 0.0.0.0 --port 8000

# Terminal 2: Run a client example
python -c "
import asyncio
from client.async_client import PoolAsyncClient

async def main():
    async with PoolAsyncClient('http://localhost:8000') as client:
        created = await client.create_session(theories=['Main'], field='HOL')
        print(f'Session: {created[\"session_id\"]}')
        await client.close_session(created['session_id'])

asyncio.run(main())
"
```

### Environment Variables for Server Configuration

Set before starting the server:

```bash
# Pool and concurrency
ISABELLE_POOL_SIZE=24              # Max concurrent sessions (default 24)
ISABELLE_INITIAL_SESSIONS=8        # Pre-warm sessions on startup (default 3)
ISABELLE_IDLE_TIMEOUT=1800         # Session idle timeout in seconds (default 1800)
ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER=4  # Sledgehammer slots at memory tier `ok` (default ~cores/8); prevents
                                   # gateway OOM. The counter is tier-aware (services/sledgehammer_slots.py):
                                   # warning -> 1 slot, critical/external -> 0; fill-job steps park until the
                                   # tier drops, interactive /sledgehammer calls are refused (503) at critical.

# fill_sorries job (POST /api/v1/sessions/{id}/fill_sorries; services/fill_sorries.py)
ISABELLE_FILL_TIMEOUT_S=30         # Default per-sorry sledgehammer budget (s)
ISABELLE_FILL_MAX_TOTAL_S=600      # Default wall budget of one job (s), parking included; the rest is skipped
ISABELLE_FILL_PARK_POLL_S=5        # A parked step re-checks the tier this often (s)
ISABELLE_FILL_SAMPLE_S=2           # ATP memory attribution sample interval (s) while a step runs
# LSP MCP wrapper (mcp_servers/lsp/config.py): ISABELLE_MCP_LSP_FILL_TIMEOUT_S=30,
# ISABELLE_MCP_LSP_FILL_MAX_TOTAL_S=600, ISABELLE_MCP_LSP_FILL_PATIENCE_S=240 (isabelle_fill_sorries
# returns the partial state with continuing: true after this; keep it <= the MCP client's tool
# timeout - 60), ISABELLE_MCP_LSP_FILL_POLL_S=3; ISABELLE_MCP_LSP_FIELD_FROM_IMPORTS=false (bindings and
# scratch sessions start on the image the server's parse suggests for the dotted imports, suggested_field;
# an image the server lacks is built on first use)

# Concurrent session creation (server/app/core/config.py, server/app/services/session_manager_helpers.py)
ISABELLE_PARALLEL_SESSION_CREATE=true  # Default true: the gateway lock is held only to read the gateway, so N
                                   # sessions start concurrently (4 at once: HOL 17.7 s -> 8.2 s, AutoCorrode
                                   # 101.8 s -> 42.5 s). false restores one-at-a-time creation.
ISABELLE_SESSION_START_BUILD=true  # Default true: a session image that is missing or not up to date is built
                                   # during session start, inside the request (minutes; outside the build
                                   # gates). false: session start fails instead, naming the image and the
                                   # command that lists the differing sources (server_utils.scala). An image
                                   # copied to another HOME counts as outdated when ROOTS/components paths
                                   # differ; check images with `isabelle build -n -v` (without -v, -n is silent).

# Per-session parallel proof checking (server/repl/src/main/scala/repl/session_manager.scala, read by Scala via sys.env)
ISABELLE_PARALLEL_PROOFS=2         # Isabelle parallel_proofs per session (default 2): 0=sequential,
                                   # 1=fork top-level proofs, 2=also fork nested have/show bodies.
                                   # VERIFIED effective: ~8x on independent structured proofs.
ISABELLE_SESSION_THREADS=4         # Isabelle `threads` per session (default 4). NOTE: currently a
                                   # no-op -- the poly prover farm is sized from hardware at process
                                   # launch, not from this session option (farm stays ~cores). To
                                   # actually cap per-session threads it must be set at the gateway/
                                   # prover launch layer (details in the internal working notes).

# Prover wait budgets (Scala side; every wait on the prover is wall-bounded — ISSUES.md Bug 15)
ISABELLE_REPL_SETTLE_TIMEOUT=60    # Settle budget (s) for waits with no request timeout (rollback, vector_step)
ISABELLE_REPL_SUBGOALS_TIMEOUT=20  # Overlay query budgets (s): subgoals/in_proof/state; also
ISABELLE_REPL_LOCAL_FACTS_TIMEOUT=20        #   LOCAL_FACTS, GLOBAL_FACTS_TIMEOUT_MINUTES (5)
ISABELLE_REPL_MEM_TIMEOUT=5        # Per-session memory probe budget (pid + Poly/ML heap stats)
ISABELLE_TIMEOUT_BACKEND_GRACE=10  # Python-side future timeout = request timeout + this grace,
                                   # so the JVM's own timeout result (with rollback) arrives first

# Caching and memory
ISABELLE_ENABLE_CACHE=false        # Enable session caching (default false)
ISABELLE_MAX_CACHE_SIZE=1          # Max cached sessions per key (default 1)
ISABELLE_ENABLE_MEMORY_MANAGEMENT=true  # Enable Python/cgroup memory management (default true)

# Memory management (Python/cgroup-based; gated by ISABELLE_ENABLE_MEMORY_MANAGEMENT).
# The server measures real container memory via the cgroup (memory.current vs
# memory.max, falling back to host MemTotal when unlimited) and, under pressure,
# evicts idle sessions before admitting new ones — refusing with 503 only if
# nothing idle remains. See server/app/services/memory_monitor.py.
ISABELLE_MEMORY_PRESSURE_THRESHOLD=85.0 # Block new sessions above this used% (default 85.0)
ISABELLE_MEMORY_MIN_AVAILABLE_MB=512    # Also block if available memory below this (default 512)
ISABELLE_MEMORY_WARNING_THRESHOLD=70.0  # X-Memory-State header: warning from this used% (critical = refusing)
ISABELLE_MEMORY_PROBE_MIN_INTERVAL_S=15 # Per-session ML memory probe at most once per interval (request path)
ISABELLE_MEMORY_FACTS_LIST_CAP=50       # Max evictable/leased rows in memory 503 bodies and GET /api/v1/memory
ISABELLE_MEMORY_ERROR_PATTERNS="Out of memory|Run out of store|exception Size raised"  # error_kind="memory" matches
ISABELLE_MEMORY_SESSION_ESTIMATES="HOL=2048,HOL-Library=3072,HOL-Analysis=4096"  # budget admission: new-session size by field
ISABELLE_MEMORY_BUILD_ESTIMATE_MB=4096  # budget admission: one `isabelle build` process
ISABELLE_MEMORY_WARNING_SWEEP_INTERVAL_S=10  # sweep interval while warning/critical (normal: ISABELLE_CLEANUP_INTERVAL)
ISABELLE_MEMORY_FALLBACK_SYSTEM_MB=4096 # Limit used when cgroup + MemTotal unreadable (default 4096)

# Isabelle location (gateway + heap pool resolve $ISABELLE_HOME/bin/isabelle; container default)
ISABELLE_HOME=/opt/isabelle
ISABELLE_HEAP_POOL_DIR=/root/.isabelle/heap_pool      # default is host-aware: ~/.isabelle/heap_pool (= this in the container)
ISABELLE_HEAP_POOL_ALLOWED_ROOTS=/app:/root/.isabelle      # default is host-aware: <repo>:~/.isabelle (= this in the container)

# Proof state and field
ISABELLE_SHOW_STATES=true          # Include raw proof states in responses (default true)
ISABELLE_DEFAULT_FIELD=HOL         # Default field if not specified (default HOL)

# Server and logging
ISABELLE_SERVER_HOST=0.0.0.0       # Bind address (default 0.0.0.0)
ISABELLE_SERVER_PORT=8000          # Listen port (default 8000)
ISABELLE_SERVER_LOG_LEVEL=INFO     # Log level (default INFO)
ISABELLE_SERVER_LOG_DIR=logs       # Log directory (default logs/)
ISABELLE_SERVER_LOG_FILE=server.log # Log file (default server.log)
ISABELLE_SERVER_MAX_LOG_SIZE_BYTES=10485760  # Rotating log size (default 10MB)
ISABELLE_SERVER_LOG_BACKUP_COUNT=5 # Rotating log backups (default 5)
```

Example: start with smaller pool for testing:
```bash
export ISABELLE_POOL_SIZE=4
export ISABELLE_INITIAL_SESSIONS=2
python -m server.app.main
```

### Monitoring (Prometheus / Grafana / cAdvisor)

The server exposes Prometheus metrics and k8s-style health probes (no extra flags):

- `GET /metrics` — Prometheus metrics. HTTP request count/latency histograms (per route
  template, via `prometheus-fastapi-instrumentator`) plus domain metrics defined in
  `server/app/core/metrics.py`: `isabelle_pool_server_sessions_created_total`,
  `isabelle_pool_server_sessions_evicted_total{reason}`, `isabelle_pool_server_pool_exhausted_total{reason}`,
  `isabelle_pool_server_gateway_restarts_total`, `isabelle_pool_server_sledgehammer_{total{result},seconds,inflight}`,
  and current-state gauges `isabelle_pool_server_sessions_{active,busy,leased}`,
  `isabelle_pool_server_memory_{used,limit}_bytes`, `isabelle_pool_server_memory_pressure_pct`,
  `isabelle_pool_server_gateway_up` (from `SessionManager.get_lru_info()`).
- `GET /api/v1/memory` — memory facts for clients: `state` (ok/warning/critical), the snapshot
  (`used_mb`, `limit_mb`, `available_mb`, `pressure_pct`, `source`), `builds_in_flight`,
  `evictable`/`leased` session rows (capped, by RSS) and every session's expiry
  (`idle_expires_at`, `lease_expires_at`, `expires_in_s`) and attributed memory (`rss_mb` of its
  ML process + children, `heap_mb` = Poly/ML `size_heap`). Never lease ids, never advice: the MCP
  words the advice. Every response also carries the `X-Memory-State` header (cached tier, no I/O),
  every 503 body carries `error` (`memory_pressure` with the same facts, `pool_full`,
  `gateway_unavailable`) plus `Retry-After`, and step/chunk/document results carry `error_kind`
  (`memory` | `prover_died` | null). Leased sessions are never evicted (owner decision).
- `GET /healthz` — liveness (always 200 if the process serves).
- `GET /readyz` — readiness: 200 when the gateway is alive, else 503.
- `GET /` — human-readable health summary (sessions, memory, `gateway_alive`).

Bring up the monitoring stack (compose services `prometheus`, `grafana`, `cadvisor`) —
start the server first, then:
```bash
docker compose up -d prometheus grafana cadvisor
# Grafana   http://localhost:3000  (admin/admin) — "Isabelle Pool Server" dashboard
# Prometheus http://localhost:9090/targets       — isabelle-pool-server + cadvisor should be UP
# raw        http://localhost:8000/metrics
```
Config lives under `deploy/monitoring/` (prometheus.yml, Grafana datasource + dashboard
provisioning). `docker-compose.yml` also sets `mem_limit: 14g` on `isabelle-pool-server` so the
memory admission gate has a real cgroup ceiling and cAdvisor can report OOM events.
Note: the compose service runs `bash`; start the server manually (`python -m
server.app.main`) or Prometheus targets show DOWN until it is up.

### Evaluation Workflow

One-time setup:
```bash
cd /path/to/Isabelle-Pool-Server
source .venv/bin/activate
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
mkdir -p evaluation/results
CORPUS="evaluation/HOL_corpus/Examples/processed"  # Small example corpus
OUT="evaluation/results"
```

Small-step baseline (local):
```bash
python -m evaluation.scripts.eval_smallstep_isabelle-pool-server \
  --repo-root . \
  --corpus "$CORPUS" \
  --output "$OUT/smallstep_isabelle_pool_server_aligned.json"
```

Small-step server (session reuse, parallel workers):
```bash
# Start server in another terminal first
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --field HOL \
  --num-workers 4 \
  --output "$OUT/smallstep_server_with_reuse.json"
```

Big-step baseline (local isabelle build):
```bash
python -m evaluation.scripts.eval_bigstep_isabelle_build \
  --corpus "$CORPUS" \
  --isabelle-bin "$(which isabelle)" \
  --parent-session HOL \
  --jobs 4 \
  --output "$OUT/bigstep_isabelle_build.json"
```

Big-step server:
```bash
python -m evaluation.scripts.eval_bigstep_server_client_ver \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --timeout 1800 \
  --field HOL \
  --output "$OUT/bigstep_server_client.json"
```

For HOL-Analysis corpus, use --field HOL-Analysis and adjust import paths via process.py.

## Key Architectural Decisions

### Session Management with OrderedDict-based LRU
The session manager uses Python's OrderedDict (O(1) eviction) instead of sorting by timestamp (O(n)). This is critical for high-concurrency scenarios where many sessions may be created/released rapidly.

### Lease-based Concurrency
Sessions support multiple "leases" (client holds identified by X-Lease-Id header). This allows safe concurrent access without forcing session destruction when a client releases its lease.

### Pooling and Idle Cleanup
The server maintains a pool of warm Isabelle sessions. Idle sessions (older than IDLE_TIMEOUT_SECONDS) are automatically cleaned up by a background task. When the pool is exhausted, new sessions are created on-demand up to POOL_SIZE, then LRU eviction occurs.

### Small-step vs Big-step Verification
- **Small-step** (execute_command): Interactive, maintains session state, allows checkpoints and rollback. Used for step-by-step proof development. The request `timeout` is a JVM wall budget: a command still running when it expires is ROLLED BACK and reported as `success=false` with a timeout error (retry with a larger timeout) — it never keeps running in the background (ISSUES.md Bug 15).
- **Big-step** (verify_bigstep_text): Batch verification via isabelle build, no session reuse, good for whole-theory checking and parallelization.

### Sledgehammer Integration
Sledgehammer is exposed end-to-end as a small-step automation primitive: the `isabelle_pool_server_sledgehammer` Query_Operation in server/repl/src/ml/REPL.ML runs Isabelle's sledgehammer as a PIDE overlay on a host command (Document_Utils.overlay_query), Backend_Probes.sledgehammer hosts it on the document's last command (the LSP-like sledgehammer_at hosts it on the command at a given line — same operation), and the FastAPI endpoint POST /api/v1/sessions/{session_id}/sledgehammer (routes/automation.py, schemas SledgehammerRequest/SledgehammerResponse) runs it on the current proof goal under a lease. Outside a proof it returns no suggestions. No text edit is made, so nothing can leak into the proof script. `success` (and `found` of `/sledgehammer_at`) is true only for a `Try this:` line; both carry `outcome`/`method` from success_checker.classify_sledgehammer. The server-side **fill_sorries job** (merged 2026-10-21 from impl-de-sorry) builds on the same mechanism: the `isabelle_pool_server_sledgehammer_fill` Query_Operation (max_proofs, optional prover list, a `subgoals=N` line) hosted on a command by chunk-report index (Document_Utils.sledgehammer_at_command_json, SessionFillMixin in services/session_memory.py); services/fill_sorries.py runs it sequentially on every `sorry` of a submitted text, patches the proof, reports an ETA, and is started/polled/cancelled via routes/fill.py; services/sledgehammer_slots.py is the one tier-aware counter for interactive sledgehammers and fill steps (`ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER` at tier ok, 1 at warning, 0 at critical — jobs park, interactive calls are refused); services/atp_attribution.py walks the gateway JVM's descendants (the solvers run in their own sessions under `bash_process`) and attributes their RSS to the session whose step runs (`sledgehammer_mb` / `_peak_mb` / `_shared_with` in `GET /api/v1/memory`). The LSP MCP exposes it as `isabelle_fill_sorries` / `isabelle_fill_status` / `isabelle_fill_cancel`. NOTE: the Query_Operation registrations are intentionally at the top level of REPL.ML, outside the `Repl` struct (the struct only holds the extraction functions); see commit ca73379 for the original placement conflict.

### Caching (Optional)
Session caching can be enabled to reuse initialized sessions for the same import dependencies. By default disabled to avoid stale state issues.

## Common Issues & Troubleshooting

**"Bad component..." during Scala/Isabelle compilation**
- Create an empty file under Isabelle's Admin/components path before rebuilding.

**Server starts but commands fail immediately**
- Inside container: verify which isabelle, java -version, python --version are correct.
- Run ./server/repl/Admin/init to ensure Isabelle components are initialized.
- Run `isabelle scala_build` to rebuild the Scala backend (compile errors show there).

**pip install -r requirements.txt fails**
- There is no requirements file: every dependency lives in pyproject.toml. Use `pip install -e ".[mcp,dev]"` (extras `mcp`, `eval`, `dev`).

**Docker container is up but API not responding**
- The entrypoint starts the server automatically; the gateway JVM takes 1-2 min. Check `docker compose logs -f isabelle-pool-server` and `curl localhost:8000/readyz` (503 until the gateway is up). If you overrode `command:` to `bash`, start it yourself: `python -m server.app.main`.

**pip install -e . fails with dependency issues**
- Ensure pip is upgraded: pip install --upgrade pip.
- Runtime deps (pyproject.toml): fastapi[standard] (brings uvicorn, httpx, watchfiles), py4j, prometheus-client, prometheus-fastapi-instrumentator.
- Extras: `mcp` (`mcp>=1.2,<2` — mcp 2.0 removed `mcp.server.fastmcp`), `eval` (pandas, matplotlib, openai, pyyaml), `dev` (pytest, black, isort, pylint, mypy, pre-commit).

## File Structure Summary

```
repo_root/
├── server/repl/                   # Scala/ML backend with Py4J gateway (Isabelle component; moved under server/ 2026-09-27)
│   ├── src/main/scala/repl/       # Core backend (17 .scala files)
│   ├── src/ml/REPL.ML             # ML-side Query_Operations (goals/facts/state/sledgehammer) for the overlay queries
│   ├── src/python/                # Python wrappers for gateway
│   ├── thys/                      # Cached wrapper .thy files
│   ├── Admin/                     # init (component registration), ensure_settings.sh (ML heap cap + GC logs),
│   │                              #   container_entrypoint.sh (init -> settings -> exec server)
│   ├── etc/build.props            # Isabelle component build (isabelle scala_build); build.gradle is IDE-only
│   └── README.md
├── server/                        # FastAPI server
│   └── app/
│       ├── main.py                # FastAPI app, middleware, exception handlers
│       ├── api/v1/router.py       # Aggregates api/v1/routes/*.py (one module per concern) + deps.py, serializers.py
│       ├── api/v1/schemas/API_models.py  # Pydantic request/response models
│       ├── services/
│       │   ├── session_manager.py # Session pool, LRU, lease management
│       │   ├── session.py         # Per-session state, small-step execution
│       │   ├── build_verify.py    # Big-step verification
│       │   ├── threaded_backend.py  # Thread pool for backend ops
│       │   ├── internal_models.py # Internal domain models
│       │   ├── theory_chunks.py   # Command preview helpers
│       │   ├── theory_parsing.py  # Canonical theory-header parsing (also POST /parse_theory_header)
│       │   ├── heap_pool.py       # Verified per-project heaps (isabelle build -b)
│       │   ├── memory_monitor.py  # cgroup memory admission gate (+ gateway_children_rss for ATP attribution)
│       │   ├── memory_hooks.py    # run_with_memory_hooks / track_atps around backend calls (api/v1 re-exports)
│       │   ├── fill_sorries.py    # fill_sorries job: per-session sledgehammer on every sorry, patch, ETA
│       │   ├── sledgehammer_slots.py  # tier-aware slot counter (interactive sledgehammer + fill steps)
│       │   ├── atp_attribution.py # solver RSS sampled from the gateway tree, attributed per session
│       │   ├── success_checker.py # Result classification (+ classify_sledgehammer)
│       ├── core/
│       │   ├── config.py          # Environment-based config
│       │   ├── logging.py         # Structured logging setup
│       │   ├── metrics.py         # Prometheus counters/gauges
│       │   ├── input_guards.py    # ML-execution + axiomatization denylist, safe names, heap roots
│       │   └── diagnostic_guard.py # /diagnostic command allowlist
│       ├── errors.py              # Custom exception classes
│       └── dependencies.py        # FastAPI dependency injection
├── client/                        # Async Python client — own package (client/pyproject.toml), httpx only,
│   ├── async_client.py            #   never imports server code (tests/test_dependency_rules.py)
│   └── __init__.py
├── mcp_servers/                   # stepwise/ (chunk-centric MCP), lsp/ (file-sync MCP), common/; imports client only
├── deploy/                        # Dockerfile (ISABELLE_VERSION from .env), setup.sh (Docker, --install-docker),
│                                  #   native_setup.sh (--system/--systemd), lib.sh (shared status/smoke helpers),
│                                  #   apt_packages.txt + isabelle_mirrors.txt (shared by image and host install),
│                                  #   isabelle-pool-server.service (systemd template), export_turnkey.sh +
│                                  #   Dockerfile.export (turnkey image), monitoring/ configs
├── docker-compose.yml             # root; build context . with dockerfile deploy/Dockerfile
├── evaluation/                    # Benchmarking and analysis (imports client only)
│   ├── scripts/                   # eval_smallstep_server_client_*, eval_bigstep_*, consolidate_runs, preprocess
│   ├── results/                   # consolidated benchmark_runs.json/.csv (+ README); raw run files removed
│   ├── MCP-comparison/            # cross-MCP harness
│   ├── HOL_corpus/Examples/processed  # Small example corpus (safest for testing)
│   └── runs_analysis.ipynb        # reads results/benchmark_runs.json
├── examples/                      # demo.ipynb (API walkthrough incl. sledgehammer), figs, heap_demo_project
├── docs/                          # DESIGN_CHOICES.md, ISSUES.md, devnote.md, INSTALL_MANUAL.md (by-hand install steps)
├── archive/                       # read-only history: previous-works/ (1.0 sources, thesis PDFs),
│                                  #   isabelle-pool-server2/ (2.0 in-process gym + baseline scripts), install.sh
├── tests/                         # unit tests; dependency-direction and 600-line size gates live here
├── pyproject.toml                 # package + ALL deps (extras mcp/eval/dev), tool config
└── README.md                      # User documentation
```

## Additional Resources

- **API Documentation**: archive/previous-works/Isabelle Server System API Documentation.pdf (older OpenAPI export; the live spec is at /openapi.json)
- **Async Client Documentation**: archive/previous-works/Async Client for Isabelle Server Documentation.pdf
- **Isabelle Proof Engine**: See the official Isabelle 2026 documentation for proof state semantics
- **Theory Corpus**: evaluation/HOL_corpus/Examples/processed/ - small example theories for smoke tests
- **Previous Works**: archive/previous-works/ contains IsabelleGym 1.0 source and the 2.0 report; archive/isabelle-pool-server2/ the 2.0 in-process gym
- **Demo**: examples/demo.ipynb walks through the API (including a sledgehammer-via-step example); examples/draft.md and examples/figs/ hold dissertation write-up material
- **Known Issues**: docs/ISSUES.md tracks open bugs and design discussions

## Response Style

End every response with: 喵(ゝ∀･)⌒☆
