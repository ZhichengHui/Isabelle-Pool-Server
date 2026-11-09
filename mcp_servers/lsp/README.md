# mcp_servers.lsp — LSP-like MCP server (file-sync workflow)

An MCP server for agent-driven Isabelle development over *files*, in the spirit
of lean-lsp-mcp: tools take a `file_path` (never a session id), the file is
re-synced from disk before every query, and execution happens on warm scratch
sessions — never on the file's session. Built strictly on top of
`PoolAsyncClient` (no server-core edits); needs the Isabelle Pool Server HTTP
server running (`docker compose up -d`, `python -m server.app.main` in the
container, port 8000).

**Positions everywhere are 1-based line/column, UTF-16 columns** (editor/LSP
convention, matching the server's report `range` fields).

## Server instructions and upfront tools

The server sends MCP server instructions (`_INSTRUCTIONS` in `server.py`): what the server
does and the tools behind its key capabilities (incremental checking, `isabelle_multi_attempt`,
sledgehammer and `isabelle_fill_sorries`, `still_running`, inspection). Claude Code's tool
search loads only tool names and server instructions at session start. `isabelle_open`,
`isabelle_diagnostic_messages` and `isabelle_goal` carry `_meta` `anthropic/alwaysLoad`, so
Claude Code loads them upfront; the other tools stay deferred.

## Skill for agents

`skills/isabelle-proof-development/SKILL.md` is a Claude Code skill on developing proofs with this
MCP: locating with `isabelle_diagnostic_messages` and iterating with `isabelle_multi_attempt`, the
order quick attempts → `isabelle_sledgehammer` → `sorry` skeleton with `isabelle_fill_sorries`,
not guessing `metis` calls, a 10-second limit per proof step and what to do on `still_running`,
searching with `isabelle_query`, and running the batch build only as the final check. Install it by copying the directory to `~/.claude/skills/` (or a project's
`.claude/skills/`). It draws on the public `isabelle-proof-development` skill, kappelmann's
`isabelle` skill (isabelle-pide-ai), yonoteam's `proving-with-sorry-skeletons` and AutoCorrode's
`ic2` skill, mapped onto this MCP's tools.

## Sync model (copied buffer)

The MCP never writes files. Per open file it keeps a text cache; **every
file-scoped tool call first re-reads the file from disk**; if the text changed
since the last sync, the new text is pushed via `load_document(report=true)`
(the lean-lsp-mcp `reload_from_disk` analog). If the server evicted the session
(404), the binding is recreated transparently and the file reloaded.

A load waits until every command is processed, at most `..._LOAD_TIMEOUT`. When it
times out, the results of `isabelle_open`, `isabelle_sync` and every file-scoped
tool that are JSON objects get `still_running`: the commands still running (line,
kind, `running_for_s`, the seconds since this MCP first saw the command running)
and a note. Their results are missing; other commands may be incomplete (PIDE
checks later proofs in parallel, so their errors can still appear). A command that
does not terminate makes every later check of the file wait the full budget until
it is changed. The next call reloads the file even if it is unchanged, so that it
does not answer from the report of the timed-out load.

**Security**: like the HTTP API it wraps, this server has NO authentication —
the streamable-http transport binds 127.0.0.1 by default; deploy behind a
firewall or SSH tunnel, never expose the port publicly.

## Tools

Lifecycle:
| Tool | What it does |
|---|---|
| `isabelle_open(file_path, task_group?, heap_session?, label?)` | Bind a file to a session (auto-acquired on first use with defaults if not called; released same-dependency-key sessions are reused warm — every sync resets via load_document, so dirty reuse is safe); heap context chosen here. The acquire passes the file's own header imports as `theories`, so imports beyond `Main` (Complex_Main, `"HOL-Analysis.Derivative"`, …) resolve — see the theories-at-acquire fix |
| `isabelle_close(file_path, destroy?)` | Release the session and unbind. With `destroy=true` (or `ISABELLE_MCP_LSP_CLOSE_DESTROYS=true`) the session is torn down immediately instead of released warm — the sanctioned way to free a multi-GB session; reopening the file rebinds transparently |
| `isabelle_sync(file_path)` | Force a disk→session sync check (normally implicit) |

Memory (see "Memory protocol" below):
| Tool | What it does |
|---|---|
| `isabelle_list_sessions()` | What this MCP holds (open files + warm scratch sessions): idle time, expiry, memory per session (`ref` is display-only) |
| `isabelle_memory_status()` | Server memory state, used/limit, what you hold vs. what others hold, advice |
| `isabelle_release_idle(max_idle_s?)` | Close idle warm scratch sessions; with `max_idle_s > 0` also release idle open files (opt-in: next query reloads) |

Read-only (each syncs first):
| Tool | What it does |
|---|---|
| `isabelle_diagnostic_messages(file_path, severity?)` | Per-command errors/warnings with line/col ranges + status (severity filter: `error`/`warning`) |
| `isabelle_goal(file_path, line)` | Goal state before/after the command that starts at the first non-blank character of `line` (a line inside a command, blank or a comment maps to the command before it); needs `ISABELLE_SHOW_STATES=true` (the default), otherwise the result carries an error instead of empty goals |
| `isabelle_command_at_line(file_path, line)` | The command containing `line` (+ range) |
| `isabelle_proof_state(file_path)` | Tip subgoals / proof_finished / pending_qed |
| `isabelle_source(file_path)` | The source as the prover sees it |
| `isabelle_query(file_path, command)` | One read-only query command — `thm`, `term`, `prop`, `typ`, `prf`, `find_theorems`, `find_consts`, `find_*`, any `print_*` (guarded server-side; ML/IO rejected). Returns only the query's own output; runs before the file's final `end` (or after the last command if there is none), so facts of a closed `context`/locale need their qualified name |
| `isabelle_local_facts(file_path)` / `isabelle_global_facts(file_path, limit?)` | Proof-context vs theory-level facts |
| `isabelle_hover_info(file_path, line, column)` | Entity kind + type/statement |
| `isabelle_definition(file_path, line, column)` | Def positions: file targets (heap/distribution sources) or in-node line ranges (entities defined in the file itself) |
| `isabelle_sledgehammer(file_path, line?, subgoal?, timeout_s?)` | Tip goal (no `line`) or positioned at `line` (+subgoal) |
| `isabelle_fill_sorries(file_path, timeout_s?, max_total_s?, patience_s?)` | The server runs sledgehammer on every `sorry` of the file (one job per file, `timeout_s` per sorry, `max_total_s` per job) and returns `patched_text` + per-sorry results, ETA and queue position; after `patience_s` (default 240) it returns the partial state with `continuing: true` while the job keeps running server-side |
| `isabelle_fill_status(file_path)` / `isabelle_fill_cancel(file_path)` | Collect a continuing job (same result shape; polling keeps the session alive) / stop it (the step in flight ends within its own budget; proofs found so far stay in `patched_text`) |
| `isabelle_checkpoint(file_path)` / `isabelle_restore(file_path, checkpoint_id)` / `isabelle_rollback(file_path)` | Snapshot machinery on the file session |
| `isabelle_history(file_path)` / `isabelle_last_report(file_path)` | Command history / retained last report |

Scratch execution (never touch the file session; warm pool keyed by
task_group/heap/imports/field, `ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE`):
| Tool | What it does |
|---|---|
| `isabelle_multi_attempt(file_path, line, candidates, timeout?)` | File prefix before `line` + each candidate, verified concurrently on scratch sessions (bounded by `ISABELLE_MCP_LSP_MAX_PARALLEL`); `line` is 1 to the line count + 1 (the last value appends after the end of the file), else an error; per-candidate success/proof_open/pending_qed, `used_sorry` of the candidate itself (`prefix_used_sorry` when the text before `line` has one), `failed` commands with complete error messages (candidate-relative lines; `prefix_failed` for the text before `line`) and, for an open proof, the remaining `subgoals` |
| `isabelle_run_code(chunk, imports?, task_group?, heap_session?, timeout?)` | Independent snippet on a scratch session (own `theory` header or body of a scratch theory importing `imports`, default Main) |

Spare scratch sessions (`scratch_spare.py`, `ISABELLE_MCP_LSP_SCRATCH_SPARE`, default 4, 0 = off):
after a file is first bound, and again after each `isabelle_multi_attempt`, the MCP prepares in
the background min(`..._SCRATCH_SPARE`, `..._MAX_PARALLEL`) idle scratch sessions for the file's
context (within `..._SCRATCH_POOL_SIZE`), already loaded with the file's text. The candidates of
a multi_attempt run in parallel, so the call is as slow as its slowest candidate: spares help only
if every candidate finds one, which is why as many are kept as candidates can run at once
(measured on an AutoCorrode file: 1 candidate with a spare 0.4 s, without 40 s; 2 candidates with
only 1 spare still 39 s). A candidate on a spare neither waits for a new session
nor checks the prefix from the start (its load takes the server's incremental path). The context
comes from the file's own header, so nothing is configured per heap. Each spare is a held Isabelle
session (memory; the memory reminder closes idle ones first). A refused or failed preparation
(memory pressure, the pool at its cap) only means fewer spares.

Heap pool:
| Tool | What it does |
|---|---|
| `isabelle_build_heap(task_group, project, session_name?)` | `isabelle build -b` the project's top-level .thy files into a verified heap (long-running) |
| `isabelle_heap_status(task_group?, project?)` | With both args: the full manifest (file hashes, ROOT text, fingerprint, log tail). Otherwise: pool listing **plus `available_heaps`** — every base session image on disk (HOL-Analysis, distribution heaps, pool-built), so you can see what sessions can start from before naming `heap_session`/`field` anywhere |

Server endpoints the agent may also want (REST, same server the MCP wraps):
`GET /api/v1/heaps/available` (the raw `available_heaps` listing) and
`POST /api/v1/parse_theory_header {text}` → `{theory_name, imports,
suggested_field}` — the server's canonical header parse (comment-stripped,
header-anchored); use it instead of a local regex so all consumers share one
parser (client wrapper: `async_client.parse_theory_header`).

## Configuration (`ISABELLE_MCP_LSP_*` env vars)

| Var | Default | Meaning |
|---|---|---|
| `..._GYM_URL` | `http://localhost:8000` | Isabelle Pool Server |
| `..._FIELD` / `..._TASK_GROUP` | `HOL` / `default` | session image bindings and scratch sessions start on / default heap task group |
| `..._FIELD_FROM_IMPORTS` | false | start on the image the server's header parse suggests for the file's dotted imports (`suggested_field`: the largest distribution image named) instead of `..._FIELD`; an image the server lacks is built on first use, so prebuild the big ones |
| `..._HTTP_TIMEOUT` | 600 | httpx timeout (s) |
| `..._LOAD_TIMEOUT` | 30 | load_document sync budget (s); see `still_running` above |
| `..._ATTEMPT_TIMEOUT` | 180 | per-candidate/run_code budget (s) |
| `..._MAX_PARALLEL` | 4 | multi_attempt fan-out cap |
| `..._SCRATCH_POOL_SIZE` | 4 | warm scratch sessions per context |
| `..._SCRATCH_SPARE` | 4 | idle scratch sessions kept ready per context, loaded with the file: min(this, `MAX_PARALLEL`) (0 = off) |
| `..._CLOSE_DESTROYS` | false | `isabelle_close` destroys (teardown) instead of warm-releasing |
| `..._FILL_TIMEOUT_S` / `..._FILL_MAX_TOTAL_S` | 30 / 600 | fill_sorries: sledgehammer budget per `sorry` / wall budget per job (server defaults `ISABELLE_FILL_*`) |
| `..._FILL_PATIENCE_S` / `..._FILL_POLL_S` | 240 / 3 | how long `isabelle_fill_sorries` waits before returning `continuing: true` (keep ≤ the MCP client's tool timeout − 60) / poll interval |
| `..._TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `..._HOST` / `..._PORT` | `127.0.0.1` / `8849` | streamable-http bind |

## Run

```bash
pip install -e ".[mcp]" -e ./client   # from the repo root: MCP SDK (mcp<2) + the async client
PYTHONPATH=. python -m mcp_servers.lsp.app                                   # stdio
ISABELLE_MCP_LSP_TRANSPORT=streamable-http PYTHONPATH=. python -m mcp_servers.lsp.app   # HTTP on :8849
```

## Notes / deferrals (v1)

- Header imports must be qualified heap names for heap-backed files
  (`imports "MySession.Bar"`); bare project-relative names fail the header gate
  (server-side normalization is a documented deferral).
- Scratch sessions are held leased for reuse; they are reclaimed by the
  server's abandoned-lease reaper if the MCP dies.
- No completions / code actions / widgets yet.

## Module layout

`server.py` is the import point for the shared objects (`mcp`, `pool`, `TOOLS`, the `tool` /
`synced_tool` decorators); `app.py` holds the file/scratch/heap tools and `main()`,
`memory_tools.py` the memory tools, `fill_tools.py` the fill_sorries tools. New code imports from `server.py`, not through `app.py`
(which only re-exports for compatibility).

## Memory protocol

The server never evicts a leased session (bindings and scratch sessions are leased — they are
this MCP's to release) and never words advice; it reports **facts**: every 503 body has an
`error` key (`memory_pressure` with the numbers and the sessions involved, `pool_full`,
`gateway_unavailable`), every response carries `X-Memory-State` (ok / warning / critical), and
`GET /api/v1/memory` lists every session's expiry and attributed memory. This server keeps a
**registry** of what it holds (session id and lease stay private; the agent sees a display-only
`ref`), turns a memory 503 into `{"error": "memory_pressure", "your_sessions": [...],
"others": {...}, "advice": [...]}`, and while the server is at warning/critical attaches a
`memory_reminder` to results and closes its idle warm scratch sessions first. Advice, in order:
release what you hold but do not use (`isabelle_release_idle()`, `isabelle_close(file,
destroy=true)`), retry after `retry_after_s`, and — when the memory is held by other clients,
builds, or external pressure — stop and tell the user. While it runs, the MCP renews the lease of
every session it holds (`POST /api/v1/sessions/{id}/renew_lease`, every
`ISABELLE_MCP_LSP_LEASE_RENEW_S`, default 60 s), so the server's abandoned-lease rule
(`ISABELLE_MAX_LEASE_AGE`) closes only the sessions of a client that died; warm scratch sessions
are kept however long they are idle and let go on memory pressure. Knob for advice:
`ISABELLE_MCP_LSP_ADVICE_IDLE_S` (default 120 s).

### Sledgehammer on `sorry`s (fill_sorries)

Instead of calling `isabelle_sledgehammer` goal by goal, the agent writes the proof with
`sorry` at every goal it cannot close and calls `isabelle_fill_sorries(file_path)`: the server
locates the `sorry` commands, runs sledgehammer on each one (`max_proofs=1`, one step at a time
per file) and the tool returns `patched_text` with every proved `sorry` replaced (a `by …`
answer verbatim; several single-method answers for a multi-subgoal `sorry` as an `apply` chain
+ `done`), the per-sorry outcomes and the lines still `sorry`. The agent writes the text to the
file and verifies with `isabelle_diagnostic_messages`. The server caps the sledgehammers it runs
by memory tier — 2 at `ok`, 1 at `warning`, none at `critical`, where a job is **parked**
(`parked: true`, `parked_reason`) until the tier drops — and reports a deterministic ETA
(`eta_s.typical` = remaining × 6 s, `eta_s.worst` = remaining × (`timeout_s` + 30) + queue wait)
and the job's `queue_position` (−1 = not queued). The tool waits `ISABELLE_MCP_LSP_FILL_PATIENCE_S`
(240 s; keep it ≤ the MCP client's tool timeout − 60) and then returns the partial state with
`continuing: true`; the job keeps running server-side until `max_total_s` —
`isabelle_fill_status(file_path)` collects it (each poll counts as activity on the session) and
`isabelle_fill_cancel(file_path)` stops it. While a step runs, other tools on the same file
answer `busy`. The ATP processes a step spawns are attributed to the file's session as
`sledgehammer_mb` (the server's `/api/v1/memory` rows; `isabelle_list_sessions` shows it once a
refresh has merged the rows, and `isabelle_memory_status` counts what other clients' running
sledgehammers hold).
