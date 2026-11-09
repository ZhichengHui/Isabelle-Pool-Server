package repl

import scala.jdk.CollectionConverters._

import isabelle._

/** Transient read-only probes of [[ReplBackend]]: open subgoals, in-proof
 *  predicate, local/global facts, sledgehammer, rendered proof state, source
 *  text, and the generic `probe_transient` diagnostic primitive. These serve
 *  BOTH MCP servers — the chunk-centric one (state / subgoals / facts /
 *  sledgehammer / diagnostic tools) and the LSP-like read-only one.
 *
 *  Every STATE query is an OVERLAY QUERY, not a text edit (since 2026-09-30):
 *  a Query_Operation registered in REPL.ML is attached as a temporary document
 *  overlay to the node's LAST command (`Repl_Session.current_state_host`), runs
 *  against that command's result state — which IS the current toplevel state,
 *  exactly what an appended command would see — and its instance-tagged
 *  results are read from the host's command_results
 *  (`Document_Utils.overlay_query`, the machinery `sledgehammer_at` already
 *  used). The document, rollback chain and command history are never touched,
 *  so there is nothing to discard and no probe can leak into the script. (The
 *  previous design inserted `ML_val ‹Repl.send_*_tagged …›` commands and read
 *  replies from per-backend Scala.Fun_Strings channel queues; a probe whose
 *  reply timed out stayed in the document — docs/ISSUES.md tracker REPL-1.)
 *  The wait is bounded by a per-query budget; on expiry the overlay is removed
 *  (cancelling the print task) and an error is raised — the same exception
 *  contract the channel timeouts had, so the Python side is unchanged.
 *
 *  FINISHED THEORIES (document ends with theory `end`): after `end` there is
 *  no theory context, so `open_subgoals`/`in_proof`/`sledgehammer`/
 *  `get_proof_state` short-circuit to their definitional answers (a successful
 *  `end` means no proof is open) and the fact queries host on the last command
 *  BEFORE `end` (`skip_end`), i.e. observe the state the theory closed with.
 *
 *  `probe_transient` is the one remaining INSERTION probe: it runs an
 *  arbitrary caller-supplied Isar command (`thm`, `find_theorems`, …), which
 *  no print function can do. It inserts the command, reads its output, and
 *  removes the edit again (before a trailing `end` via
 *  `Repl_Session.with_probe_before_end`, whose bracket always removes it). */
object Backend_Probes {
  /** Wall budgets for the overlay queries. Env-tunable; the Python side reads
   *  the same variables for its outer timeouts (server/app/core/config.py). */
  val SUBGOALS_BUDGET_MS: Long =
    sys.env.get("ISABELLE_REPL_SUBGOALS_TIMEOUT").flatMap(_.toLongOption).getOrElse(20L) * 1000L
  val LOCAL_FACTS_BUDGET_MS: Long =
    sys.env.get("ISABELLE_REPL_LOCAL_FACTS_TIMEOUT").flatMap(_.toLongOption).getOrElse(20L) * 1000L
  val GLOBAL_FACTS_BUDGET_MS: Long =
    sys.env.get("ISABELLE_REPL_GLOBAL_FACTS_TIMEOUT_MINUTES").flatMap(_.toLongOption).getOrElse(5L) * 60000L
  /** Grace added to the Isabelle-level sledgehammer timeout (the ATP budget)
   *  before the overlay is abandoned: prover start-up and result relay. */
  val SLEDGEHAMMER_GRACE_MS: Long = 30000L
  /** Budget for the memory probe (`mem_stats`): it runs in the request path
   *  after ordinary commands, so a distressed prover must cost at most this
   *  much extra, not the 20 s subgoal budget. */
  val MEM_BUDGET_MS: Long =
    sys.env.get("ISABELLE_REPL_MEM_TIMEOUT").flatMap(_.toLongOption).getOrElse(5L) * 1000L
}

trait Backend_Probes { this: ReplBackend =>
  import Backend_Probes._

  /** Run `print_fn` (a REPL.ML Query_Operation name + "_query") as an overlay
   *  on the current-state host and return its result lines in order.
   *  `skip_end`: host on the command before a trailing theory `end`. Raises
   *  `error` on budget expiry or when the ML side reported an error (callers
   *  keep the channel-era exception contract); [] when the node has no command
   *  yet — nothing to observe. */
  private def state_query(
      print_fn: String,
      args: List[String],
      budget_ms: Long,
      skip_end: Boolean
  ): List[String] =
    repl_session.current_state_host(skip_end) match {
      case None => Nil
      case Some(host) =>
        val (done, content, errors) = repl_session.overlay_query(host, print_fn, args, budget_ms)
        if (!done) error(s"Timeout waiting for $print_fn result (budget ${budget_ms} ms)")
        else if (errors.nonEmpty) error(s"$print_fn failed: ${errors.head}")
        else content
    }

  /** The ML process pid seen by the memory probe, kept once known so later
   *  answers (and answers given while the overlay cannot run) still carry it.
   *  Forgotten by `reset` (Backend_Lifecycle): the reset stops that process, and
   *  a stale pid handed out while the new theory is not begun (e.g. its header
   *  failed) was read by the server as "prover died" and lost the session. */
  @volatile private var cached_ml_pid: Option[String] = None

  private[repl] def forget_ml_pid(): Unit = cached_ml_pid = None

  /** Memory facts of this session's ML process (REPL.ML `Repl.mem_stats`):
   *  `pid` (from /proc/self; absent on non-Linux hosts) and the Poly/ML
   *  runtime statistics (`size_heap`, `size_heap_free_last_GC`, ...), as a
   *  key -> value map. Empty before the first theory command — the overlay
   *  needs a host command — except for a previously seen pid. Hosted before a
   *  trailing `end` like the fact queries. Raises on budget expiry
   *  (`MEM_BUDGET_MS`), which the Python side counts as a failed sample. */
  def mem_stats(): java.util.Map[String, String] = {
    val lines =
      if (!repl_session.current_thy_begun) List()
      else
        try state_query("isabelle_pool_server_mem_query", Nil, MEM_BUDGET_MS,
              skip_end = repl_session.current_thy_ended)
        catch { case ERROR(msg) => List("error=" + msg) }  // never a traceback per interval
    val facts = new java.util.LinkedHashMap[String, String]()
    for (line <- lines) {
      val i = line.indexOf('=')
      if (i > 0) facts.put(line.substring(0, i), line.substring(i + 1))
    }
    val seen_pid: String | Null = facts.get("pid")
    if (seen_pid != null) cached_ml_pid = Some(seen_pid)
    else cached_ml_pid.foreach(pid => facts.put("pid", pid))
    facts
  }

  /** Isabelle session phase (`Ready`, `Terminated(...)`, ...); see
   *  Repl_Session.phase_string. */
  def session_phase(): String = repl_session.phase_string

  /** Imports (newline-separated) a session acquired with `session_theories`
   *  (newline-separated) does not cover; newline-separated, empty when all are
   *  covered or cannot be judged. See Repl_Session.uncovered_imports. */
  def uncovered_imports(session_theories: String, requested: String): String = {
    def lines(s: String): List[String] = Library.split_lines(s).map(_.trim.nn).filter(_.nonEmpty)
    repl_session.uncovered_imports(lines(session_theories), lines(requested)).mkString("\n")
  }

  /** Pretty-printed open subgoals of the current proof state ([] when not in a
   *  proof — including after a trailing theory `end`, which closes the theory
   *  with no proof open by definition). Consumed via GET .../subgoals by both MCPs. */
  def open_subgoals(): java.util.List[String] = {
    val subgoals =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) List()  // post-`end`: no proof can be open
      else state_query("isabelle_pool_server_goals_query", Nil, SUBGOALS_BUDGET_MS, skip_end = false)
    subgoals.asJava
  }

  /** True while the toplevel is inside a proof block (the ML `Toplevel.is_proof`
   *  predicate: true for the whole block lifetime, including after a terminal
   *  `show` while `qed` is still pending). Definitionally false after a trailing
   *  theory `end` (a successful `end` means no proof is open). */
  def in_proof(): Boolean =
    if (!repl_session.current_thy_begun) false
    else if (repl_session.current_thy_ended) false  // post-`end`: no proof can be open
    else state_query("isabelle_pool_server_in_proof_query", Nil, SUBGOALS_BUDGET_MS, skip_end = false) == List("1")

  /** Facts visible in the current proof context; consumed via
   *  GET .../facts/local by both MCPs. On a finished theory the query hosts on
   *  the command BEFORE the trailing `end`; the ML side still gates on
   *  `Toplevel.is_proof`, so an empty list there is correct. */
  def local_facts(): java.util.List[String] = {
    val facts =
      if (!repl_session.current_thy_begun) List()
      else state_query("isabelle_pool_server_local_facts_query", Nil, LOCAL_FACTS_BUDGET_MS,
             skip_end = repl_session.current_thy_ended)
    facts.asJava
  }

  /** Facts of the current theory's global context, up to `limit`; consumed via
   *  GET .../facts/global by both MCPs. Same post-`end` hosting as local_facts. */
  def global_facts(limit: Int): java.util.List[String] = {
    require(limit > 0, "limit must be positive")
    val facts =
      if (!repl_session.current_thy_begun) List()
      else state_query("isabelle_pool_server_global_facts_query", List(limit.toString), GLOBAL_FACTS_BUDGET_MS,
             skip_end = repl_session.current_thy_ended)
    facts.asJava
  }

  /** Run sledgehammer on the first subgoal with an Isabelle-level budget of
   *  `timeout_s` seconds; returns the collected suggestion lines. Consumed via
   *  the sledgehammer endpoint/tool by both MCPs. Meaningless after a trailing
   *  theory `end` (no open goal) or outside a proof — returns [] immediately
   *  rather than waiting out the budget. Same query operation as the LSP-like
   *  `sledgehammer_at`, hosted on the document's last command. */
  def sledgehammer(timeout_s: Int): java.util.List[String] = {
    val suggestions =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) List()  // post-`end`: no open goal
      else
        try
          state_query("isabelle_pool_server_sledgehammer_query", List(timeout_s.toString, "1"),
            timeout_s.toLong * 1000L + SLEDGEHAMMER_GRACE_MS, skip_end = false)
        catch {
          // not inside a proof: definitional "no suggestions", not a failure
          case ERROR(msg) if msg.contains("Unknown proof context") => List()
        }
    suggestions.asJava
  }

  /** Rendered current proof state; consumed via the state endpoint (MCP
   *  `proof_state` tool) by both MCPs. Errors immediately on a finished theory
   *  instead of waiting out the budget (no proof can be open after `end`). */
  def get_proof_state(): Repl_Result = build_result {
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error(
        "Cannot retrieve proof state without beginning theory."
      )
    else if (repl_session.current_thy_ended)
      Repl_Output.add_error(
        "Cannot retrieve proof state after theory end (theory is closed)."
      )
    else
      try
        state_query("isabelle_pool_server_state_query", Nil, SUBGOALS_BUDGET_MS, skip_end = false)
          .foreach(Repl_Output.add_output)
      catch { case ERROR(msg) => Repl_Output.add_error(msg) }
  }

  /** Execute a command TRANSIENTLY: insert it, capture its writeln/state output, then
   *  discard the edit so the theory node and rollback chain are untouched. This is the
   *  one INSERTION-based probe left (an arbitrary Isar command cannot run as a print
   *  function); the state queries above are overlays.
   *
   *  Use this for ANY read-only query (diagnostic, search, etc.) where you need the
   *  command's output but do NOT want it to persist in the proof script. Keyword
   *  allowlist/denylist gatekeeping is enforced upstream on the server side.
   *  Consumed via POST .../diagnostic by both MCP servers.
   *
   *  The wait for the command's evaluation is bounded by `wall_budget_ms` (the
   *  request's timeout): on expiry the output is whatever was produced so far, an
   *  error names the timeout, and the edit is removed as always — which cancels the
   *  runaway (e.g. a `find_theorems` over a huge fact base).
   *
   *  On a FINISHED theory (trailing `end`) the command is inserted BEFORE the `end`
   *  (an appended command would never execute) and removed again afterwards, leaving
   *  the document byte-identical. */
  def probe_transient(isar_string: String, wall_budget_ms: Long): Repl_Result = build_result {
    def note_timeout(settled: Boolean): Unit =
      if (!settled)
        Repl_Output.add_error(
          s"Diagnostic timed out after ${wall_budget_ms} ms (still running) — removed; output is partial")
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error("Cannot run probe without beginning theory.")
    else if (repl_session.current_thy_ended) {
      repl_session.with_probe_before_end { insert =>
        insert(isar_string).foreach { offset =>
          note_timeout(repl_session.output_command_at_offset(offset, wall_budget_ms))
        }
      }  // bracket removes the probe afterwards (try/finally)
    }
    else {
      repl_session.send_edit(isar_string)
      note_timeout(repl_session.output_current_query_results(wall_budget_ms))  // read output first
      repl_session.discard_last_edit()  // then drop the transient command (cancels it if still running)
    }
  }

  /** Current theory source text; consumed via GET .../source by both MCPs. */
  def get_source(): Repl_Result = build_result {
    Repl_Output.add_output(repl_session.current_source)
  }
}
