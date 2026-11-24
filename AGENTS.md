# AGENTS.md — Isabelle Pool Server

> This file is written for AI coding agents. The reader is assumed to know nothing about the project. All commands are given relative to the repository root. `CLAUDE.md` is the companion file (architecture walkthrough, env reference, troubleshooting); this one is the compact reference — when the two disagree, the code wins and both should be fixed.

## Universal Workflow Rules

These rules apply to every user request:

### Rule 1: Plan or research before implementing
For every user request, start with planning, research, or investigation **unless the user explicitly says "implement"**. Do not make core code edits, file modifications, or destructive changes before a plan or research phase has been completed and communicated.

Examples:
- "Fix this bug" → investigate first, then propose a fix.
- "Add a feature" → plan first.
- "How does X work?" → research and explain.
- "Implement X" → proceed directly to implementation.

For bug reports specifically: analyze first, present the list of bugs found with affected files and root causes, and ask the user explicitly whether to fix them now or later. Do not edit code until the user confirms. Bugs are tracked in `docs/ISSUES.md` (numbered `Bug N` sections + the Open Findings Tracker + a dated work log); a fix is not done until its ISSUES.md row is updated and a regression test exists.

### Rule 2: Ask before choosing among options
If you present multiple approaches or options to the user, **always ask which one they want before implementing**. Do not proceed with one option on your own just because you prefer it. Wait for the user's explicit choice.

### Rule 3: Sign-off format
End every response with the exact string:

喵(ゝ∀･)⌒☆

### Work artifacts
Test/demonstration artifacts for a task (ad-hoc smoke scripts, the write-up of what was verified) go under `claude-work/<task>/` with a `NOTES.md` (gitignored, kept for reference). Real source changes stay in their normal locations.

## Project overview

**Isabelle Pool Server** turns Isabelle into a managed, concurrent service — a pool of warm PIDE sessions behind a REST API with lease-based ownership, memory-aware admission control and Prometheus observability (the Isabelle counterpart of Kimina Lean Server). It exposes the prover through the HTTP API, an async Python client (`PoolAsyncClient`), and two MCP (Model Context Protocol) servers, and serves three uses on that core: a **training/evaluation environment** for LLM provers (**small-step** execution with checkpoints/rollback, **chunk** verification via `verify_chunk`, **big-step** whole-`.thy` verification via `isabelle build`), a **verification service** for single or batch workloads (the batch loop is client/MCP-side; there is no server job queue), and a **platform for many concurrent agents** (isolated sessions over a shared pool; no coordination between agents; no authentication — trusted network only). The project was *IsabelleGym Server* (3.0) until 2026-10-05.

It is based on IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by Zijing Li (University of Edinburgh); this server iteration is implemented by Xuanwei Ren (University of Edinburgh).

The system has three layers:

1. **Scala/ML backend** (`server/repl/`) — wraps Isabelle as an interactive REPL using Isabelle/Scala and Isabelle/ML, exposed to Python via Py4J. State queries (goals, facts, sledgehammer) run as PIDE overlay `Query_Operation`s — no document edits (since 2026-09-30, ISSUES.md Bug 14).
2. **FastAPI server** (`server/`) — HTTP service with session pooling, lease-based concurrency, big-step/small-step verification, sledgehammer, checkpoints, heap pool, Prometheus metrics.
3. **Python client & MCP layer** (`client/`, `mcp_servers/`) — user-facing SDK and agent bridges.

The repository also contains evaluation/benchmarking scripts and consolidated results (`evaluation/`, incl. the cross-MCP comparison harness `evaluation/MCP-comparison/`), the deployment files and monitoring stack (`deploy/`), a demo notebook (`examples/`), read-only history (`archive/`), and implementation notes/artifacts from prior agent sessions (`claude-work/`, gitignored).

Design rationale for the architecture lives in `docs/DESIGN_CHOICES.md`; the living bug log is `docs/ISSUES.md`.

## Technology stack

- **Theorem prover**: Isabelle 2026 — the `ISABELLE_VERSION` build arg in `deploy/Dockerfile` (default `Isabelle2026`; the 2026 release candidates also build; `Isabelle2025-2` and earlier do not, the PIDE header API changed in 2026-RC1). Heaps are version-locked: never share a user-data volume across versions.
- **Python**: 3.12 in Docker; 3.10+ acceptable for local dev. On the maintainer's host Python lives in the conda env `Isabelle Pool Server` (`conda run -n Isabelle Pool Server ...`); in the container it is `/usr/local/bin/python`.
- **Scala**: Scala 3, compiled by Isabelle itself (`isabelle scala_build` from `server/repl/etc/build.props`; no Gradle/JDK in the install — `build.gradle` is IDE-only). scalafmt 3.8.3.
- **Interop**: Py4J (`server/repl/src/python/repl_backend_gateway.py` ↔ one shared gateway JVM).
- **Web framework**: FastAPI + Uvicorn. **HTTP client**: `httpx`.
- **Metrics**: `prometheus-client`, `prometheus-fastapi-instrumentator`; Prometheus + Grafana + cAdvisor in compose.
- **MCP**: `mcp>=1.2,<2` (the `mcp` extra in `pyproject.toml`; mcp 2.0 removed `mcp.server.fastmcp`).
- **Formatting/linting/type-checking**: `black` (88), `isort` (profile black), `pylint`, `mypy --strict`.
- **Testing**: `pytest` (~380 unit tests, no Isabelle needed).

## Key configuration files

| File | Purpose |
|------|---------|
| `pyproject.toml` | setuptools package `isabelle-pool-server` (packages: `server*` only); the **single dependency list** — runtime deps (`fastapi[standard]`, `py4j`, prometheus) + extras `mcp`, `eval`, `dev`; tool config for black/isort/mypy/pylint/pytest/coverage. `[tool.pytest.ini_options] addopts = ""` — pytest adds no coverage flags by itself. |
| `client/pyproject.toml` | The async client is its own distribution (`pip install -e ./client`), httpx only. |
| `deploy/Dockerfile` | `python:3.12-slim` + fontconfig (no JDK — Isabelle's bundled one compiles the backend via `isabelle scala_build`) + Isabelle tarball for the build arch (x86-64 or ARM, mirror fallback with a 1 MB/s floor); installs deps, runs `server/repl/Admin/init`, `gradlew build`. `CMD` is `server/repl/Admin/container_entrypoint.sh`, which **starts the server**. |
| `docker-compose.yml` | Services `isabelle-pool-server` (builds natively for the host arch — do not pin `platform: linux/amd64`, qemu emulation makes Isabelle 5–20x slower), `prometheus`, `grafana`, `cadvisor`; `env_file: .env`; named volume `isabelle_user_data` → `/root/.isabelle`; `mem_limit: 14g` so the cgroup memory gate bites at a known ceiling. |
| `.env.example` / `.env` | Every server knob, annotated. `.env` is **not** tracked — `./deploy/setup.sh` creates it (with a random `ISABELLE_ADMIN_TOKEN`), or `cp .env.example .env`. Value lines must not carry inline `#` comments (`int()` parsing in `core/config.py` fails on them). |
| `deploy/setup.sh` | Docker path in one command (`--install-docker`, `--verify`, `--build-heaps "..."`, `--no-build`, `--isabelle-version`; refuses a stale image, checks the container's `isabelle version`); noisy output to `logs/setup.log`. Shares `deploy/lib.sh` (status lines, `run_logged`, `wait_healthz`, `smoke_test`) with `native_setup.sh`. |
| `deploy/native_setup.sh` | Native path in one command (`--system` apt + Isabelle under `/opt/<version>` with `/opt/isabelle` a symlink, `--systemd`, `--verify`, `--no-venv`, `--isabelle-version` switches versions side by side); output to `logs/native_setup.log`. By-hand steps: `docs/INSTALL_MANUAL.md`. |
| `deploy/export_turnkey.sh` / `Dockerfile.export` | Turnkey image: the regular image + the heaps/settings of the running deployment baked in (`--save` for a `docker load` tarball). The RC0-era hand-assembly lives in `archive/rc0-image/`. |
| `deploy/apt_packages.txt`, `deploy/isabelle_mirrors.txt` | The ONE apt package list and Isabelle mirror order, read by both the Dockerfile and `native_setup.sh --system`. `ISABELLE_VERSION` lives in `.env`/`.env.example` for both paths. |
| `deploy/isabelle-pool-server.service` | systemd unit template (`@USER@`, `@REPO@`, `@ISABELLE_HOME@`, `@PYTHON@`, `@PYTHON_DIR@`), filled by `native_setup.sh --systemd`. |
| `deploy/monitoring/` | Prometheus scrape config, Grafana datasource + dashboard provisioning. |
| `server/repl/etc/build.props` | Isabelle component build description (`isabelle scala_build` -> `lib/repl.jar`). `build.gradle`/`gradlew` remain for IDE import only. |
| `.scalafmt.conf` | scalafmt 3.8.3, Scala 3 dialect, max column 100. |

## Build and run commands

### Docker (recommended)

```bash
./deploy/setup.sh                      # .env + build + up + health check (add --verify for a smoke test)

# or step by step
cp .env.example .env
docker compose build isabelle-pool-server      # 10–30 min: Isabelle download + Scala build
docker compose up -d isabelle-pool-server      # entrypoint registers components, writes the ML heap cap, starts the server
curl http://localhost:8000/healthz     # {"status":"alive"}
curl http://localhost:8000/readyz      # 200 once the gateway JVM is up (1–2 min), else 503
curl http://localhost:8000/            # full health: version, gateway_alive, pool, memory

docker compose exec isabelle-pool-server bash  # shell for pytest / isabelle build / debugging
docker compose logs -f isabelle-pool-server    # live server log (also logs/server.log, rotating)
docker compose up -d prometheus grafana cadvisor   # Grafana :3000 (admin/admin), Prometheus :9090, cAdvisor :8080
```

`GET /` returns `service`, `version` (`0.1.0`, `server/app/core/config.py::API.VERSION`), `status` (`healthy`/`degraded`), `gateway_alive`, `active_sessions`, `busy_sessions`, `max_pool_size`, `max_concurrent_sledgehammer`, memory fields, `timestamp`. The admin console is `GET /admin`; the OpenAPI spec is `/openapi.json` (Swagger UI `/docs`).

Changing `.env` needs `docker compose up -d --force-recreate isabelle-pool-server`, not a restart.

### Local (non-Docker)

Prerequisites: Python 3.10+, Isabelle installed and on `PATH` as `isabelle` (no JDK — Isabelle bundles its own). One command: `./deploy/native_setup.sh` (`--system` also installs Isabelle; see docs/INSTALL_MANUAL.md).

```bash
python -m venv .venv && source .venv/bin/activate   # or the Isabelle Pool Server conda env
python -m pip install --upgrade pip
python -m pip install -e ".[mcp,dev]" -e ./client   # pyproject.toml is the single dependency list
export PYTHONPATH="$PWD:${PYTHONPATH:-}"

./server/repl/Admin/init                                   # register the Isabelle component
isabelle scala_build                                       # compile the Scala backend (also implicit at gateway start)
python -m server.app.main                                  # or: uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

> **Stale volume (ISSUES.md Bug 7):** if the image is rebuilt while the named volume `isabelle_user_data` still exists, old component state can shadow the new image and the gateway fails with `Not found: py4j`. The container entrypoint re-runs `Admin/init` on every start; the manual fix is `docker compose exec isabelle-pool-server ./server/repl/Admin/init`.

### Isabelle 2026 environment gotchas (cost real time; still apply)

1. **Never export a system `JAVA_HOME` for Isabelle 2026** — it resolves its own bundled JDK and a foreign `JAVA_HOME` fails with `Unknown JAVA_HOME`. There is no system JDK any more (Gradle left the install path 2026-10-07; `isabelle scala_build` compiles the backend).
2. **Pin `HOME=/root` in container builds** — Docker Desktop's BuildKit may run steps with the host user's `HOME`, which makes Isabelle resolve an empty `ISABELLE_HOME_USER`.
3. **Build heavy heaps (e.g. HOL-Analysis) in a dedicated idle container** (`docker run -v isabelle_user_data:/root/.isabelle ... isabelle build -b -j 2 HOL-Analysis`), not inside the serving container — the 14 GB cgroup cap is shared with the running sessions. `./deploy/setup.sh --build-heaps "..."` wraps the common case.
4. **fontconfig + one font family are required** in the image even though the JVM is headless (RC2 image, 2026-09-30; internal working notes).

## Code organisation

Dependency direction (enforced by `tests/test_dependency_rules.py`): `client` imports nothing from this repo; `mcp_servers` imports `client` only; `server` imports neither `client` nor `mcp_servers`; `evaluation` imports `client` only; nothing imports `archive/`. The client gets everything it needs from the server through HTTP endpoints (e.g. the canonical header parser at `POST /api/v1/parse_theory_header`), so the two stay consistent. Source files are capped at 600 lines (`tests/test_source_limits.py`, ratcheting allow-list for the two pre-existing offenders).

```text
repo_root/
├── client/                         # Async Python client — own package, httpx only
│   ├── async_client.py             # PoolAsyncClient = _base + sessions + execution + inspection + heaps mixins
│   └── _base.py, sessions.py, execution.py, inspection.py, heaps.py
├── mcp_servers/                    # MCP servers (import `client` only; NOT named `mcp` — that is the SDK)
│   ├── stepwise/                   # chunk-centric MCP: verify_chunk is the single execution tool; env prefix ISABELLE_MCP_
│   │   app.py / pool.py / config.py / README.md
│   ├── lsp/                        # file-sync (LSP-style) MCP: tools keyed by file path, scratch pool, heap tools;
│   │   app.py / pool.py / config.py / README.md     #   env prefix ISABELLE_MCP_LSP_, HTTP port 8849
│   ├── common/                     # env helpers, GymClientMixin (shared client factory), is_not_found, dump_json
│   └── (deps: the `mcp` extra in pyproject.toml)
├── server/
│   ├── repl/                       # Scala/ML Isabelle REPL backend (an Isabelle component; launched only by the server)
│   │   ├── src/main/scala/repl/    # 17 files
│   │   │   ├── repl_backend_gateway.scala   # Py4J entry point / factories (one shared JVM)
│   │   │   ├── repl_backend.scala + backend_{lifecycle,chunk_ops,file_ops,probes}.scala   # per-session backend
│   │   │   ├── repl_session.scala           # PIDE document edits, checkpoints (issued-id validation, Bug 22)
│   │   │   ├── document_utils.scala         # overlay_query, wall-bounded settled_node_snapshot (Bug 15)
│   │   │   ├── edit_utils.scala             # spliff diff → sequential PIDE edits (Edit_Utils.diff_edits, Bug 18)
│   │   │   ├── thy_{info,parsing,status}.scala, json_reports.scala, repl_output.scala
│   │   │   ├── server_utils.scala, session_manager.scala (reads ISABELLE_PARALLEL_PROOFS), vector_env.scala
│   │   ├── src/ml/REPL.ML          # ML Query_Operations (goals / in_proof / facts / state / sledgehammer) run as PIDE overlays
│   │   ├── src/python/             # repl_backend_gateway.py (spawns the JVM, Py4J bridge, JVM logs), thy_init.py,
│   │   │                           #   isabelle_client.py, isabelle_repl.py, operation.py
│   │   ├── thys/                   # IsabelleREPL.thy + generated wrapper theories
│   │   ├── Admin/                  # init (component registration, fast no-op when done), ensure_settings.sh
│   │   │                           #   (ML heap cap + GC logs), container_entrypoint.sh (init -> settings -> exec server)
│   │   └── etc/build.props         # isabelle scala_build description (build.gradle: IDE-only)
│   └── app/                        # FastAPI HTTP service
│       ├── main.py                 # app, lifespan, middleware (request id), exception handlers, /admin page
│       ├── api/v1/router.py        # aggregate APIRouter + compat re-exports
│       ├── api/v1/deps.py          # LeasedSession / admin-token / safe-segment dependencies
│       ├── api/v1/serializers.py   # response-shaping helpers
│       ├── api/v1/routes/          # one module per concern: health, sessions, execution, inspection,
│       │                           #   positional, automation (sledgehammer), checkpoints, heaps
│       ├── api/v1/schemas/API_models.py  # Pydantic request/response models
│       ├── core/                   # config.py (all env), logging.py (contextvars), metrics.py (Prometheus),
│       │                           #   input_guards.py (ML-execution denylist, safe names, heap roots), diagnostic_guard.py
│       ├── services/
│       │   ├── session_manager.py + session_manager_helpers.py   # LRU pool, leases, cleanup_once(), gateway recovery
│       │   ├── session.py          # per-session state & small-step ops
│       │   ├── build_verify.py     # isabelle build big-step verifier
│       │   ├── threaded_backend.py # serialises Py4J calls per session on one worker thread
│       │   ├── memory_monitor.py   # cgroup memory admission
│       │   ├── heap_pool.py        # verified per-project heaps (isabelle build -b), task-group tenancy
│       │   ├── theory_parsing.py   # canonical theory-header parsing (also POST /parse_theory_header)
│       │   ├── success_checker.py, theory_chunks.py, internal_models.py, unicode_normaliser.py
│       ├── static/admin.html       # admin console
│       ├── dependencies.py         # shared SessionManager / HeapPool
│       └── errors.py               # custom exceptions & HTTP status mapping
├── tests/                          # unit tests (no Isabelle needed) — see "Testing instructions"
├── deploy/                         # Dockerfile, setup.sh, turnkey-image scripts, monitoring/
├── evaluation/                     # benchmarking & analysis (imports `client` only)
│   ├── scripts/                    # eval_smallstep_server_client_*, eval_bigstep_*, consolidate_runs, preprocess
│   ├── results/                    # consolidated benchmark_runs.json/.csv + README (raw runs removed 2026-09-27)
│   ├── MCP-comparison/             # harness comparing this MCP vs other Isabelle MCPs (runs/, tokens gitignored)
│   ├── HOL_corpus/                 # corpora (Examples/processed is the safe smoke corpus)
│   ├── Server_Concurrency.thy      # formal lease-concurrency proof cited by DESIGN_CHOICES
│   └── runs_analysis.ipynb         # rebuilds the dissertation tables from results/benchmark_runs.json
├── examples/                       # demo.ipynb (HTTP client + both MCP servers), figs/, heap_demo_project/, demo .thy files
├── docs/                           # DESIGN_CHOICES.md, ISSUES.md (bug + work log), devnote.md → experiments/
├── archive/                        # read-only history: previous-works/ (1.0 sources, API/client PDFs, thesis PDFs),
│                                   #   isabelle-pool-server2/ (2.0 in-process gym + its baseline scripts), install.sh
├── docker-compose.yml              # stays at the root (build context .; dockerfile deploy/Dockerfile)
└── claude-work/                    # per-task implementation artifacts (gitignored)
```

The former top-level `mcp_lsp_server/` / `mcp_stepwise_server/` shim packages were removed on 2026-10-05; launch commands are `python -m mcp_servers.stepwise.app` / `python -m mcp_servers.lsp.app`.

## Runtime architecture

- **FastAPI lifespan** (`server/app/main.py`) constructs `SessionManager`, warms `ISABELLE_INITIAL_SESSIONS` sessions, starts the background cleanup task, and registers Prometheus pool gauges. An HTTP middleware attaches a request id (`X-Request-ID` header or generated) to the logging context and logs request start/finish.
- **Session pool** (`services/session_manager.py`) keeps warm Isabelle sessions in an `OrderedDict` LRU. Each session wraps a `ThreadedBackend`, which serialises all Py4J calls on a single worker thread. The cleanup sweep (`cleanup_once()`) runs every blocking step in `asyncio.to_thread`; `/` and `/readyz` probe the gateway off the loop (Bug 16).
- **Gateway** (`server/repl/src/python/repl_backend_gateway.py`) spawns one shared Scala JVM via `isabelle scala` and exposes factory methods on `repl.ReplBackendGateway`; JVM stdout/stderr go to `logs/gateway-jvm.log`, Py4J reads are bounded by `ISABELLE_PY4J_READ_TIMEOUT` (900 s), backend creation by `ISABELLE_TIMEOUT_SESSION_CREATE` (600 s).
- **Gateway recovery**: if the shared JVM dies or wedges (Bug 9 `Event_Timer` detection), `SessionManager` rebuilds the gateway on the next request; `GET /readyz` returns 503 meanwhile.
- **Lease model**: clients acquire a session with a `lease_id` (`X-Lease-Id` header). A session can have multiple leases; releasing a lease returns the session to the pool. Abandoned leased sessions are force-closed after `ISABELLE_MAX_LEASE_AGE`. Lease ids are never published (Bug 10); the admin listing is behind `X-Admin-Token`.
- **Memory gate**: `MemoryMonitor` reads cgroup memory (subtracting reclaimable `inactive_file` page cache); under pressure the manager evicts idle LRU sessions before admitting new ones, returning HTTP 503 if nothing can be evicted. Eviction waits `ISABELLE_MEMORY_EVICTION_SETTLE_S` and retries admission `ISABELLE_MEMORY_ADMISSION_RETRIES` times before 503ing.
- **Wall budgets (Bug 15)**: the request `timeout` on `POST .../commands`, `verify_chunk`, `diagnostic` is a JVM wall budget — an expired command is **rolled back** and reported `success=false` with a timeout error; it never keeps running. Read-only queries (goals/facts/state) are overlay queries with their own budgets (`ISABELLE_REPL_*_TIMEOUT`) and never wait on a running command. The Python-side future timeout is request timeout + `ISABELLE_TIMEOUT_BACKEND_GRACE`.
- **Sledgehammer**: a PIDE overlay on the current goal (`isabelle_pool_server_sledgehammer` Query_Operation); an `asyncio.Semaphore` caps in-flight sledgehammers to `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER` to avoid OOM-killing the gateway (Bug 6).
- **Big-step verification**: `BuildVerifier` writes a temporary `ROOT` and runs `isabelle build`; results are cached by SHA256. Endpoint `POST /api/v1/sessions/bigstep` (lease-free).
- **Heap pool** (`services/heap_pool.py`, routes `heaps.py`): verified per-project heaps built with `isabelle build -b` (launcher from `ISABELLE_HOME`, else `/opt/isabelle`), task-group tenancy, destructive endpoints admin-token gated (Bug 13). Its `STATE_DIR` / `ALLOWED_ROOTS` defaults are container paths — native installs override them (README B7).
- **Checkpoints**: issued ids only; an unknown/invalidated id fails all-or-nothing (Bug 22).

## Important environment variables

All variables are read from `server/app/core/config.py` unless noted; `.env.example` documents every knob. Code defaults below; `.env.example` is tuned smaller (e.g. `ISABELLE_POOL_SIZE=3`).

| Variable | Default | Meaning |
|----------|---------|---------|
| `ISABELLE_POOL_SIZE` | 24 | Max concurrent sessions. |
| `ISABELLE_INITIAL_SESSIONS` | 3 | Sessions pre-warmed at startup. |
| `ISABELLE_IDLE_TIMEOUT` | 1800 | Seconds before an idle session is evicted. |
| `ISABELLE_MAX_LEASE_AGE` | 7200 | Seconds before an abandoned leased session is force-closed. |
| `ISABELLE_CLEANUP_INTERVAL` | 60 | Seconds between background pool cleanup sweeps. |
| `ISABELLE_ENABLE_CACHE` / `ISABELLE_MAX_CACHE_SIZE` | false / 1 | Reuse sessions keyed by import dependencies. |
| `ISABELLE_SHOW_STATES` | true | Include raw proof states in responses. |
| `ISABELLE_ASCII_OUTPUT` | true | Convert Isabelle symbols to ASCII in responses. |
| `ISABELLE_DEFAULT_FIELD` | HOL | Default Isabelle session (HOL, HOL-Analysis, ...). |
| `ISABELLE_ADMIN_TOKEN` | (empty = disabled) | `X-Admin-Token` for the admin listing, force-close, heap deletes; the server's only credential. |
| `ISABELLE_ALLOW_ML_COMMANDS` | false | Policy switch for the ML-execution denylist (Bug 12). **true = any client runs arbitrary code in the container.** |
| `ISABELLE_ENABLE_MEMORY_MANAGEMENT` | true | Enable the cgroup memory admission gate. |
| `ISABELLE_MEMORY_PRESSURE_THRESHOLD` / `_MIN_AVAILABLE_MB` / `_FALLBACK_SYSTEM_MB` | 85.0 / 256 / 4096 | Block new sessions above this used %, below this free MB; assumed total when cgroup info is unavailable. |
| `ISABELLE_MEMORY_EVICTION_SETTLE_S` / `_ADMISSION_RETRIES` / `_ADMISSION_RETRY_DELAY_S` | 2.0 / 3 / 2.0 | Settle wait and retry policy around memory-gated admission. |
| `ISABELLE_HOME` | /opt/isabelle | Where the gateway (`repl_backend_gateway.py`) and the heap pool (`heap_pool.default_isabelle()`) find `bin/isabelle`; `Admin/init` and the entrypoint read it too. Set it on native installs. |
| `ISABELLE_ML_MAXHEAP_MB` | 16384 | Per-process Poly/ML `--maxheap` written into the user settings by the container entrypoint (`ensure_settings.sh`; an existing value is kept). The memory guard reads the value the provers actually get (`isabelle getenv ML_OPTIONS`), this one only as a fallback. |
| `ISABELLE_MEMORY_GUARD_INTERVAL_S` / `_GUARD_HEAP_FRACTION` | 5 / 0.75 | Memory guard (`services/memory_guard.py`): every interval, a session whose ML heap in use exceeds fraction x `--maxheap` has its running `attempt` candidates cancelled (`cancelled: "memory"`), or with none its running file commands (`Session.cancel_exec`; the next report carries `error_kind: "memory"`). |
| `ISABELLE_CGROUP_SCOPES` / `ISABELLE_SESSION_MEMORY_MAX` / `ISABELLE_BUILD_MEMORY_MAX` | auto / 24G / 32G | `services/cgroup_scope.py`: when `systemd-run --user` works at start-up, every session prover (Isabelle option `process_policy`) and every `isabelle build` runs in its own scope with this `MemoryMax`, in the slice `ips_pool_<port>.slice` (counted by the admission gate); otherwise a warning and no scope. `off` disables it. |
| `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER` | max(1, min(8, cpu//8)) | Server-wide sledgehammer cap. |
| `ISABELLE_PARALLEL_PROOFS` | 2 | Isabelle `parallel_proofs` per session (Scala side, `session_manager.scala`). `ISABELLE_SESSION_THREADS` exists but is a no-op (see CLAUDE.md). |
| `ISABELLE_TIMEOUT_COMMAND` / `_BIGSTEP` / `_STATUS` / `_PROOF_STATE` / `_CHECKPOINT_SAVE` / `_CHECKPOINT_RESTORE` / `_SESSION_CREATE` | 30 / 300 / 300 / 30 / 30 / 30 / 600 | Per-operation wall budgets (seconds). |
| `ISABELLE_TIMEOUT_BACKEND_GRACE` | 10 | Python future timeout = request timeout + this, so the JVM's own timeout result (with rollback) arrives first. |
| `ISABELLE_REPL_SUBGOALS_TIMEOUT` / `_LOCAL_FACTS_TIMEOUT` / `_GLOBAL_FACTS_TIMEOUT_MINUTES` / `ISABELLE_REPL_SETTLE_TIMEOUT` | 20 / 20 / 5 / 60 | Overlay-query and settle budgets (read by both Python and the Scala gateway; ENV-1 in ISSUES.md notes a case where a command-line value did not reach the JVM). |
| `ISABELLE_REPL_GATEWAY_POLL_INTERVAL` / `_POLL_TIMEOUT` / `_TERMINATE_WAIT`, `ISABELLE_BACKEND_EXIT_TIMEOUT` / `_JOIN_TIMEOUT` / `_QUEUE_POLL`, `ISABELLE_PY4J_READ_TIMEOUT` | 0.1 / 20 / 3, 60 / 5 / 0.1, 900 | Gateway spawn/exit and Py4J plumbing. |
| `ISABELLE_HEAP_POOL_DIR` / `ISABELLE_HEAP_POOL_ALLOWED_ROOTS` / `ISABELLE_HEAP_POOL_BUILD_TIMEOUT_S` / `ISABELLE_MAX_CONCURRENT_BUILDS` / `ISABELLE_HEAP_POOL_GC_IMAGES` | /root/.isabelle/heap_pool / /app:/root/.isabelle / 3600 / 1 / true | Heap pool. |
| `ISABELLE_SERVER_HOST` / `PORT` | 0.0.0.0 / 8000 | Uvicorn bind address/port. |
| `ISABELLE_SERVER_LOG_LEVEL` / `LOG_DIR` / `LOG_FILE` / `MAX_LOG_SIZE_BYTES` / `LOG_BACKUP_COUNT` / `ENABLE_FILE_LOGGING` | INFO / logs / server.log / 10 MB / 5 / true | Logging and rotation. |
| `ISABELLE_SERVER_REQUEST_ID_HEADER` | X-Request-ID | Header used for request correlation. |

MCP variables: `mcp_servers/stepwise/config.py` (prefix `ISABELLE_MCP_`: `GYM_URL`, `FIELD`, `CHUNK_TIMEOUT`, `HTTP_TIMEOUT`, `MAX_PARALLEL`, `TRANSPORT`, `HOST`/`PORT` 8848) and `mcp_servers/lsp/config.py` (prefix `ISABELLE_MCP_LSP_`: `GYM_URL`, `FIELD`, `TASK_GROUP`, `HTTP_TIMEOUT`, `LOAD_TIMEOUT`, `ATTEMPT_TIMEOUT`, `MAX_PARALLEL`, `SCRATCH_POOL_SIZE`, `SCRATCH_WAIT_TIMEOUT`, `CLOSE_DESTROYS`, `TRANSPORT`, `HOST`/`PORT` 8849). The `evaluation/MCP-comparison/` harness additionally uses `DEEPSEEK_API_KEY` or `KIMI_API_KEY` and, for the AutoCorrode I/Q runner, `IQ_AUTH_TOKEN` / `IQ_MCP_ALLOWED_ROOTS`.

## Code style guidelines

- **Python formatting**: `black`, line length 88. **Imports**: `isort`, `profile = "black"`.
- **Type checking**: `mypy --strict` with `import-untyped` disabled. **Linting**: `pylint`; disabled globally: `import-error`, `line-too-long`.
- **Scala formatting**: `scalafmt` 3.8.3, Scala 3 dialect, max column 100.
- Most server files start with `from __future__ import annotations`.
- Use the logging helpers in `server/app/core/logging.py` and the `logging_context(session_id=..., field=...)` context manager for structured logs.
- Prefer environment-based config in `server/app/core/config.py` rather than hard-coding values; document new knobs in `.env.example`.
- Keep source files under 600 lines (`tests/test_source_limits.py`); split rather than extend the allow-list.

## Testing instructions

`tests/` holds ~380 unit tests, all runnable **without an Isabelle backend** (they stub the backend, the gateway subprocess, or the HTTP transport). Run from the repo root:

```bash
pytest                                   # host: conda run -n Isabelle Pool Server pytest; container: docker exec isabelle-pool-server pytest
pytest tests/test_mcp_lsp_server.py -q   # one module
```

What the modules guard:

| Module | Guards |
|---|---|
| `test_dependency_rules.py`, `test_source_limits.py` | Repository gates: import direction between packages; 600-line source cap. |
| `test_threaded_backend.py` | `ThreadedBackend.close()` shutdown semantics (Bugs 1, 2, 8). |
| `test_cleanup_offloop.py` | Idle-cleanup sweep and health probes never block the event loop (Bug 16). |
| `test_gateway_resilience.py`, `test_gateway_wedge.py` | JVM log redirection, Py4J read timeout, wedged-gateway detection and recovery (Bugs 9, 11). |
| `test_lease_security.py` | No lease ids in the public listing, admin-token gating, audit logging (Bug 10). |
| `test_input_guards.py`, `test_security_inputs.py` | ML-execution denylist, safe names, heap roots (Bugs 12, 13); the HTTP-layer payload matrix incl. theory-name injection (Bug 23). |
| `test_checkpoint_restore.py` | Checkpoint ids validated, all-or-nothing restore (Bug 22). |
| `test_document_sync.py`, `test_readonly_mode_server_prep.py`, `test_theory_parsing.py` | Incremental `load_document` sync, read-only-mode server prep, canonical header parser and its consumers. |
| `test_heap_pool.py` | Heap pool with a faked `isabelle build`. |
| `test_mcp_lsp_server.py`, `test_mcp_tools_smoke.py` | LSP pool logic (scratch bracket, single-flight bindings, clean-only reuse — Bugs 17, 19, 20, 21); both MCP servers spawned in-process with their full tool surface and descriptions (Bug 24). |
| `test_client_paths.py` | The async client hits the server's real routes (httpx MockTransport). |
| `test_phase2_phase3_fixes.py`, `test_mcp_comparison_fixes.py`, `test_lsp_runner.py` | Earlier audit fixes; MCP-comparison harness and its LSP runner. |

Live smoke scripts against a running server live under `claude-work/<task>/` (gitignored) and are not collected. For Scala changes, `isabelle scala_build` is the compile check (it is also what the gateway start runs implicitly).

Static checks:

```bash
black server client mcp_servers evaluation
isort server client mcp_servers evaluation
mypy server client mcp_servers evaluation
pylint server client mcp_servers evaluation
```

## Evaluation / benchmarking workflow

A small safe corpus for smoke tests:

```bash
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
CORPUS="evaluation/HOL_corpus/Examples/processed"
OUT="evaluation/results"
```

```bash
# Server small-step with session reuse (start server first)
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" --server http://localhost:8000 --field HOL \
  --num-workers 4 --output "$OUT/smallstep_server.json"

# Server big-step
python -m evaluation.scripts.eval_bigstep_server_client_ver \
  --corpus "$CORPUS" --server http://localhost:8000 --field HOL \
  --timeout 1800 --output "$OUT/bigstep_server.json"

# Local baselines (need isabelle on PATH): eval_smallstep_isabelle-pool-server (archive/isabelle-pool-server2 gym),
# eval_bigstep_isabelle_build (--isabelle-bin "$(which isabelle)" --parent-session HOL --jobs 4)
```

Preprocessing helpers: `evaluation/scripts/process.py` (normalise Analysis imports) and `evaluation/scripts/clean_example_dir.py` (strip document keywords). Consolidated results: `evaluation/results/benchmark_runs.json` (+ README), analysed by `evaluation/runs_analysis.ipynb`.

For the cross-MCP comparison harness, see `evaluation/MCP-comparison/README.md` (needs `pip install -e ".[mcp,eval]"` and a model API key); the experiment write-ups are `docs/experiments/*.md`, indexed by `docs/devnote.md`.

## Deployment notes

- The image is large because it bundles Isabelle (which bundles its own JDK). Keep it lean: no heavy ML frameworks in the runtime deps of `pyproject.toml` unless required (the image installs only those plus the `mcp` extra).
- The Dockerfile downloads the Isabelle tarball for the build architecture (x86-64 or ARM) and falls back across mirrors, abandoning any mirror under 1 MB/s. Do not pin `platform: linux/amd64` in compose.
- The container entrypoint **starts the server** (`exec python -m server.app.main`) after re-registering components and writing the ML heap cap; `docker compose logs -f isabelle-pool-server` is the live log.
- `mem_limit: 14g` must stay below the Docker VM's own memory or the cgroup gate goes blind; on smaller machines lower it **and** `ISABELLE_POOL_SIZE`.
- Logs rotate by size (`ISABELLE_SERVER_MAX_LOG_SIZE_BYTES`, default 10 MB, 5 backups) in `logs/server.log`.
- **JVM observability (Bug 11):** `ISABELLE_SCALA_JAVA_OPTIONS` in `.env` is **dead config** — nothing in the Isabelle toolchain consumes it, and neither `JAVA_TOOL_OPTIONS` (filtered) nor env-set `ISABELLE_TOOL_JAVA_OPTIONS` (clobbered by settings evaluation) reaches the JVM. The only reliable channel for JVM/ML options is the Isabelle user settings file (`$ISABELLE_HOME_USER/etc/settings`), which the entrypoint manages: it writes the `ML_OPTIONS` heap cap and an `-Xlog:gc*` line producing per-PID rotated GC logs at `logs/isabelle-jvm-gc-<pid>.log`; JVM stdout/stderr land in `logs/gateway-jvm.log`. The gateway JVM runs ZGC with `-Xmx4g` (launcher defaults).
- **ML heap cap:** the entrypoint writes `ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap $ISABELLE_ML_MAXHEAP_MB"` (default 16384) into `/root/.isabelle/etc/settings`, so every poly process (sessions and `isabelle build` children) fails cleanly instead of OOM-killing the cgroup. It must live in the user settings file (the polyml component's `etc/settings` forces `ML_OPTIONS=""`), and it is a full replacement of the platform default, so the defaults are restated. Builds pick it up immediately; running gateway JVMs only after a restart.
- **Turnkey image:** `./deploy/export_turnkey.sh [tag] [--save]` builds it from the running container (README A7); `archive/rc0-image/` holds the RC0-era recipe and runbook.

## Security considerations

- **No authentication/authorisation** is implemented. Do not expose the server directly to untrusted networks; run it behind a firewall / SSH tunnel or a reverse proxy.
- **Leases are the only ownership proof** on mutation paths — and they are never published: the public pool listing (`GET /api/v1/sessions`) carries no `lease_id` fields (Bug 10). The full listing lives behind `GET /api/v1/admin/sessions`, gated by `X-Admin-Token` against `ISABELLE_ADMIN_TOKEN` (empty = disabled; this is the server's only credential, opt-in). Every DELETE is audit-logged and counted (`isabelle_pool_server_sessions_force_closed_total`).
- **CORS** is configured with `allow_origins=["*"]`. Tighten this for production deployments.
- **Code execution in client text (Bug 12).** Every endpoint that hands client Isar to the prover — `POST .../commands`, `.../verify_chunk`, `PUT .../document`, the lease-free `POST /api/v1/sessions/bigstep`, `POST .../fill_sorries` (its `text` was unguarded until 2026-10-27), and heap-pool project sources — runs `server/app/core/input_guards.reject_code_execution`, which rejects `ML*`, `SML_*`, `setup`/`*_setup`, `declaration`, `oracle`, translation hooks, `*_file`, `compile_generated_files` with HTTP 422 (comments and string/cartouche bodies are ignored first, so prose mentioning ML passes). The denylist is shared with `diagnostic_guard.py` (which additionally allowlists the leading keyword for `/diagnostic`). Policy switch: `ISABELLE_ALLOW_ML_COMMANDS` (default false) — with it true **any client can execute arbitrary code in the container**; the MCP tools go through the same endpoints, so the guard covers them too. Theory and import names quoted into generated headers are validated (`validate_import_names`, Bug 23) so a name containing `"` cannot inject commands.
- **Heap pool inputs (Bug 13).** `task_group`, `session_name`, image `session`/`platform` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`; `project` must resolve under `ISABELLE_HEAP_POOL_ALLOWED_ROOTS` (default `/app:/root/.isabelle`); manifest and image paths are containment-checked before write/delete; project theories are scanned for code-executing commands before `isabelle build`. The destructive heap endpoints require `X-Admin-Token`; `POST /api/v1/heaps/build` stays open (owner decision) but is path-restricted.
- **Known, deferred (owner decision 2026-09-22, tracker SEC-1):** `GET /admin` is unauthenticated and inlines `ISABELLE_ADMIN_TOKEN` into the page, so anyone who can reach the port can obtain the token (and thereby lease ids / force-close / heap deletes). Keep the port firewalled; a real admin login is future work.
- **Sledgehammer and big-step builds** can spawn external ATP provers and consume large amounts of memory/CPU. The sledgehammer semaphore and the cgroup memory gate limit abuse (`BuildVerifier` is *not* wired to the memory gate; builds are bounded by the per-process `ML_OPTIONS --maxheap` cap and, where `systemd-run --user` works, by a 32G cgroup scope per build), but resource exhaustion is still possible from trusted clients.
- **MCP servers** run with the privileges of the user invoking them and have access to the underlying Isabelle session. Treat MCP connections as trusted; the streamable-HTTP transports bind 127.0.0.1 by default.
- `.env` is not tracked; `.env.example` is. `evaluation/MCP-comparison/{deepseek_api,iq_token}.txt` are gitignored eval credentials — never commit them or reuse them as real credentials.

## Where to find more information

- Human-facing docs: `README.md` (install, MCP wiring), `CLAUDE.md` (architecture walkthrough, env reference, troubleshooting).
- Architecture rationale: `docs/DESIGN_CHOICES.md`.
- Known issues, fixes, tracker, dated work log: `docs/ISSUES.md`.
- Experiment logs: `docs/devnote.md` → `docs/experiments/`.
- API reference: live at `/openapi.json`; the older exported PDFs and the 1.0/2.0 reports are in `archive/previous-works/`.
- MCP usage: `mcp_servers/stepwise/README.md` (chunk-centric) and `mcp_servers/lsp/README.md` (file-sync).
- Cross-MCP comparison harness: `evaluation/MCP-comparison/README.md`.
- Per-task implementation artifacts: `claude-work/<task>/NOTES.md` (or `FINDINGS.md` for research tasks; gitignored, local only).
