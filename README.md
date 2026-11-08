# Isabelle Pool Server

**Isabelle Pool Server** turns the Isabelle theorem prover into a managed, concurrent
service: a pool of warm proof sessions behind a REST API, with lease-based ownership,
memory-aware admission control, and Prometheus observability. It is the Isabelle
counterpart of [Kimina Lean Server](https://github.com/project-numina/kimina-lean-server)
(FastAPI + a pool of prover REPLs + an import-keyed cache + a Python SDK), extended with
checkpoints and rollback, whole-chunk and whole-theory verification, a heap pool, and two
MCP servers for LLM agents.

On that core it serves three purposes:

- a **training and evaluation environment** for LLM-based provers — **small-step**
  execution (one Isar command at a time, with checkpoints/rollback), **chunk** verification
  (`verify_chunk`: a whole proof chunk in one PIDE edit with per-command status) and
  **big-step** verification (whole `.thy` files via `isabelle build`);
- a **verification service** for single theories or batch workloads — result caching,
  pre-built heaps, pool-level parallelism; the batch loop lives in the client or MCP layer
  (there is no server-side job queue);
- a **platform for many concurrent agents** — the MCP servers give each agent an isolated
  session over a shared prover pool, inside a trusted network (there is no authentication;
  agents are isolated from each other, not coordinated).

Successor to IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by
Zijing Li (University of Edinburgh); this server iteration (IsabelleGym 3.0 until its
renaming in October 2026) is implemented by Xuanwei Ren (University of Edinburgh).

Components:

| Path | What it is |
|---|---|
| `server/` | FastAPI service: session pool, leases, memory admission, metrics (`server/app/api/v1/routes/` holds the endpoints) |
| `server/repl/` | Scala/ML Isabelle REPL backend (PIDE sessions, one shared gateway JVM); launched only by the server |
| `client/` | Async Python HTTP client (`PoolAsyncClient`); its own package (`pip install -e ./client`), httpx only, never imports server code |
| `mcp_servers/` | MCP servers for LLM agents: `stepwise/` (chunk-centric, `verify_chunk` as the one execution tool), `lsp/` (file-sync, LSP-style — tools take a file path), `common/` shared bits |
| `deploy/` | `Dockerfile` (one file, every Isabelle version via `ISABELLE_VERSION`), `setup.sh`, turnkey-image scripts, Prometheus/Grafana/cAdvisor configs (`docker-compose.yml` stays at the root) |
| `evaluation/` | Benchmark CLIs, `results/benchmark_runs.json` (consolidated runs), `MCP-comparison/` harness |
| `examples/` | `demo.ipynb` API walkthrough (HTTP client + both MCP servers), figures, heap demo project |
| `tests/` | Unit tests (no Isabelle needed; ~390 tests) |
| `docs/` | `DESIGN_CHOICES.md` (rationale), `ISSUES.md` (bug log + dated work log), `devnote.md` → `experiments/` (MCP-comparison experiment logs) |
| `archive/` | Read-only history: IsabelleGym 1.0 sources, the 2.0 in-process gym and its baseline scripts, the unmaintained `install.sh` |

Design rationale for the architecture lives in [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md);
the living bug log is [ISSUES.md](docs/ISSUES.md).

---

## Setup

One command per path. Both end with the same HTTP API on port 8000, and the
[Usage](#usage) section applies to either. Everything the scripts do, step by step, is in
[docs/INSTALL_MANUAL.md](docs/INSTALL_MANUAL.md).

| | A. Docker (recommended) | B. Native Linux server |
|---|---|---|
| Host needs | Docker Engine + Compose v2 (the script can install them on Ubuntu/Debian) | Ubuntu 22.04/24.04 or similar, `sudo` |
| Isabelle, Python | inside the image | installed by the script (`--system`) |
| Memory cap | `mem_limit` in `docker-compose.yml` (cgroup-aware admission gate) | per-process ML heap cap; admission gate measures whole-host RAM |
| Start on boot | `restart: unless-stopped` | systemd unit (`--systemd`) |
| Time to first `healthz` | 10–30 min build + 1–2 min start | ~20 min + 1–2 min start |

Sizing for both: budget **~30 GB disk** (Isabelle + heaps) and **16 GB+ RAM**. Each Isabelle
session is 1.5–4 GB resident; `ISABELLE_POOL_SIZE × ~4 GB` plus one `isabelle build` must
fit. The project targets **Isabelle2026** (the 2026 release candidates also build;
Isabelle2025-2 and earlier are not supported: the PIDE header API changed in 2026-RC1).
Heaps are version-locked — never share a user-data directory or volume
between two Isabelle versions.

### A. Docker

```bash
git clone https://github.com/Deep-LCF/Isabelle-Pool-Server.git
cd Isabelle-Pool-Server
./deploy/setup.sh --verify                      # add --install-docker on a bare Ubuntu/Debian box
```

The script creates `.env` (with a random `ISABELLE_ADMIN_TOKEN`), builds the image
(first time: Isabelle download + backend compile, 10–30 min, output in `logs/setup.log`),
starts the container, waits for `/healthz`, and with `--verify` proves `lemma True by simp`
through the API. Other flags: `--build-heaps "HOL-Library HOL-Analysis"` prebuilds session
heaps for heavy imports; `--no-build` restarts without rebuilding (refused when the image was
built for another Isabelle than `.env` names); `--isabelle-version Isabelle2026` picks the
Isabelle to build and writes it to `.env`. After the start the script checks that the
container runs that version. Then:

- **Tune `.env`** if needed — `ISABELLE_POOL_SIZE` (sessions, ~4 GB each),
  `ISABELLE_INITIAL_SESSIONS`, `ISABELLE_MEMORY_PRESSURE_THRESHOLD`, `ISABELLE_ML_MAXHEAP_MB`,
  `ISABELLE_ADMIN_TOKEN`, `ISABELLE_PARALLEL_SESSION_CREATE` (default `true`: sessions are created
  concurrently; `false` creates them one at a time); apply with `docker compose up -d --force-recreate isabelle-pool-server`.
- **Operate:** `docker compose logs -f isabelle-pool-server` (also `logs/server.log`),
  `docker compose stop isabelle-pool-server`, upgrade with `git pull && ./deploy/setup.sh`;
  a new Isabelle: `./deploy/setup.sh --isabelle-version <V>` (heaps are per version, rebuild
  them with `--build-heaps`).
- **Share a ready-made image** (no build, heaps included): `./deploy/export_turnkey.sh --save`
  ([manual A6](docs/INSTALL_MANUAL.md#a6-turnkey-image-no-build-heaps-included)).

### B. Native Linux server

```bash
git clone https://github.com/Deep-LCF/Isabelle-Pool-Server.git ~/Isabelle-Pool-Server
cd ~/Isabelle-Pool-Server
./deploy/native_setup.sh --system --systemd --verify
```

`--system` installs the apt packages and Isabelle (`/opt/<version>`, with `/opt/isabelle` a
symlink to the one in use; `--isabelle-version` picks another release and switches the
symlink, keeping the old install); the script then creates `.venv` and installs the server + client,
registers the Isabelle component and compiles the Scala backend, writes the ML heap cap into
the Isabelle user settings, creates `.env`, and with
`--systemd` installs and starts the `isabelle-pool-server` service; `--verify` proves
`lemma True by simp` against it. Output goes to `logs/native_setup.log`. Without `--system`
it expects `isabelle` on `PATH` (or `ISABELLE_HOME`); without `--systemd` it prints the
foreground run command. Re-run the script after `git pull` or an Isabelle upgrade.

---

## Usage

Everything below is identical for both setups. The server listens on `0.0.0.0:8000` with
**no authentication** — keep it firewalled (`sudo ufw allow from <your-ip> to any port 8000`)
or tunnel over SSH (`ssh -N -L 8000:localhost:8000 user@server`).

### 1. Health, docs, admin

```bash
curl http://localhost:8000/healthz     # liveness: {"status":"alive"}
curl http://localhost:8000/readyz      # readiness: 200 once the gateway JVM is up, else 503
curl http://localhost:8000/            # version, gateway_alive, active/busy sessions, memory
```

- Interactive API docs (Swagger UI): `http://localhost:8000/docs`; raw spec at `/openapi.json`.
- Admin console: `http://localhost:8000/admin` — pool contents, heap images, force-close.
  Destructive actions need `ISABELLE_ADMIN_TOKEN` from `.env` (without it they render
  locked). The console page itself is unauthenticated — one more reason to firewall the port.
- Prometheus metrics: `/metrics`.
- Memory facts: `GET /api/v1/memory` — tier (`ok` / `warning` / `critical`), the cgroup snapshot
  (the container's cgroup, or on a native host the server's own cgroup v2 directory, so a systemd
  `MemoryMax` limit is what the gate enforces),
  builds in flight, and every session's expiry and attributed memory (`rss_mb` of its ML process,
  `heap_mb`). Every response also carries an `X-Memory-State` header; every 503 body has an
  `error` key (`memory_pressure` with the same facts, `pool_full`, `gateway_unavailable`); step /
  chunk / document results carry `error_kind` (`memory` | `prover_died` | null). Leased sessions
  are never evicted — clients are reminded instead (see §4).
- Sledgehammer: `POST …/sledgehammer` and `…/sledgehammer_at` report `success` / `found` only when a
  proof was found (a `Try this:` line) and expose `outcome` (`proved` | `none` | `timeout` | `error`) and
  `method` (the first suggestion, timing cut); "No proof found" used to count as success. `POST …/fill_sorries`
  starts the server-side sledgehammer job on every `sorry` of a submitted proof (poll `GET`, cancel
  `DELETE`): sequential per session, tier-aware slots (2 at ok, 1 at warning, parked at critical), a
  deterministic ETA, and the patched proof text in the result. The LSP MCP wraps it as
  `isabelle_fill_sorries` / `isabelle_fill_status` / `isabelle_fill_cancel` (mcp_servers/lsp/README.md), and
  `GET /api/v1/memory` rows attribute the solvers a step spawns to its session (`sledgehammer_mb`).

### 2. The HTTP API in four calls

Sessions are **leased**: `acquire` gives you a `session_id` and a `lease_id`; every mutating
call carries the lease in the `X-Lease-Id` header; `release` hands the session back to the
warm pool for reuse. This is exactly what `deploy/setup.sh --verify` does.

```bash
# 1. acquire a warm session (or create one) on HOL with Main imported
RESP=$(curl -s -X POST localhost:8000/api/v1/sessions/acquire \
  -H 'Content-Type: application/json' -d '{"theories": ["Main"], "field": "HOL"}')
SID=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
LEASE=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')

# 2. begin a theory in it (the server generates the `theory Scratch imports Main begin` header;
#    a fresh session has no theory begun — verify_chunk answers "theory not begun" without this)
curl -s -X POST "localhost:8000/api/v1/sessions/$SID/enter_theory/Scratch" \
  -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" -d '{"imports": ["Main"]}'

# 3. verify a whole proof chunk under one wall budget (seconds)
curl -s -X POST "localhost:8000/api/v1/sessions/$SID/verify_chunk" \
  -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" \
  -d '{"chunk": "theorem t: \"rev (rev xs) = xs\" by (induct xs) auto", "timeout": 60}'
# → {"success": true, "proof_open": false, "pending_qed": false, "used_sorry": false,
#    "timed_out": false, "stuck_line": null, "execution_time": ...,
#    "commands": [{"index": 0, "line": 1, "kind": "theorem", "status": "ok", ...}]}

# 4. release the lease (the session stays warm for the next caller)
curl -s -X POST "localhost:8000/api/v1/sessions/$SID/release" -H "X-Lease-Id: $LEASE"
```

The first `acquire` for an import set pays session creation (~1 min; longer if a heap has
to be built — prebuild heaps, see docs/INSTALL_MANUAL.md). A `timeout` on `verify_chunk`/`commands` is a hard wall budget:
an expired command is rolled back and reported `success=false` with `stuck_line` naming
the still-running command — nothing keeps running in the background.

**The one rule for judging results:** a theorem is proved only when
`success=true` **and** `proof_open=false` **and** `used_sorry=false`. `success` alone means
"no command errored" — an open or `sorry`-closed proof is not a result.

Other endpoints (all under `/api/v1/sessions/{id}`, lease required): `POST commands` (one
Isar command, small-step), `GET state` / `subgoals` / `goals` / `source` / `facts/local` /
`facts/global`, `POST checkpoints` / `checkpoints/{id}/restore` / `rollback`,
`POST diagnostic` (read-only `thm`/`term`/`find_theorems`), `POST sledgehammer`,
`PUT document` (replace the whole theory text, incremental sync), positional queries
(`command_at_line`, `hover`, `definition`, `sledgehammer_at`). Lease-free:
`POST /api/v1/sessions/bigstep` (whole-theory `isabelle build`),
`POST /api/v1/parse_theory_header`, `GET /api/v1/heaps/available`. Browse them at `/docs`.

### 3. The Python client

`client/` is a standalone package (httpx only) — install it where your agent runs:

```bash
pip install -e ./client        # or copy the client/ directory into your project
```

```python
import asyncio
from client import PoolAsyncClient

async def main():
    async with PoolAsyncClient("http://localhost:8000") as gym:
        s = await gym.acquire_session(theories=["Main"], field="HOL")
        sid, lease = s["session_id"], s["lease_id"]
        try:
            r = await gym.verify_chunk(
                sid, 'theorem t: "rev (rev xs) = xs" by (induct xs) auto',
                timeout=60, lease_id=lease)
            proved = r["success"] and not r["proof_open"] and not r["used_sorry"]
            print("proved:", proved)
            if not proved:                      # ask sledgehammer on the open goal
                print(await gym.sledgehammer(sid, lease_id=lease))
        finally:
            await gym.release_session(sid, lease_id=lease)

asyncio.run(main())
```

Same surface as the HTTP API: `execute_command`, `get_proof_state`, `save_checkpoint` /
`restore_checkpoint` / `rollback`, `diagnostic`, `load_document`, `verify_bigstep_text` /
`verify_bigstep_file`. [examples/demo.ipynb](examples/demo.ipynb) walks through all of it
end to end, including both MCP servers.

### 4. Connecting an LLM agent (MCP)

The MCP servers are thin agent-facing layers over the HTTP API:

```
LLM agent  ⇄  MCP server (stdio or streamable-HTTP)  ⇄  Isabelle Pool Server  ⇄  Isabelle
```

There are two, for two agent shapes (rationale: [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) §2.9):

| Server | Launch | Shape |
|---|---|---|
| `mcp_servers.stepwise` | `python -m mcp_servers.stepwise.app` | **Chunk-centric.** The agent submits Isar text; `verify_chunk` is the one execution tool, failed text rolls back. Env prefix `ISABELLE_MCP_`, HTTP port 8848. Walkthrough below. |
| `mcp_servers.lsp` | `python -m mcp_servers.lsp.app` | **File-sync (LSP-style).** Tools take a `file_path`; the file is re-read from disk before every query and the broken state is kept for inspection. Env prefix `ISABELLE_MCP_LSP_`, HTTP port 8849. See [mcp_servers/lsp/README.md](mcp_servers/lsp/README.md). |

Sessions and leases are managed automatically per MCP connection — the agent never sees a
`session_id`. Each connection gets an isolated Isabelle session; a fresh `enter_theory`
always starts from a clean document.

**Memory protocol.** The server reports facts, the MCP words the advice. Each MCP server keeps a
registration list of what it holds (theory / file / scratch sessions, idle time, expiry,
attributed memory; `list_my_sessions` / `isabelle_list_sessions`, display-only `ref`s). A call
refused for memory comes back as `{"error": "memory_pressure", "your_sessions": [...],
"others": {...}, "advice": [...]}` — release what you hold but do not use
(`close_theory()`, `isabelle_release_idle()`, `isabelle_close(file, destroy=true)`), retry
after `retry_after_s`, and when other clients, builds or external pressure hold the memory,
stop and tell the user. While the server is at `warning` / `critical` every result carries a
`memory_reminder`; `memory_status` / `isabelle_memory_status` show who holds what. Rationale:
[DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) §1.6 and §2.10.

#### Prerequisites (on the machine that runs the agent / MCP client)

```bash
cd Isabelle-Pool-Server
pip install -e ".[mcp]" -e ./client   # MCP SDK (mcp<2) + the async client, from the pyproject.toml extras
```

The MCP server needs `PYTHONPATH` pointing at the repo root (so `client` and `mcp_servers`
import) and the gym server URL (default `http://localhost:8000`). The gym server must be
running.

#### Option A — stdio (local agents: Claude Code, Claude Desktop, Cursor)

The client spawns the MCP server as a subprocess; one process per connection.

**Claude Code** (either command works):

```bash
claude mcp add isabelle-pool-server \
  --env PYTHONPATH=/absolute/path/to/Isabelle-Pool-Server \
  --env ISABELLE_MCP_GYM_URL=http://localhost:8000 \
  -- python -m mcp_servers.stepwise.app
```

or drop a `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "isabelle-pool-server": {
      "command": "python",
      "args": ["-m", "mcp_servers.stepwise.app"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/Isabelle-Pool-Server",
        "ISABELLE_MCP_GYM_URL": "http://localhost:8000"
      }
    }
  }
}
```

**Claude Desktop:** the same JSON block under `mcpServers` in
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows). **Cursor:** same block in
`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global). **Your own agent loop:**
spawn `python -m mcp_servers.stepwise.app` over stdio with those two env vars;
`evaluation/MCP-comparison/common/mcp_client.py` is a minimal working example with the
`mcp` Python SDK (spawn → `initialize` → `tools/list` → `tools/call`).

#### Option B — streamable-HTTP (remote server, multiple agents)

Run the MCP server as a standalone service next to the gym server:

```bash
cd Isabelle-Pool-Server
PYTHONPATH=. ISABELLE_MCP_TRANSPORT=streamable-http \
  ISABELLE_MCP_HOST=0.0.0.0 ISABELLE_MCP_PORT=8848 \
  python -m mcp_servers.stepwise.app
```

Point HTTP-capable MCP clients at `http://<server>:8848/mcp`. Concurrent connections are
isolated from each other (per-connection sessions). Like the gym API there is no built-in
auth — firewall the port or tunnel (`ssh -N -L 8848:localhost:8848 user@your-server`).

#### What the agent gets (stepwise server)

| Tool | Purpose |
|---|---|
| `enter_theory(name, imports, field)` | Start (or restart) a proof session; begins the theory header for you |
| `verify_chunk(text, timeout, detail)` | **The one execution tool.** Submit one command or a whole proof; returns per-command status (`ok/failed/running/unprocessed`), names the stuck line on timeout, auto-rolls-back failures |
| `proof_state()` | Current open subgoals |
| `source()` | The theory source as the prover sees it |
| `diagnostic(command)` | Read-only queries (`thm`, `term`, `find_theorems`, `print_*`) — transient, never touches the proof |
| `sledgehammer(timeout_s)` | Automated proof search on the open goal; returns ready-to-paste methods |
| `checkpoint()` / `restore(id)` / `rollback()` | State management |
| `verify_batch(items, max_parallel)` | Check many independent chunks concurrently across the session pool |
| `close_theory()` | Dispose this connection's session |

A typical agent flow:

```
enter_theory(name="Scratch", imports=["Main"])
verify_chunk("theorem foo: \"rev (rev xs) = xs\"\n  by (induct xs) auto")
  → success=True proof_open=False used_sorry=False   ✓ proved
# or, when stuck:
verify_chunk("theorem bar: ...\nproof -\n  have step1: ... by simp")
  → success=True proof_open=True                     (open goal kept)
sledgehammer()                                        → "by (metis ...)"
verify_chunk("  show ?thesis by (metis ...)\nqed")
```

#### MCP configuration (env vars, stepwise server)

| Variable | Default | Meaning |
|---|---|---|
| `ISABELLE_MCP_GYM_URL` | `http://localhost:8000` | The gym HTTP server |
| `ISABELLE_MCP_FIELD` | `HOL` | Default Isabelle session for new theories |
| `ISABELLE_MCP_CHUNK_TIMEOUT` | `180` | Default wall budget (s) per `verify_chunk` |
| `ISABELLE_MCP_HTTP_TIMEOUT` | `600` | httpx timeout (must exceed chunk timeout) |
| `ISABELLE_MCP_MAX_PARALLEL` | `4` | Cap for `verify_batch` fan-out |
| `ISABELLE_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ISABELLE_MCP_HOST` / `ISABELLE_MCP_PORT` | `127.0.0.1` / `8848` | HTTP transport bind |

The lsp server takes the same names with the `ISABELLE_MCP_LSP_` prefix (plus its own
`LOAD_TIMEOUT`, `ATTEMPT_TIMEOUT`, `SCRATCH_POOL_SIZE`, `TASK_GROUP`, `CLOSE_DESTROYS`).

#### MCP smoke test

With the gym server running, verify the MCP layer end-to-end without any agent:

```bash
cd Isabelle-Pool-Server
PYTHONPATH=. python - <<'EOF'
import asyncio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(command="python", args=["-m", "mcp_servers.stepwise.app"],
                                   env={"PYTHONPATH": ".", "ISABELLE_MCP_GYM_URL": "http://localhost:8000"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            await s.call_tool("enter_theory", {"name": "Smoke", "imports": ["Main"]})
            out = await s.call_tool("verify_chunk",
                {"text": 'theorem t: "rev (rev xs) = xs" by (induct xs) auto'})
            print(out.content[0].text)
            await s.call_tool("close_theory", {})

asyncio.run(main())
EOF
```

Expected: the tool list, then `success=True proof_open=False used_sorry=False ...`.

### 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/readyz` stays 503 for minutes | Gateway JVM still starting (1–2 min normal), or it died — check `logs/gateway-jvm.log`; natively also `isabelle components -l` and re-run `server/repl/Admin/init` |
| `Not found: py4j` on start (Docker) | Stale `isabelle_user_data` volume — `docker compose exec isabelle-pool-server ./server/repl/Admin/init` (Bug 7) |
| `Fontconfig head is null` in the JVM log (native) | Install `fontconfig fonts-dejavu-core` (`native_setup.sh --system` does) |
| `Unknown JAVA_HOME` from `isabelle` (native) | A global `JAVA_HOME` is exported; Isabelle resolves its own JDK — unset it in the server's environment |
| HTTP 503 "memory pressure" | The admission gate is protecting the host — lower `ISABELLE_POOL_SIZE`, raise `mem_limit`, or wait for idle sessions to be evicted |
| First `acquire` takes many minutes | A heap for a heavy import set is being built — prebuild it (`setup.sh --build-heaps` / `isabelle build -b`) |
| `verify_chunk` returns `success=false, timed_out=true` | The budget expired; `stuck_line` names the looping command; it was rolled back — retry with a larger `timeout` or a different proof |
| `McpError: Connection closed` immediately | The MCP subprocess died on startup — missing `PYTHONPATH` or pip deps; run `PYTHONPATH=. python -m mcp_servers.stepwise.app` by hand to see the traceback |
| `enter_theory` hangs then errors | Gym server not running / wrong `ISABELLE_MCP_GYM_URL`, or a heap is being built |
| `success=True` but the agent isn't done | Working as intended — check `proof_open` / `used_sorry` |

---

## Beyond the basics

- **MCP comparison harness** (this MCP vs Isabelle-MCP vs AutoCorrode I/Q):
  [evaluation/MCP-comparison/README.md](evaluation/MCP-comparison/README.md); experiment
  write-ups under [docs/experiments/](docs/experiments/).
- **Evaluation scripts** for small-step/big-step benchmarking: `evaluation/scripts/`
  (each runs as `python -m evaluation.scripts.<name>`); consolidated results in
  [evaluation/results/](evaluation/results/README.md).
- **Developer docs:** [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) (architecture,
  conventions, env reference), [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) (rationale),
  [ISSUES.md](docs/ISSUES.md) (bug log). The older API/client reference PDFs and the
  1.0/2.0 reports live in `archive/previous-works/`.
- **Running the tests:** `pytest` from the repo root (~390 unit tests, no Isabelle needed).
