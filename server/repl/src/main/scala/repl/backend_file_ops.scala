package repl

import isabelle._

/** LSP-like file-sync surface of [[ReplBackend]]: load a chunk (typically a
 *  whole document) and report per-command status WITHOUT rolling back ordinary
 *  failures — the broken state stays in the node so the caller can inspect and
 *  fix it (the dual of `verify_chunk`'s transactional semantics). Consumed via
 *  PUT /api/v1/sessions/{id}/document with report=true — the file-sync
 *  primitive of the planned LSP-like read-only MCP.
 *
 *  Also hosts the INCREMENTAL file-sync primitive `sync_document` (Phase B1):
 *  a spliff-diffed PIDE replace edit against the live node, so re-syncs after
 *  small agent edits re-process only the changed tail instead of resetting
 *  and re-elaborating the whole file (see its Scaladoc for the fallback
 *  contract and the divergent timeout semantics).
 *
 *  Also hosts the LSP-like READ-ONLY line queries (`command_at_line`,
 *  `goals_at_line`): snapshot-based, no edits and no ML probes, so they work at
 *  any document position — including past a trailing theory `end`, where the
 *  channel probes of Backend_Probes would hang.
 *
 *  Semantics: on BUDGET TIMEOUT the chunk IS discarded (to cancel a runaway
 *  command); on ordinary failure it is kept. The `probe_state` flag controls
 *  whether proof_open/pending_qed are probed — callers pass false when the
 *  text ends with theory `end`, where a trailing probe would never execute. */
trait Backend_File_Ops { this: ReplBackend =>

  /**
   * Load a chunk and report per-command status, WITHOUT rollback on ordinary
   * failure (LSP-style: broken state stays in the node so the caller can
   * inspect and fix it — the dual of verify_chunk's transactional semantics).
   * Returns the same JSON per-command report shape as verify_chunk.
   * Used by the LSP-like file-sync mode via load_document
   * (PUT /api/v1/sessions/{id}/document with report=true).
   *
   * Two caveats:
   *  - On BUDGET TIMEOUT the chunk IS discarded (discard_last_edit) to cancel
   *    the runaway command — leaving a looping `metis` churning is never what a
   *    caller wants. The report's `timed_out`/`running` line names the loop.
   *    With `keep_on_timeout` (step_chunk_report_kept, the file-binding load of the LSP
   *    MCP) the chunk is KEPT instead, exactly as sync_document keeps its replace: the
   *    commands go on running, the header stays processed, and the next load of the same
   *    file takes the incremental path. Discarding a first load that needs more than
   *    the budget made it start over on every call and never finish.
   *  - `proof_open`/`pending_qed` are only probed when the chunk succeeded AND
   *    `probe_state` is true. Callers pass probe_state=false when the text ends
   *    with theory `end`: a probe appended past `end` never executes, so the
   *    ML channel would hang until timeout. On failure both are false (the
   *    caller inspects `commands[]` instead).
   *  - When the kept chunk leaves the document ending with theory `end`, both
   *    are hardcoded false WITHOUT probing: a successful `end` means no proof
   *    is open (and probing past `end` would hang). The `probe_state` flag is
   *    kept as belt-and-braces for reports generated differently.
   */
  def step_chunk_report(isar_string: String, wall_budget_ms: Long, probe_state: Boolean): String =
    step_chunk_report_impl(isar_string, wall_budget_ms, probe_state, keep_on_timeout = false)

  /** step_chunk_report that keeps the chunk when the budget expires (see above). */
  def step_chunk_report_kept(isar_string: String, wall_budget_ms: Long, probe_state: Boolean): String =
    step_chunk_report_impl(isar_string, wall_budget_ms, probe_state, keep_on_timeout = true)

  private def step_chunk_report_impl(
      isar_string: String, wall_budget_ms: Long, probe_state: Boolean, keep_on_timeout: Boolean
  ): String = {
    Repl_Output.reset()
    // Guard on ENTERED (not begun): for a full-file load the theory header is part of
    // the chunk itself, so `current_thy_begun` is still false before this first edit —
    // send_edit processes the header as part of the insertion (need_header_processing).
    if (!repl_session.entered_some_thy)
      Json_Reports.failed_chunk_report("no theory entered")
    else {
      repl_session.send_edit(isar_string)
      val report = repl_session.chunk_status_report(wall_budget_ms)
      val proof_open =
        if (report.timed_out) { if (!keep_on_timeout) repl_session.discard_last_edit(); false }
        else if (!report.success) false
        else if (repl_session.current_thy_ended) false  // trailing `end`: theory closed
        else probe_state && in_proof()
      val pending_qed = proof_open && open_subgoals().isEmpty
      JSON.Format(report.fields + ("proof_open" -> proof_open) + ("pending_qed" -> pending_qed))
    }
  }

  /** INCREMENTAL counterpart of step_chunk_report (Phase B1): diff `text`
   *  against the live node and submit the difference as ONE PIDE edit, so PIDE
   *  re-processes only from the first changed command onward (jEdit-style),
   *  instead of the caller resetting and re-elaborating from line 1.
   *
   *  Returns the same JSON per-command report shape as step_chunk_report, with
   *  two contract additions:
   *   - `"line_semantics": "absolute"` — command `line` fields are ABSOLUTE
   *     node lines (the replace diff has no contiguous chunk to relativize
   *     against), and the report covers only the re-processed tail (commands
   *     starting at/after first_changed_line - 1).
   *   - `"fallback": "no_begun_theory" | "header_changed"` INSTEAD of a report
   *     when no incremental edit may be attempted (no begun theory, or the
   *     first diff hunk touches the `theory … begin` span — includes a
   *     theory-name change). The caller MUST fall back to the reset path.
   *
   *  TIMEOUT SEMANTICS differ from step_chunk_report ON PURPOSE: on budget
   *  expiry the replace edit is NOT discarded — a replace is an interleaved
   *  insert/remove sequence with no single insert to discard, unlike the
   *  append-only chunk step_chunk_report can drop via discard_last_edit. The
   *  report carries timed_out=true and the partial state stays for inspection
   *  (LSP-style). probe_state has the same meaning as in step_chunk_report. */
  def sync_document(text: String, wall_budget_ms: Long, probe_state: Boolean): String = {
    Repl_Output.reset()
    repl_session.replace_document(text, wall_budget_ms) match {
      case Left(reason) => Json_Reports.sync_fallback_report(reason)
      case Right(report) =>
        val proof_open =
          if (report.timed_out) false  // replace kept (no discard) — see scaladoc
          else if (!report.success) false
          else if (repl_session.current_thy_ended) false  // trailing `end`: theory closed
          else probe_state && in_proof()
        val pending_qed = proof_open && open_subgoals().isEmpty
        JSON.Format(
          report.fields
            + ("proof_open" -> proof_open)
            + ("pending_qed" -> pending_qed)
            + ("line_semantics" -> "absolute"))
    }
  }

  /** Read-only jEdit-style query: the command containing `line` (1-based) of the
   *  current node, as JSON {found, kind, source, range}. Snapshot-based — no edits,
   *  no ML probes — so it is safe at ANY document position, including past a trailing
   *  theory `end` (where the channel probes of Backend_Probes hang). Consumed via
   *  GET /api/v1/sessions/{id}/command_at_line by the LSP-like file-sync mode. */
  def command_at_line(line: Int): String = {
    Repl_Output.reset()
    repl_session.command_at_line(line)
  }

  /** Read-only jEdit-style query: rendered goal state before/after the command
   *  containing `line` (1-based), as JSON
   *  {found, command: {kind, source, range}, goals_before: [str], goals_after: [str]}.
   *  REQUIRES the `show_states` PIDE option (state messages only exist with it on —
   *  ISABELLE_SHOW_STATES, default true since Stage 2.2). Consumed via
   *  GET /api/v1/sessions/{id}/goals by the LSP-like file-sync mode. */
  def goals_at_line(line: Int): String = {
    Repl_Output.reset()
    repl_session.goals_at_line(line)
  }

  /** Hover info at a 1-based line/col (UTF-16 columns), as JSON
   *  {found, range, contents: [str]}. Snapshot + Rendering — no evaluation, no
   *  edits, no overlays. Consumed via GET /api/v1/sessions/{id}/hover by the
   *  LSP-like file-sync mode. */
  def hover_at(line: Int, col: Int): String = {
    Repl_Output.reset()
    repl_session.hover_at(line, col)
  }

  /** Go-to-definition at a 1-based line/col, as JSON {found, targets: [...]}.
   *  Heap/source entities resolve to file positions (`~~/` expanded); entities
   *  defined in the entry node itself resolve to in-node line ranges. Snapshot
   *  markup only — no evaluation. Consumed via GET /api/v1/sessions/{id}/definition
   *  by the LSP-like file-sync mode. */
  def definition_at(line: Int, col: Int): String = {
    Repl_Output.reset()
    repl_session.definition_at(line, col)
  }

  /** Sledgehammer on the open goal at a 1-based line (optional subgoal index),
   *  via the `isabelle_pool_server_sledgehammer` overlay print op (REPL.ML) — no text
   *  edits, no channel probes. JSON: {found, results: [str]} or
   *  {found:false, error} (no open goal / timeout). Consumed via
   *  POST /api/v1/sessions/{id}/sledgehammer_at (semaphore-bounded server-side). */
  /** Sledgehammer on the goal of the command with report index `index` (the
   *  server-side `fill_sorries` job: host = the command before a `sorry`), with
   *  `max_proofs` and an optional space-separated `provers` list. JSON:
   *  {found, subgoals, results} or {found:false, subgoals, error}. Semaphore-bounded
   *  server-side like the other sledgehammer entry points. */
  def sledgehammer_at_command(index: Int, subgoal: Int, timeout_s: Int, max_proofs: Int, provers: String): String = {
    Repl_Output.reset()
    repl_session.sledgehammer_at_command(index, subgoal, timeout_s, max_proofs, provers)
  }

  def sledgehammer_at(line: Int, subgoal: Int, timeout_s: Int): String = {
    Repl_Output.reset()
    repl_session.sledgehammer_at(line, subgoal, timeout_s)
  }
}
