# DESIGN_CHOICES.md

Design rationale for Isabelle Pool Server (IsabelleGym 3.0 until its renaming on 2026-10-05), written down after the fact so the choices — and the
roads not taken — survive the people who made them. Format per choice: what we chose, what
the old/alternative option was, pros and cons of each, why the chosen option won, and what
possibilities the choice deliberately gave up. Bug numbers refer to ISSUES.md; deeper
histories live in the internal working notes (not tracked).

---

## 1. Isabelle Pool Server — from local REPL gym to a concurrent server

### 1.1 Deployment model: embedded Python library → HTTP service

**Old:** IsabelleGym 2.0 was an in-process Python library. Every training/eval worker
embedded its own REPL and spawned its own Isabelle.
**Chosen:** a FastAPI HTTP service (`server/`) owning all Isabelle state; clients speak REST.

- *Embedded pros:* no network hop, no serialization, trivially simple to start.
- *Embedded cons:* every worker pays full Isabelle startup; no sharing of warm sessions; no
  central resource control (N workers = N uncoordinated multi-GB `poly` processes); Python-only
  clients; nothing to monitor.
- *Server pros:* one warm pool shared by all clients; central admission control, metrics
  (`/metrics`, Grafana), and crash recovery; language-agnostic (the MCP server, eval scripts,
  and curl are all just HTTP clients); containerizable for cluster use.
- *Server cons:* operational surface (leases, pools, timeouts), request/response overhead,
  and a whole class of concurrency bugs we then had to fix (Bugs 1–8).

**Why:** the target workload is many concurrent LLM agents/rollouts against a resource that
costs gigabytes and tens of seconds per instance. Sharing and gating that resource is the
whole game; an embedded library cannot do either. **Abandoned:** the local install path
(`archive/install.sh`, archived 2026-09-27) is unmaintained — the server assumes the container layout (`/app`,
`/opt/isabelle`) and that is deliberate: one supported layout instead of N half-working ones.

### 1.2 Prover bridge: Scala-side PIDE via Py4J → one gateway JVM

**Chosen:** a Scala program (`server/repl/`, an Isabelle component) using Isabelle's own Scala API (Headless PIDE sessions),
exposed to Python through a single Py4J gateway JVM.
**Alternatives:** (a) the official `isabelle server` JSON/TCP protocol; (b) research bridges
(scala-isabelle / PISA-style); (c) driving `isabelle console` as a subprocess REPL.

- *(a) pros:* stable, documented, no custom Scala. *cons:* too coarse — no per-command status
  inside a document, no transient probes, limited document-edit control; exactly the
  capabilities `verify_chunk`/`diagnostic` are built on.
- *(b) pros:* faster start. *cons:* third-party maintenance risk, and we still would have
  needed custom ML plumbing (subgoal/fact extraction in `REPL.ML`).
- *(c) pros:* trivially simple. *cons:* scraping stdout of a console is fragile, has no
  document model at all, and cannot express rollback/checkpoint semantics.

**Why:** the product differentiator is *fine-grained feedback* (which line failed, which line
is looping, state probes that don't pollute the script). Only the PIDE document model offers
that, and PIDE is only fully reachable from Isabelle/Scala. Py4J then bridges Scala↔Python
with the least ceremony. **Cost accepted:** a hand-maintained mirror (`ReplBackend` Protocol
in `repl_backend_gateway.py`) that must stay in sync with the Scala class.

### 1.3 One shared gateway JVM vs JVM-per-session

**Chosen:** all sessions live in one JVM; each `ReplBackend` owns its own Isabelle session
(own `poly` process and PIDE document), so state queries read that document's own results
and need no per-backend routing. (Until 2026-09-30 a `channel_id` tag routed ML→Scala
channel replies to the right backend; the channels are gone — see 1.14.)
**Alternative:** a JVM per session.

- *Per-session pros:* crash isolation; no cross-session interference.
- *Per-session cons:* a JVM heap per session on top of each `poly` process — memory roughly
  doubles; multi-second JVM spawn per session.
- *Shared pros:* amortized JVM cost; instant backend creation.
- *Shared cons:* shared fate — when sledgehammer bursts OOM-killed the JVM (Bug 6), every
  session died at once.

**Why:** memory is the binding constraint (each session's `poly` is already 1.5–2.5 GB); a
per-session JVM would halve pool capacity. The shared-fate risk was made acceptable by
**gateway crash recovery** (detect dead JVM → purge sessions → rebuild, instead of a bricked
server) and by removing the crash *causes* (sledgehammer semaphore, memory admission).
**Note:** each backend still constructs its own Scala `Session_Manager` (own in-JVM Isabelle
server); a shared variant exists but is unused — consolidation is a known open item.

### 1.4 Backend concurrency: one dedicated worker thread per session

**Chosen:** `ThreadedBackend` — every session's Py4J calls are serialized through a single
worker thread and a job queue.
**Alternatives:** a lock around backend calls; or trusting async/await.

- The Scala backend is not thread-safe and Py4J multiplexes one socket, so *some*
  serialization is mandatory. A plain lock serializes too but lets calls run on arbitrary
  threads (including the event loop — see 1.9) and gives no place to implement drain/reject
  semantics on shutdown. Async doesn't help: the underlying socket calls are blocking.

**Why:** a queue-owning thread gives ordering, a natural shutdown protocol, and a single
choke point for instrumentation. **Hard-won lessons encoded here:** `exit()` must go through
the same queue (calling it directly raced in-flight jobs — Bug 1), and the shutdown guard
must not reject the backend's *own* exit job (Bug 8 — the regression that silently leaked
every session's poly process).

### 1.5 Session lifecycle: LRU pool + exclusive leases + dependency-key reuse

**Chosen:** sessions live in a capacity-capped LRU pool; a client *acquires* a session and
gets an exclusive lease token (`X-Lease-Id`) that all session operations must present;
sessions are reusable across clients when their **dependency key** (sorted imports + field
hash) matches; idle and abandoned-lease sessions are reaped by a background sweep.
**Alternatives:** create-and-destroy per request; sticky client→session affinity without
tokens; no reuse at all.

- *Create-per-request:* correctness by construction, but a 30–60 s session start per request
  is unusable interactively.
- *Sticky affinity without leases:* races the moment two workers guess the same session, and
  leaks sessions when clients die silently.
- *Leases pros:* exclusive use is enforced by the server, not by client discipline;
  expiry (`MAX_LEASE_AGE`) plus force-close bounds the damage of a vanished client. There is
  a formal concurrency proof for acquire/release (`evaluation/Server_Concurrency.thy`).
- *Reuse pros:* amortizes the dominant cost. *cons:* a reused session carries document state.

**Why:** leases give multi-client safety with a self-healing failure mode. On reuse, the
proof-leakage incident (an agent reading a previous attempt's proof via `source()`) taught us
the refinement now used by the MCP layer: **clean-only reuse** (`reuse_dirty=False`) — reuse
is only worth its risk when the session has no command history. **Abandoned:** cross-client
sharing of *dirty* sessions, and the Scala-side session cache (`enable_cache`, default off) —
PIDE-level sharing of one session between backends was never made safe enough to trust.

### 1.6 Memory management: JVM-heap heuristics → cgroup admission control

**Old:** the Scala layer measured `Runtime.getRuntime` JVM heap and made "memory decisions"
that were never actually wired to anything — and measured the wrong thing anyway (each
session's memory lives in a separate `poly` OS process the JVM cannot see).
**Chosen:** a Python `MemoryMonitor` reading the **container cgroup** (the number the OOM
killer actually uses) — or, on a native cgroup-v2 host, the server's **own cgroup directory**
(`/proc/self/cgroup`), so a systemd `MemoryMax` on the unit or a `systemd-run --scope` applies
to the gate (2026-10-20) — corrected by subtracting reclaimable `inactive_file` page cache;
admission control before session creation; under pressure, evict idle LRU sessions (never
busy/leased ones), settle between evictions, retry admission briefly before refusing with 503.

- *Per-session quotas (alternative):* poly offers no cooperative way to cap or account its
  heap from outside; enforcing per-process rlimits kills sessions mid-proof unpredictably.
- *Do nothing / trust the OOM killer (alternative):* the killer takes the shared JVM first
  (biggest RSS) — one greedy request destroys every session (Bug 6's failure mode).

**Why:** admission at create time plus bounded eviction is the only lever that matches where
the memory actually is. The page-cache correction and settle/retry came from the
close-then-create race: teardown → kernel accounting lags → false "no memory" rejections.

**Update 2026-10-17 (memory awareness, plan `claude-work/2026-10-17-plan-memory-mcp-awareness`):**
the cgroup stays the truth for the total; on top of it:

- *Attribution from the ML side (owner decision D2):* a Query_Operation in `REPL.ML`
  (`isabelle_pool_server_mem`) returns the session's ML pid (read from `/proc/self` — `PolyML.ml_pid`
  and `Posix` are hidden from user-level ML by the sanitized name space) and the Poly/ML runtime
  statistics; the server reads the process's resident size (+ sledgehammer children) from `/proc`
  once per sweep and keys "prover alive" on pid + start time. Isabelle itself runs an
  `ML_Statistics.monitor` poly next to every session, so `Session.runtime_statistics` already
  streams the same heap numbers — used since stage D (2026-11-24) by the memory guard
  (`ReplBackend.ml_heap`, backend_memory.scala), which must read the heap while every worker
  of the prover is busy (the overlay probe needs a worker).
- *Soft and hard limits per session (stage D, 2026-11-24):* the guard cancels the session's
  candidates, else its running commands, when the heap in use (`size_heap -
  size_heap_free_last_GC`; `size_heap` alone is the reserved heap, which does not shrink) exceeds
  75% of `--maxheap` (16384); a systemd scope per prover (`process_policy`, `MemoryMax=24G`)
  is the hard limit, whose kill the existing death check reports as `prover_died`. External
  provers of sledgehammer are started by the gateway JVM (`bash_process`), not by the prover, so
  they are not in the session's scope; the sledgehammer slots bound their total.
- *Facts, not advice (D4):* every 503 body carries an `error` key with the structured facts,
  every response an `X-Memory-State` tier (cached, no I/O), `GET /api/v1/memory` the full picture;
  results carry `error_kind` (`memory` from the ML out-of-memory text, `prover_died` from the
  request-path death check — a dead poly otherwise answers a step with stale output). The MCP
  servers word the advice (§2.10).
- *Leased sessions are never evicted (D3):* the server reclaims idle *unleased* sessions
  (largest first when sized) and reminds clients; the MCPs let their own go (scratch TTL,
  `release_idle`).
- *Budget admission:* a create or build is admitted only if used + reserved in-flight work + a
  static per-field estimate stays under the threshold; the reservation is taken at the admission
  decision (atomic on the event loop) — the live runs showed concurrent creates all passing a
  gate that reserved later. Builds no longer bypass the gate; native hosts fall back to
  `/proc/meminfo` (the gate used to read 0 there); `external_pressure` names memory held outside
  the container (host short AND others over the container's promised headroom).

### 1.7 Heavy-operation gating: server-wide sledgehammer semaphore

**Chosen:** a global semaphore (`MAX_CONCURRENT_SLEDGEHAMMER`) queues sledgehammer requests.
**Alternatives:** unbounded (Bug 6: W=16 concurrent sledgehammers OOM-killed the gateway);
per-session limits (useless — the burst comes from *many* sessions at once).

**Why:** sledgehammer is itself a multi-prover parallel job; the measured throughput knee was
~4 on 32 cores, so queueing beyond that costs nothing and prevents the one workload class
that reliably killed the JVM. Backpressure (waiting) beats failing.

### 1.8 Execution granularities: small-step, `verify_chunk`, big-step — all three, with
`verify_chunk` as the centerpiece

**Old:** small-step only (one command per round trip, subgoals after each) — the natural REPL
inheritance.
**Chosen:** keep small-step (checkpoints/rollback/RL uses), add **`verify_chunk`** (a whole
chunk as ONE PIDE edit under a single wall budget, returning per-command
`ok/failed/running/unprocessed` in source order, a partial report naming the `stuck_line` on
budget expiry, and automatic rollback of failed chunks), and keep **big-step**
(`isabelle build` on a temp session) as the strict ground truth.

- *Small-step-only cons:* for an LLM agent, one command per round trip explodes rounds and
  tokens; per-command timeouts surface as opaque failures.
- *Whole-file-only (big-step) cons:* no incremental state, no "which line is looping",
  minutes per iteration.
- *verify_chunk cons:* `success=True` means "no command errored", NOT "theorem proved" — a
  subtlety that must be (and is, aggressively) documented, because agents will happily
  self-deceive. Proved requires `success ∧ ¬proof_open ∧ ¬used_sorry`.

**Why:** the chunk is the natural unit of LLM proof output. One edit checks it with
intra-chunk parallelism, and the per-command report converts "timeout" from a dead end into
actionable feedback (*this* line loops). Big-step stays because a fresh `isabelle build` is
the only verdict immune to warm-session artifacts — the comparison arbiter runs on it, and
the I/Q "attempt 10" incident (in-session check accepted a metis call that fails cold)
proved that independence matters. **Abandoned:** the RL-era vector environment
(`vector_step`/`vectorise`) still exists in the backend but is unused by the server/MCP path.

### 1.9 Event-loop discipline: every blocking call off the loop

**Old (implicit):** router handlers called session methods via `asyncio.to_thread`, but
`_create_session` still did gateway spawn and Py4J calls inline; sledgehammer/metadata paths
similarly leaked blocking work onto the loop.
**Chosen:** all Py4J traffic and process spawning runs in worker threads; the event loop only
schedules.

**Why:** observed failure — a busy JVM turned one inline Py4J call into a frozen server
(every endpoint, including `/healthz`, unresponsive). In an asyncio server there is exactly
one rule: the loop never waits on the prover. The alternative (multi-worker uvicorn) doesn't
fit because the SessionManager is deliberately a single-process singleton owning one gateway.
**Gap closed 2026-09-30 (audit SRV-1, ISSUES.md Bug 16):** the idle/pressure cleanup
coroutine used to call the synchronous `close_session`, the settle-sleeping memory relief and
the gateway probe/recovery inline; `/readyz` and `/` ran the (5 s-cached) probe inline too.
All of them now go through `asyncio.to_thread` (`SessionManager.cleanup_once`), and
`tests/test_cleanup_offloop.py` measures loop gaps during a sweep so the rule cannot regress
silently. `/metrics` never needed it: the instrumentator's endpoint is a sync `def`, which
FastAPI runs in the threadpool.

### 1.10 Diagnostics as transient probes with a syntactic guard

**Chosen:** `POST /diagnostic` runs read-only Isar queries (`thm`, `find_theorems`,
`print_*`, …) as *transient* PIDE edits — output captured, edit discarded, rollback chain and
history untouched — gated by an allowlist (leading keyword) + denylist (`ML`, `setup`,
file-IO anywhere) in `diagnostic_guard.py`, plus (since Bug 12) the ML-execution guard of
`input_guards.py` that every text endpoint shares.
**Alternatives:** let clients run queries through the normal step path (pollutes the script
and rollback chain; earlier sledgehammer probes literally leaked `ML_val` into saved proofs);
or allow arbitrary commands (a prover command like `ML` is remote code execution).

**Why:** agents need lookups constantly, and lookups must be free of side effects — both on
the proof state and on the host. The conservative guard knowingly rejects a few legitimate
queries (documented trade-off) because the denied class includes code execution.
**Scope note (2026-09-30):** the transient-EDIT mechanism now applies ONLY to `diagnostic`
(`probe_transient` — an arbitrary Isar command cannot run any other way). The state queries
(subgoals, in-proof, facts, sledgehammer, proof state) no longer edit the document at all —
they are overlay queries, see 1.14.

### 1.11 Per-session PIDE parallelism: modest fixed defaults, env-tunable

**Chosen:** `parallel_proofs=2`, ML `threads` capped at 4 by default (not 0 = auto),
overridable per deployment (`ISABELLE_PARALLEL_PROOFS`, `ISABELLE_SESSION_THREADS`).
**Alternative:** Isabelle's auto settings (each session grabs all cores).

**Why:** with M concurrent sessions, auto-threads oversubscribes cores M-fold and spikes peak
heap (a Bug 6 contributor). The cap trades single-session latency for pool stability. For
single-agent workloads (the MCP comparison), the right tuning inverts: one session, all
cores (`threads=8`) — which is why it's config, not a constant.
**Correction (2026-11-24, replaces the audit REPL-8 note that called it a no-op):**
`ISABELLE_SESSION_THREADS` is effective: the session option `threads` sizes the prover's worker
farm (the Future scheduler that runs commands, forked proofs and print functions). Measured with
8 overlay queries (print functions of pri 0) on one session: with `threads=4` they ran 4 at a time
(8 x 5 s took 10.2 s), with `threads=10` all 8 at once (7.2 s). What it does not cap is the poly
process's OS thread count (GC and other runtime threads). It is also the number of
`isabelle_multi_attempt` candidates that run at once in the file session.

### 1.12 Container is the unit of deployment — and must be native-arch

**Chosen:** everything runs in one container (repo volume-mounted for dev, heaps in a named
volume); the image builds for the host architecture, selecting the matching Isabelle tarball
(x86-64 vs ARM) with a mirror-fallback download chain.
**Old:** `platform: linux/amd64` pinned — which silently ran the entire prover stack under
qemu on Apple Silicon at a 5–20× penalty (session creation went from ~1 min to many minutes;
one heap build measured 15+ min emulated vs 2m56s native).

**Why:** Isabelle's path assumptions and component registration make "works on my machine"
expensive; one blessed layout keeps it reproducible. The arch lesson is recorded because it
was invisible: everything *worked*, just absurdly slowly.

### 1.13 Imports: static verified heaps in a shared heap pool — not live file sync

**Chosen (2026-08, for the LSP-like read-only mode):** imported theories are **static** for
a session's lifetime and are served from **verified session heaps** — each project is built
once with `isabelle build -b` (build passing IS the verification gate), kept in a
server-side heap pool, and shared by every session working on that project. Agents write
qualified heap imports (`imports "HeapTest.Bar"`); the wrapper theory states them (the
header-overwrite behavior in `edit_utils.scala` is load-bearing: a document's own import
list only gates loading — the visible context comes from the wrapper). Stale sources →
explicit rebuild, never silent live re-sync.
**Alternative researched and rejected: live draft-node syncing** (editor-style: watch/pull
file changes, `use_theories` reloads, dependencies auto-open — Isabelle-MCP's model).
A second alternative, the **overlay/query-operation query layer** (Pure's
`Query_Operation`, no vscode fork needed), was also fully researched and validated as
feasible — then deferred, not rejected.

- *Live-sync pros:* imports editable mid-session; matches how human editors work.
- *Live-sync cons:* it is an IDE feature, and our workload is agents — dependency sets are
  fixed per attempt, the agent edits the entry file, and mid-run import changes essentially
  don't happen. It also needs the full style-4 stack (real-path nodes, perspective-driven
  evaluation, live dependency manager, document-bookkeeping rework) — weeks, for a
  capability agents won't use. Spike evidence: file-backed nodes accept content reloads but
  not interactive probe edits (without perspective work), and the probe ML environment
  cannot reach them (ML envs inherit only through imports — hard Isabelle semantics).
- *Heap-pool pros:* build = verification (an unbuildable import can never be served);
  startup ~4 s vs tens of seconds re-checking sources (this is also the fix for the slow
  HOL-Computational_Algebra-scale wrapper loads); heap pages shared read-only across
  sessions via mmap/COW; imports are Milan's reuse boundary made explicit (IsabelleGym 1.0
  already treated "import a finished theory" as the freeze point for sharing results).
- *Heap-pool cons:* import edits require an explicit rebuild (deliberate — that IS the
  gate); agents must write qualified names (a header-normalization shim is a deferred
  option); disk footprint of the pool needs watching (PISA's ~35 GB/instance duplication
  is the cautionary tale — our pool builds once per project, not per instance).

**Why:** the target is agent workflows (training/eval + MCP), not interactive editing. The
heap model gives verified, fast, shareable imports with one small Scala change (`dirs`
plumbing), while live sync buys nothing agents use and costs the largest reconstruction on
the table. **Given up (deliberately):** live import editing without rebuild. The overlay query layer
was deferred here and then ADOPTED on 2026-09-30 as the mechanism behind every state query
(1.14); what remains unadopted is only its "any node" generality — queries still target the
session's own document. Full
evidence was in the 2026-08 research notes (`research-use-theories-spike`,
`research-heap-pool`, `research-style4-feasibility`, `research-lsp-readonly-mode/
IMPORT_SYNC_PLAN.md`) — NOTE (2026-09-30 review): those folders are no longer under this
tree's internal working notes; the surviving record is this section and ISSUES.md's work log. The
overlay path's sledgehammer parity became `sledgehammer_at`, and since 2026-09-30 the same
machinery backs every state query (1.14); the generic one-registration diagnostic
dispatcher proven in that spike remains a possible upgrade for `diagnostic`.

### 1.14 State queries as PIDE overlay queries — not probe edits (2026-09-30)

**Old:** every read-only state question (open subgoals, in-proof, local/global facts,
sledgehammer, rendered proof state) was an `ML_val ‹Repl.send_*_tagged …›` command appended
at the document tip; the ML side pushed the answer back through a per-backend
`Scala.Fun_Strings` channel queue tagged with the backend's `channel_id`, the Scala side
blocked on that queue, waited for the probe to consolidate ("settle"), then removed the
edit (`discard_last_edit`). Finished theories needed a second path that inserted the probe
*before* the trailing `end`.
**Chosen:** every state query is a PIDE **overlay query**: a `Query_Operation` registered in
`REPL.ML` (`isabelle_pool_server_goals`, `_in_proof`, `_local_facts`, `_global_facts`, `_state`,
`_sledgehammer`) is attached as a temporary overlay to the document's **last command**
(`Document_Utils.current_state_host`; the command before `end` for the fact queries on a
finished theory), runs against that command's result state — which IS the current toplevel
state — and each `writeln_result` becomes an instance-tagged entry in the host's
`command_results` that `Overlay_Queries.overlay_query` collects under a per-query budget and
then removes the overlay. Nothing is inserted. The LSP-like `sledgehammer_at` (host = the
command at a line) and the stepwise `sledgehammer` (host = last command) are now one
operation. `probe_transient` (`diagnostic`, 1.10) stays insertion-based because it runs an
arbitrary Isar command.
**Alternative kept in mind:** patch the insert/discard bracket (`with_probe_settle`) so a
retried probe is discarded first — the minimal fix for the leak (audit REPL-1).

- *Probe-edit pros:* no PIDE-internal API beyond text edits; the ML function sees the
  toplevel state directly. *cons:* a read-only question implemented as a mutation that must
  be undone — every bug in this area was a restoration bug: a probe whose channel reply timed
  out stayed in the script and the rollback chain forever (REPL-1; `_MLVAL_RE` in
  `build_verify.py` existed to scrub such residue out of bigstep text; an earlier incident
  needed two `rollback` calls to undo one line); the ML→Scala channel plumbing (~200 lines of
  queues, `CH:` tags, `Scala_Functions` service) was shared JVM state that one ML compile
  error broke for every session at once; the settle barrier was an unbounded wait (REPL-2);
  finished theories needed a special insertion path.
- *Overlay pros:* zero document mutation — the leak class cannot occur; results come back
  through PIDE's own `command_results` with a PIDE-generated instance id (no queues, no
  routing, no shared mutable state between backends); the wait is bounded by construction
  and removing the overlay IS the cancellation (PIDE drops the print task); one host
  selection rule replaces the finished-theory special case; the output is XML with markup
  intact, which is what structured feedback (audit REPL-7) will need. Net −300 lines.
- *Overlay cons:* an overlay's print task runs only after its host command has finished
  evaluating, so a query issued while a long command runs returns "timed out" at its budget
  instead of an answer (the same visible outcome as the old channel timeout, now bounded); the
  ML function's return path is `writeln_result` per line rather than a typed list.

**Why:** the migration (owner decision, Option B: migrate directly rather than patch first)
removes the whole class of restoration bugs instead of fixing one instance, deletes more
code than it adds, and unifies the two MCPs on one mechanism that had already been validated
in production by `sledgehammer_at`. Verified live on the Isabelle2026-RC2 image
(internal working notes): all probes in proof / after `qed` / after
`end`, `verify_chunk`'s internal probes, no `ML_val` in the source at any point, and a query
against a still-running 45 s command failing cleanly at its 20 s budget with no residue.
**Given up:** the forked, non-blocking ML sledgehammer thread (the overlay's print task is
the async unit now) and the `ISABELLE_REPL_*_TIMEOUT` knobs' old meaning as channel poll
timeouts — same names, now overlay wall budgets.

### 1.15 Every prover wait is wall-bounded; a timed-out small step rolls back (2026-09-30)

**Old:** one helper, `stable_node_snapshot`, waited until *every* command in the node was
consolidated, with no deadline, and almost every read went through it — including the
purely syntactic ones (where does the header end, is the last command `end`, which command
contains line N). `verify_chunk` had its own budgeted loop; nothing else did. A looping
`metis` sent through `POST /commands` therefore blocked the session's single worker thread
for as long as the loop ran: the Python future timed out, the JVM never returned, and every
later request queued behind it (audit REPL-2, ISSUES.md Bug 15).
**Chosen:** two snapshot primitives with different contracts — `node_snapshot` waits only
for a *stable* session (no pending edits) and is used by every syntactic or
read-what-is-there query; `settled_node_snapshot(budget_ms)` waits for consolidation under a
deadline and reports whether it settled, and is used only where a command's *results* are
printed. The budget is the request's own timeout, threaded from Python into `step` and
`diagnostic` (the Python future timeout sits a grace period above it, so the backend's
answer always arrives first). Waits with no request behind them (`rollback`, `vector_step`)
use one env default. **On expiry, `step` discards the command it inserted** — the same rule
`verify_chunk` has always applied — and returns `success=false` with an error naming the
timeout.
**Alternative (Option B, rejected by the owner):** keep the timed-out command running and
report "still running"; the worker is freed, but PIDE evaluates strictly in document order,
so every later command would queue behind the runaway inside the prover and the client would
have to `rollback` by hand before doing anything useful.

- *Roll-back pros:* the session is immediately usable, the retry story is one line ("retry
  with a larger timeout"), and it matches the chunk path agents already know. *cons:* a slow
  but eventually-successful command no longer completes in the background — the small-step
  contract changed, and clients that relied on "timeout means still running" must adapt.
- *Read-only queries never waiting:* `goals_at_line`, hover, definition and the header /
  `end` checks now return what exists at that moment (jEdit semantics); the LSP `sync` is the
  barrier, which is what the file-sync contract already said.

**Why:** removing the edit is the only cancellation primitive PIDE gives us (REPL-3 is still
open), so "bounded wait + remove on expiry" is the strongest guarantee available without new
machinery, and it turns the worst failure mode (a wedged worker until the lease reaper) into
an ordinary error result. **Given up:** background completion of timed-out small steps.
`sync_document` deliberately keeps its "leave the replace in place on timeout" semantics
(there is no single insert to remove), but nothing blocks behind it any more.

---

## 2. MCP server — agent-facing design

### 2.1 Thin layer over the HTTP client — no prover logic in the MCP process

**Chosen:** `mcp_servers/` — the chunk-centric `stepwise/` server this part describes and the
LSP-like `lsp/` server of 2.9, over shared `common/` helpers — wraps `PoolAsyncClient`
(plain HTTP). No Py4J, no Isabelle,
no core edits.
**Alternative:** embed the gym in the MCP process (one fewer hop), or fork a special agent
server.

**Why:** the server already solved pooling, leases, memory, and recovery — duplicating any of
it in a second process would fork the truth. The MCP process becomes stateless-ish and
restartable at will; the extra HTTP hop is noise next to prover latencies. *Con accepted:*
MCP capabilities are capped by the HTTP API — which turned into a feature: every capability
an agent needed (diagnostic, verify_chunk) had to be added at the API level, where every
other client also benefits.

### 2.2 Sessions and leases are invisible to the agent

**Chosen:** the agent calls `enter_theory(name, imports)`; the pool acquires a leased gym
session behind the scenes, keyed to the MCP connection. `close_theory` (or a new
`enter_theory`) disposes it. The agent never sees `session_id` or lease tokens.
**Alternative:** expose `create_session`/`acquire`/`release` as tools (some Isabelle MCPs
expose their session machinery).

- *Exposed pros:* an agent could in principle juggle several sessions.
- *Exposed cons:* every round trip burns tokens on plumbing; models mismanage handles
  (the I/Q token-mutation incident is the canonical example of making a model carry
  credentials/handles it doesn't need); leaked leases block the pool for hours.

**Why:** the agent's vocabulary should be proof concepts, not infrastructure. The one real
capability lost (multi-session use) was reintroduced deliberately and safely as
`verify_batch` (2.6). **Refinements from incidents:** `_begin_theory` closes the just-acquired
session if `enter_theory` fails (else the lease leaks for `MAX_LEASE_AGE`), and acquisition
uses `reuse_dirty=False` so an agent can never inherit another attempt's document (the
confirmed proof-leak instance).

### 2.3 Connection identity: weak-keyed by the MCP session object (the "stdio key" choice)

**Chosen:** per-connection state lives in a `WeakKeyDictionary` keyed by the MCP
`ctx.session` *object*; stdio (or any transport without a session object) falls back to a
single pool-owned sentinel object (`_StdioKey`).
**Old:** a plain dict keyed by the string `f"conn-{id(ctx.session)}"`.

- *Old cons:* entries were never removed (dropped connections leaked state forever), and
  `id()` is a memory address — after GC, a **new** connection's session object can reuse the
  address and silently inherit the old connection's half-finished proof session.
- *New pros:* identity is bound to the object itself (recycling impossible by construction),
  and weak keys make cleanup automatic — when the transport drops the connection, GC removes
  the entry with no disconnect hook.
- *New cons:* no eager close callback for the underlying gym session; it waits for the
  server's abandoned-lease reaper. (A `weakref.finalize` + async close was judged more
  machinery than the residual risk warranted.)

**Why the sentinel:** weak dict keys must be weak-referenceable (a `str` is not), and a
stdio server has exactly one logical connection for its lifetime — so one strongly-held
sentinel object is both necessary and semantically correct.

### 2.4 `verify_chunk` is the ONLY execution tool

**Chosen:** one tool submits Isar text — one command or a whole proof — and returns the
per-command status report. No `step` tool.
**Alternative:** mirror the REPL (step/undo per command), as several Lean/Isabelle MCPs do.

- *Step-tool cons:* rounds ≈ commands; token cost explodes; the agent must orchestrate its
  own batching anyway.
- *verify_chunk pros:* matches how models naturally emit proofs (blocks); per-line feedback
  (`failed` with messages, `running` with `stuck_line`) tells the agent *where* to intervene
  — the design goal distilled from the Lean ecosystem survey (`lean-lsp-mcp`'s
  multi-attempt/diagnostics tools) in the internal MCP research notes.
- *Renderer choice:* terse summary by default, `detail=True` for the full table — token
  economy with an escape hatch. Failure messages are truncated; `proof_open`/`used_sorry`
  warnings are spelled out in the OUTPUT (not only in docs) because agents act on what they
  see this turn, not what the schema said.

**Why:** granularity is the token budget. One tool with rich output beats five chatty tools.
**Abandoned:** exposing `step`, `checkpoint`-centric workflows as the primary loop
(checkpoint/restore/rollback remain available but secondary), and exposing raw subgoal
objects (state is fetched on demand via `proof_state`).

### 2.5 `diagnostic` as a separate read-only tool

**Chosen:** a dedicated tool for `thm`/`term`/`find_theorems`/`print_*` that returns output
the execution path would discard, running transiently under the server-side guard (1.10).
**Alternative:** let agents put queries inside `verify_chunk` chunks.

**Why:** queries inside chunks pollute the document (and their output is dropped by the
status report), so agents either lost information or corrupted their scripts. Separating
"look" from "touch" gives the agent a safe reflex — and the guard means "look" can never
execute code. The tool description enumerates concrete examples because models use tools
they can pattern-match.

### 2.6 `verify_batch`: bounded inter-session parallelism for a sequential agent

**Chosen:** one tool fans out N independent chunks concurrently, each in its own throwaway
leased session, bounded by a semaphore (`ISABELLE_MCP_MAX_PARALLEL`), returning per-item
summaries.
**Alternatives:** teach agents to hold multiple sessions (rejected in 2.2); or nothing.

**Why:** an MCP agent is a sequential loop, but the server pool is parallel — this tool is
the bridge, letting one round test several lemma variants at once without the agent managing
sessions. The bound respects the memory admission gate; without it, one enthusiastic call
could flood the pool.

### 2.7 Transport: stdio and streamable-HTTP from the same process

**Chosen:** both, switched by env (`ISABELLE_MCP_TRANSPORT`); stdio for local single-agent
use (Claude Desktop/Code, the comparison harness spawns it per attempt), HTTP for remote or
multi-agent setups.
**Why:** stdio is zero-config and process-per-connection (isolation for free); HTTP needs
the per-connection keying of 2.3 to keep agents isolated — which is exactly why that keying
exists. Fresh-process-per-attempt (harness style) also gives clean-slate guarantees that a
long-lived server cannot.

### 2.8 The tool docstrings are the system prompt

**Chosen:** the semantics agents must not get wrong — `success=True ≠ proved`, the
proved-only-when triple, "never sorry/oops to pass", auto-rollback behavior, restart
etiquette — are written into the tool descriptions themselves (and repeated in outputs at the
moment they matter). The bundled `prove_theorem` MCP prompt is minimal and optional.
**Alternative:** rely on each client's system prompt to explain the tools.

**Why:** tool schemas are the only channel guaranteed to reach *every* MCP client unchanged;
harness prompts vary per experiment (four variants exist), but the invariants must not.
The comparison runs validated this: the failure modes that survived all prompt variants were
exactly the ones later fixed in tools/output text, not in prompts. *Con accepted:* longer
schemas cost tokens on every round — mitigated by keeping per-call outputs terse (2.4).
*Mechanics (bitten twice, 2026-09-30 and 2026-10-17):* only a string LITERAL as the first
statement is a docstring — `"""text""" + CONST` is an expression and FastMCP ships the tool with
an empty description; append notes inside the literal (or set `__doc__` in a decorator, as
`synced_tool` does). `tests/test_mcp_tools_smoke.py` guards this in-container.

### 2.9 The LSP-like file-sync MCP: a second server for file-editing agents (2026-08 →)

**Chosen:** a second MCP server, `mcp_servers/lsp/`, for harnesses whose agent edits a `.thy`
FILE (isabelle-humanize style) rather than emitting chunks: `isabelle_open(path)` binds the
file to a leased session (one `FileBinding` per canonical path), `isabelle_sync` pushes the
file's current text through `PUT /document` (incremental spliff-diffed replace since Phase
B1 — a tip edit re-processes only the changed tail; NOTE the diff→edit conversion silently
corrupted the node whenever an insert sat next to a deletion until 2026-09-30, ISSUES.md
Bug 18 — spliff's ops are simultaneous base-coordinate ops, PIDE edits are sequential, and
the shared `Edit_Utils.diff_edits` now does that mapping in one place), and the read-only tools are
position-explicit: `isabelle_goal` / `isabelle_command_at_line` (snapshot-based), `hover_info`
/ `definition` (Rendering), `isabelle_sledgehammer` (overlay, 1.14), `diagnostic_messages` /
`last_report` (the stored per-command report), plus facts, source, checkpoints, `run_code`
(gated, 1.10), heap build/status (1.13). `isabelle_multi_attempt` tries N candidate
proofs concurrently in a bounded scratch pool of extra sessions (`SCRATCH_POOL_SIZE`). Env
prefix `ISABELLE_MCP_LSP_` so both MCPs run side by side (the pre-merge `mcp_lsp_server` /
`mcp_stepwise_server` launch shims were retired on 2026-10-05).
**Alternative:** grow the chunk-centric server (2.4) with file tools.

**Why:** the two agent shapes want opposite contracts — chunk agents want transactional
`verify_chunk` (failed text rolls back), file agents want the broken state KEPT so they can
inspect and fix it (the dual, `step_chunk_report`) — and one tool surface serving both
confused models. Splitting keeps each docstring set honest (2.8). Both share the HTTP client
and the pool/lease design (2.2, 2.3); prover logic stays in the server (2.1).
**Audit gaps, all four closed 2026-09-30 (ISSUES.md Bugs 17, 19, 20, 21).**
**(Bug 21, MCP-4):** file bindings acquire with `reuse_dirty=False`, the same clean-only rule
as 2.2 — a released binding's session holds the previous file's document and cost a full
reset on reuse anyway. **(Bug 17, MCP-1):** scratch slots are now taken and
returned through one `scratch_session` bracket (released, or dropped on 404 / cancellation,
always in `finally`), the wait for a slot is bounded, and a dropped session that the server
still reports busy is closed by a deferred retry — one client disconnect can no longer wedge
`multi_attempt` for a whole run. **(Bug 19, MCP-2):** a binding is only
created for a path that exists, so a typo no longer leases a session. **(Bug 20, MCP-3):** binding creation is single-flight
per path, so concurrent first calls share one session.

---


### 2.10 Memory protocol: the server reports facts, the MCP words the advice (2026-10-17)

**Chosen:** the server never words advice and never evicts a leased session (owner decisions
D3/D4): it publishes structured facts (503 `error` keys, `X-Memory-State`, `/api/v1/memory`,
`error_kind`). Each MCP server keeps a **registration list** of the sessions it holds (kind, what
for, idle time, server expiry, attributed memory; session id and lease stay private, the agent
sees a display-only `ref` that no tool accepts), wraps every tool with a guard that turns an HTTP
error into `{"error": "memory_pressure", "your_sessions", "others", "advice"}` and attaches a
`memory_reminder` while the tier is warning/critical, and offers `list_my_sessions` /
`memory_status` / `release_idle` tools. Advice rules, in order: release what you hold but do not
use (scratch always, bindings/batch items idle for `ADVICE_IDLE_S`; never the stepwise theory —
idle there means the model was thinking), retry after `retry_after_s` when the server can reclaim
others' idle sessions, and — on repeated failure, running builds, other task groups holding the
memory, or external pressure — stop and tell the user: the agent cannot free that memory.
Advice is computed from the registry at the moment of the refusal: when sibling creates of one
fan-out are still in flight, the first refused item may see nothing releasable yet and get the
retry line instead of rule 1 — correct at that instant, and the next refusal names the scratch.
**Alternative:** advice text in the server's 503 (one source of truth for every client).
**Why:** the server is hidden from the agent; only the MCP knows its own tool names and what the
agent is doing with each session. The LSP server also frees its own warm scratch sessions first
(by tier) before asking the agent for anything. Verified live (batch 4, lock-protected run 12 on a
second server instance with a test threshold): three concurrent candidates reserve their estimates
at the admission decision, so one is refused with rule-1 advice; `isabelle_release_idle` closes the
warm scratch sessions and the retried single candidate is admitted — the agent's loop closes.
That the release is *necessary* is shown by the unit test on budget-driven relief, not by the live
run (the live threshold is a lever); the gateway JVMs' own memory is in the cgroup figure but
attributed to no session (open item).

## 3. How the server works (report v2, Appendix E; moved here 2026-10-27)

The technical report keeps the client's view (its Section 4) and points here for the mechanism.

**Tiers.** A Scala backend, packaged as an Isabelle component, creates headless sessions through
`Headless.Session`, the API behind `isabelle server`, hosted in its own process; one backend object per
session owns a draft theory node and the edit and rollback bookkeeping. All backends live in one gateway
JVM, reached from Python over a bridge whose calls are serialised per session, since neither the bridge
nor the backend is thread-safe. A FastAPI server owns the pool and the HTTP API, keeps its event loop free
of prover waits by running backend calls on worker threads, and exposes metrics and probes. The Python
client and the two MCP servers sit above it. One reflective call remains, to the private constructor of
`Server.Context`, so that the backend can invoke the server's own command handlers; packaging the backend
as an Isabelle tool (a roadmap item) removes it.

**The owned document.** Each session's document is a string the backend appends to. Every insert is
recorded in a parent-linked chain of status snapshots, so rollback removes the last insert, a checkpoint
is a snapshot, and restore replays the difference. The theory header is parsed from the accumulated text
and overwritten by a wrapper import so that the session's imports supply the visible context.
Whole-document replacement, used by the file MCP server, is diffed into sequential edits. Because PIDE
spans for a string node carry no file positions, diagnostics are located by the owning command's range.

**Verification.** The one-command call inserts a command, waits under a budget for its status, and
removes it on expiry. The chunk call inserts the block as one edit, waits for every command to settle or
the budget to expire, classifies each command, and removes the block if any command failed or the budget
expired. The whole-file check writes the theory to a temporary session directory, runs `isabelle build`
with a content-addressed cache, and returns the result.

**State queries.** Goals, in-proof status, local and global facts, rendered state and Sledgehammer are
query operations registered in Isabelle/ML and invoked from Scala as temporary overlays on the document's
last command (Wenzel 2014, PIDE); the backend polls the command's results for a finished marker under a
budget and then removes the overlay. Only the diagnostic endpoint inserts a transient command. PIDE MCP is
a headless PIDE front-end in the same sense (Wenzel 2019); the two differ in what the document is, not in
how PIDE is driven.

**Pool, leases, admission.** Sessions are keyed by sorted imports, base heap and heap fingerprint. A lease
is a token presented on every call; expired and abandoned leases are reaped on a timer, as are idle
sessions. Admission reads the server's own cgroup memory accounting, since the JVM cannot see the prover
processes' memory, estimates the next session's size by heap, and evicts least-recently-used idle sessions
before refusing; a refusal carries the facts (used, limit, available, what is leased) so the client can
decide what to do. A prover death is detected on the next call, reported as such, and the session
discarded; a gateway crash restarts the gateway. Project heaps are built with `isabelle build -b` and
recorded with a manifest of source fingerprints; a mismatch returns a stale-heap error.

**Where to look.** Session start in `server/repl/src/main/scala/repl/session_manager.scala`; document
edits in `repl_session.scala` and `edit_utils.scala`; overlay queries in `document_utils.scala` and
`src/ml/REPL.ML`; the per-command report in `document_utils.scala`; the HTTP layer in `server/app`; the
pool and memory monitor in `server/app/services`; the guards in `server/app/core`; the MCP servers in
`mcp_servers/stepwise` and `mcp_servers/lsp`; the experiments in `evaluation/memory_scaling` and
`evaluation/training_metrics`.

## Cross-cutting lesson

Nearly every choice above was refined by an incident rather than foresight: exit-through-queue
(Bug 1/8), crash recovery (Bug 6), cgroup accounting (Bug 5/6), clean-only reuse (the proof
leak), weak connection keys (the id-recycling hazard), fresh-build arbitration (attempt 10),
native-arch containers (the qemu discovery). The meta-choice that made those refinements
cheap: **small, typed, observable layers** — every layer (HTTP API, pool, backend thread,
gateway, ML) can be probed and swapped without rewriting its neighbors, and every incident
above was diagnosed from logs/metrics those layers already emitted.
