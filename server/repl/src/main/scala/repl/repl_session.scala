package repl

import scala.language.unsafeNulls

import isabelle._

/** Per-session document state and execution engine beneath [[ReplBackend]]:
 *  text edits into the PIDE document, output extraction, the rollback chain
 *  (`rollback_last_text_edit` / `discard_last_edit`), checkpoints
 *  (`save_state` / `restore_state`), wall-bounded chunk status reports, and
 *  vectorised environments. Shared infrastructure consumed by BOTH workflows
 *  (chunk-centric and LSP-like file-sync) — all access is serialized through
 *  the session's single worker thread on the Python side. */
type EnvStateID = Long

object Repl_Session {
  /** Default wall budget for settle waits that carry no per-request timeout
   *  (rollback, vector_step). Env `ISABELLE_REPL_SETTLE_TIMEOUT` (seconds), default 60. */
  val SETTLE_BUDGET_MS: Long =
    sys.env.get("ISABELLE_REPL_SETTLE_TIMEOUT").flatMap(_.toLongOption).getOrElse(60L) * 1000L
}

class Repl_Session(session_manager: Session_Manager, initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), field: String = "HOL", session_dirs: List[String] = Nil) {
  //private val helper_thy = "$ISABELLE_REPL_HOME/thys/IsabelleREPL"
  private val helper_thy = initial_thys.headOption.getOrElse("$ISABELLE_REPL_HOME/thys/IsabelleREPL")

  private var session_thys: Map[String, Thy_Info] = Map.empty

  private var current_field: String = field

  private var session_data: Session_Data =
    session_manager.get_new_session(initial_thys, field, session_dirs)
  private var current_thy_info: Option[Thy_Info] = None

  private var initial_theories: List[String] = initial_thys

  private var current_state_id_counter: Long = 0

  def vector_current_duplicates: Option[List[Thy_Info]] = vector_env.flatMap(_.current_duplicates)
  private var vector_env: Option[Vector_Env] = None

  private class StateIDManager {
    private var count: EnvStateID = 0L

    def generate(): EnvStateID = {
      require(count < Long.MaxValue, "state id counter overflow")
      val result = count
      count += 1
      result
    }

    /** Only ids this manager has ISSUED (`count` is the next, never-issued one;
     *  `<= count` used to accept it — docs/ISSUES.md Bug 22 / audit REPL-4). */
    def valid(state_id: EnvStateID): Boolean = state_id >= 0 && state_id < count
  }

  private val state_id_manager = new StateIDManager

  private def session: Headless.Session = session_data.session

  /** Isabelle session phase as text (`Ready`, `Terminated(...)`, ...): the
   *  prover-level sign, if any, that the ML process is gone. */
  def phase_string: String = session.phase.toString

  def entered_some_thy: Boolean = current_thy_info.isDefined

  def current_thy_begun: Boolean = current_thy_info match {
    case Some(thy_info) if thy_info.header_processed => true
    case _                                           => false
  }

  def current_thy_name_string: String = current_thy_info.map(_.name).getOrElse("")

  private def current_thy_node_name = {
    val thy_info = current_thy_info.getOrElse(
      error("Cannot retrieve current node name if there is no current theory.")
    )
    Document_Utils.thy_node_name(thy_info.name, thy_info.master_dir)
  }

  private def update_session_with_edits(
      edits: List[Edit],
      node_name: Option[Document.Node.Name] = None
  ): Unit =
    session.update(
      Document.Blobs.empty,
      edits.map(edit => (node_name.getOrElse(current_thy_node_name), edit))
    )

  /** Requested imports outside the context of this session (docs/ISSUES.md
   *  Bug 25). A node's context comes from the session's wrapper theory
   *  (`helper_thy`, emulated as the node's only import by Edit_Utils
   *  .get_thy_header_edits); the imports in its own header only gate loading.
   *
   *  Covered = the wrapper's ancestry: walked through the document nodes'
   *  resolved imports (theories loaded at acquisition outside the heap) and the
   *  heap base's import graph (`loaded_theories`, `all_preds`). Every name is
   *  resolved as Isabelle resolves an import (`Resources.import_name`) and a
   *  session-qualified global theory is identified with its global name
   *  (`HOL.Complex_Main` is `Complex_Main`, `HOL.Main` is `Main`; review 83baa0a
   *  B1). A requested name is judged when it resolves into the heap graph or is
   *  qualified by a known session; an unqualified unknown name (a project
   *  theory) is not judged. Nothing is judged when the ancestry cannot be
   *  completed: the only line returned is then `?unjudged: <why>`, which the
   *  server logs (review 5a4ff3a S1). `session_theories` (the acquisition-time
   *  names) are the roots only when the wrapper node is not in the document.
   *  Otherwise returns the requested spellings that are judged and not covered. */
  def uncovered_imports(
      session_theories: List[String],
      requested: List[String]
  ): List[String] = {
    val resources = session.resources
    val graph = resources.current_background.base.loaded_theories
    val structure = resources.sessions_structure

    def canonical(theory: String): String =
      if (graph.defined(theory) || !Long_Name.is_qualified(theory)) theory
      else {
        val base = Long_Name.base_name(theory)
        val global_here =
          structure.global_theories.get(base).contains(Long_Name.qualifier(theory))
        if (graph.defined(base) && global_here) base else theory
      }
    def resolve(s: String): Option[String] =
      try Some(canonical(resources.import_name("", "", s).theory))
      catch { case ERROR(_) => None }  // malformed, or a file-path import from another session
    def judged(theory: String): Boolean =
      graph.defined(theory) ||
        (Long_Name.is_qualified(theory) && structure.defined(Long_Name.qualifier(theory)))

    val nodes = session.snapshot().version.nodes
    val doc_nodes: Map[String, Document.Node] =
      nodes.domain.iterator.map(name => canonical(name.theory) -> nodes(name)).toMap
    def imports_of(node: Document.Node): List[String] = node.thy.imports.map(_._1.theory)

    @annotation.tailrec
    def ancestry(todo: List[String], seen: Set[String], heap_roots: List[String]): Either[String, Set[String]] =
      todo match {
        case Nil => Right(seen ++ graph.all_preds(heap_roots))
        case t0 :: rest =>
          val t = canonical(t0)
          if (seen(t)) ancestry(rest, seen, heap_roots)
          else if (graph.defined(t)) ancestry(rest, seen + t, t :: heap_roots)
          else
            doc_nodes.get(t).filter(_.has_thy) match {
              case Some(node) => ancestry(imports_of(node) ::: rest, seen + t, heap_roots)
              case None => Left("ancestry stops at " + t)
            }
      }

    val wrapper_base = Library.space_explode('/', helper_thy).lastOption.getOrElse("").stripSuffix(".thy")
    val wrappers = doc_nodes.collect {
      case (theory, node) if Long_Name.base_name(theory) == wrapper_base && node.has_thy => node
    }.toList
    val roots: Either[String, List[String]] =
      if (wrappers.nonEmpty) Right(wrappers.flatMap(imports_of))
      else if (session_theories.isEmpty) Left("no wrapper node " + wrapper_base + " and no session theories")
      else {
        val resolved = session_theories.map(resolve)
        if (resolved.forall(_.isDefined)) Right(resolved.flatten)
        else Left("no wrapper node " + wrapper_base + ", unresolvable " + session_theories.mkString(","))
      }
    roots.flatMap(rs => ancestry(rs, Set.empty, Nil)) match {
      case Left(why) => List("?unjudged: " + why)
      case Right(covered) =>
        requested.filter(r => resolve(r).exists(t => judged(t) && !covered(t))).distinct
    }
  }

  def enter_thy(thy_name: String, master_dir: String = ""): Unit = {
    if (current_thy_info.isDefined) set_current_required(false)
    val thy_info = session_thys.getOrElse(
      thy_name, {
        val thy_info = new Thy_Info(thy_name)
        session_thys = session_thys + (thy_name -> thy_info)
        thy_info
      }
    )
    if (master_dir.nonEmpty) thy_info.master_dir = master_dir
    current_thy_info = Some(thy_info)
    set_current_required(true)
  }

  private def set_current_required(required: Boolean): Unit = {
    val required_edit = Edit_Utils.set_required_edit(required)
    update_session_with_edits(List(required_edit))
  }

  /** Print the current node's results into Repl_Output after waiting at most
   *  `budget_ms` for it to settle; false when the budget expired with a command
   *  still running (output partial). True when no theory is entered. */
  def output_current_node_results(budget_ms: Long): Boolean =
    current_thy_info.forall(thy_info =>
      Document_Utils.output_node_results(
        session,
        current_thy_node_name,
        thy_info.last_insertion_line,
        budget_ms
      )
    )

  /** Print only the results of the commands of the most recent insertion (the transient
   *  query of probe_transient) into Repl_Output, waiting at most `budget_ms` for the node
   *  to settle; false when the budget expired with a command still running. */
  def output_current_query_results(budget_ms: Long): Boolean =
    current_thy_info.forall(thy_info =>
      Document_Utils.output_query_results_since(
        session,
        current_thy_node_name,
        thy_info.last_insertion_line,
        budget_ms
      )
    )

  /** The current theory's most recent text edit (None when nothing was inserted
   *  yet, or after replace_document re-based the bookkeeping). Lets `step` tell
   *  whether its send_edit actually inserted before rolling back on timeout. */
  def last_text_edit = current_thy_info.flatMap(_.last_text_edit)

  /** Wall-bounded per-command status report for the just-inserted chunk: JSON + success. */
  def chunk_status_report(wall_budget_ms: Long): Chunk_Report =
    current_thy_info match {
      case Some(thy_info) =>
        Document_Utils.node_status_report(
          session,
          current_thy_node_name,
          thy_info.last_insertion_line,
          wall_budget_ms
        )
      case None =>
        Chunk_Report(false, Json_Reports.empty_chunk_fields())
    }

  /** Read-only jEdit-style line queries on a fresh stable snapshot (no edits, no ML
   *  probes). Consumed by Backend_File_Ops for the LSP-like file-sync mode. */
  def command_at_line(line: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.command_at_line_json(session, current_thy_node_name, line)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def goals_at_line(line: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.goals_at_line_json(session, current_thy_node_name, line)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  /** Snapshot/Rendering-based position queries (hover / go-to-definition) and the
   *  overlay-based position-explicit sledgehammer. Read-only; consumed by
   *  Backend_File_Ops for the LSP-like file-sync mode. */
  def hover_at(line: Int, col: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.hover_at_json(session, current_thy_node_name, line, col)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def definition_at(line: Int, col: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.definition_at_json(session, current_thy_node_name, line, col)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  /** Sledgehammer hosted on the command with the given report index (fill_sorries). */
  def sledgehammer_at_command(index: Int, subgoal: Int, timeout_s: Int, max_proofs: Int, provers: String): String =
    current_thy_info match {
      case Some(_) =>
        Document_Utils.sledgehammer_at_command_json(session, current_thy_node_name, index, subgoal, timeout_s, max_proofs, provers)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def sledgehammer_at(line: Int, subgoal: Int, timeout_s: Int): String =
    current_thy_info match {
      case Some(_) =>
        Document_Utils.sledgehammer_at_json(session, current_thy_node_name, line, subgoal, timeout_s)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  /** Host command for the "current state" overlay queries of Backend_Probes: the
   *  node's last non-ignored command (its result state IS the current toplevel
   *  state); with `skip_end` the trailing theory `end` is skipped. None when no
   *  theory is entered or the node has no command yet. See
   *  Document_Utils.current_state_host. */
  def current_state_host(skip_end: Boolean): Option[Command] =
    current_thy_info.flatMap(_ =>
      Document_Utils.current_state_host(session, current_thy_node_name, skip_end))

  /** Run a registered Query_Operation as a temporary overlay on `host` in the
   *  current node and collect its instance-tagged results: (finished-in-budget,
   *  content lines, error lines). No document edit; the overlay is always removed.
   *  See Document_Utils.overlay_query. */
  def overlay_query(
      host: Command,
      print_fn: String,
      args: List[String],
      budget_ms: Long
  ): (Boolean, List[String], List[String]) =
    Document_Utils.overlay_query(session, current_thy_node_name, host, print_fn, args, budget_ms)

  /** True when the current document ends with theory `end` (last non-ignored command
   *  has span `end`). A successful `end` implies NO proof is open, and after `end`
   *  there is no theory context: a command appended past it never executes (parsed
   *  without a theory context — "missing theory context"). Backend_Probes uses this
   *  to short-circuit trivial post-`end` answers, host the fact queries on the
   *  command before `end`, and reroute probe_transient through with_probe_before_end. */
  def current_thy_ended: Boolean =
    current_thy_info.exists(_ => Document_Utils.node_ends_with_end(session, current_thy_node_name))

  /** Insert `isar_string` immediately BEFORE the trailing theory `end` command,
   *  bypassing Thy_Info's append-only insertion_point bookkeeping (the document tip
   *  logically stays at `end`). A trailing newline is appended so the probe span does
   *  not glue to the `end` command. Returns the insertion offset and the exact
   *  inserted text; None when the node has no `end` command. */
  private def insert_before_end(isar_string: String): Option[(Text.Offset, String)] =
    if (!entered_some_thy) None
    else
      Document_Utils.last_end_offset(session, current_thy_node_name).map { end_offset =>
        val text = isar_string + "\n"
        update_session_with_edits(
          List(Edit_Utils.edit_from_text_edit(Text.Edit.insert(end_offset, text)))
        )
        (end_offset, text)
      }

  /** Symmetric remove for insert_before_end. */
  private def remove_before_end(offset: Text.Offset, text: String): Unit =
    if (entered_some_thy)
      update_session_with_edits(
        List(Edit_Utils.edit_from_text_edit(Text.Edit.remove(offset, text)))
      )

  /** Bracket for the INSERTED transient command (probe_transient) on a FINISHED
   *  theory: `body` receives an inserter that places the command immediately before
   *  the trailing `end` and returns its offset. `body` is responsible for the
   *  (wall-bounded) wait on the command's evaluation — output_command_at_offset does
   *  it — and the insert is ALWAYS removed afterwards (try/finally, also when `body`
   *  throws), leaving the document byte-identical; removing a still-running command
   *  cancels it, which is the intended outcome on a timeout. Thy_Info bookkeeping is
   *  bypassed, so discard_last_edit must NOT be called for these inserts. */
  def with_probe_before_end[T](body: (String => Option[Text.Offset]) => T): T = {
    var inserted: Option[(Text.Offset, String)] = None
    try {
      body { text =>
        inserted = insert_before_end(text)
        inserted.map(_._1)
      }
    } finally {
      inserted.foreach { case (offset, text) => remove_before_end(offset, text) }
    }
  }

  /** Output the results of the single command starting at `offset` — for mid-document
   *  probes (with_probe_before_end), where output_current_node_results'
   *  last-insertion-line filter would hide them. */
  def output_command_at_offset(offset: Text.Offset, budget_ms: Long): Boolean =
    current_thy_info.forall(_ =>
      Document_Utils.output_command_at_offset(session, current_thy_node_name, offset, budget_ms))

  /** INCREMENTAL whole-document replacement (jEdit-style file sync, Phase B1):
   *  diff `new_text` against the node's CURRENT source (spliff, the same
   *  mechanism as Thy_Status.difference_edits / restore_state) and submit the
   *  difference as ONE PIDE edit, so PIDE re-processes only from the first
   *  changed command onward — instead of reset + re-elaboration from line 1.
   *
   *  Returns Left(reason) when no incremental edit may be attempted — the
   *  caller MUST fall back to the reset path:
   *    - "no_begun_theory": no theory entered, or the header was never
   *      processed (there is no stable document base to diff against);
   *    - "header_changed": the FIRST diff hunk touches the theory header text
   *      (`theory … begin` of the OLD text, without the whitespace after it) —
   *      includes a theory-name change. Header edits are not applied incrementally.
   *
   *  Otherwise the node text becomes exactly `new_text + "\n"` (mirroring
   *  send_edit's trailing newline, so the fast path and the reset path
   *  converge to the same node text), the Thy_Info bookkeeping is re-based to
   *  a fresh append-only base (Thy_Info.reset_to_fresh_base — the old
   *  rollback/checkpoint chain is CUT: pre-replace checkpoints are unsound),
   *  and Right(report) carries a wall-bounded status report covering the whole
   *  node with ABSOLUTE node lines (only the commands from the first change on
   *  are re-processed; the earlier ones keep their settled status). On budget expiry the replace edit is NOT
   *  discarded (a replace is an interleaved insert/remove sequence with no
   *  single insert to discard) — timed_out=true and the partial state stays
   *  for inspection (LSP-style, intentionally unlike verify_chunk). */
  def replace_document(new_text: String, wall_budget_ms: Long): Either[String, Chunk_Report] =
    current_thy_info match {
      case Some(thy_info) if thy_info.header_processed =>
        val node_name = current_thy_node_name
        val old_text = Document_Utils.node_source(session, node_name)
        Document_Utils.header_end_offset(session, node_name) match {
          case None => Left("no_begun_theory")
          case Some(header_end) =>
            val target_text = new_text + "\n"
            val (text_edits, first_change) =
              Edit_Utils.text_diff_edits(old_text, target_text)
            // The header text is unchanged when the first change lies at or after its
            // end AND the new text still separates `begin` from what follows (an edit
            // right at the end must not glue a token onto `begin`).
            val glued =
              target_text.length > header_end && !Character.isWhitespace(target_text.charAt(header_end))
            if (first_change.exists(c => c < header_end || (c == header_end && glued)))
              Left("header_changed")
            else {
              if (text_edits.nonEmpty)
                update_session_with_edits(List(Edit_Utils.edit_from_text_edits(text_edits)))
              thy_info.reset_to_fresh_base(target_text)
              // The report covers the WHOLE node, not only the re-processed tail:
              // the caller replaces the file's diagnostics (and multi_attempt its
              // prefix_failed) with it, so a command above the edit that still
              // fails must stay in it. Unchanged commands are already settled, so
              // this adds no waiting.
              Right(Document_Utils.node_status_report(
                session, node_name, 1, wall_budget_ms, absolute_lines = true))
            }
        }
      case _ => Left("no_begun_theory")
    }

  def send_edit(isar_string: String, node: Option[Document.Node.Name] = None): Unit = {
    val edits = current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot make edits without entering theory.")
        List()
      case Some(thy_info) =>
        val text_edit = Edit_Utils.insert_text_edit(isar_string, thy_info)
        val edit_ok = thy_info.update_given_text_edit(text_edit)
        if (!edit_ok) List()
        else {
          val additional_edits: List[Edit] =
            if (thy_info.need_header_processing)
              Edit_Utils
                .get_thy_header_edits(
                  thy_info,
                  session,
                  current_thy_node_name,
                  List(helper_thy)
                )
                .getOrElse(List())
            else List()
          Edit_Utils.edit_from_text_edit(text_edit) :: additional_edits
        }
    }
    if (edits.nonEmpty) update_session_with_edits(edits, node)
  }

  def send_vector_edit(isar_strings: List[String]): Unit =
    vector_current_duplicates.foreach { thy_infos =>
      thy_infos.zip(isar_strings).foreach { case (thy_info, isar_string) =>
        send_edit(isar_string, node = Some(Document_Utils.thy_node_name(thy_info.name, thy_info.master_dir)))
      }
    }

  def current_source: String =
    if (entered_some_thy) Document_Utils.node_source(session, current_thy_node_name).nn.strip()
    else ""

  def rollback_last_text_edit(): Unit =
    current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot rollback without entering theory.")
      case Some(thy_info) =>
        thy_info.last_text_edit match {
          case None => Repl_Output.add_error("No text edits have been made to rollback.")
          case Some(last_edit) =>
            val remove_edit = Edit_Utils.remove_insert_edit(last_edit)
            update_session_with_edits(List(Edit_Utils.edit_from_text_edit(remove_edit)))
            thy_info.rollback_last_text_edit_if_exists()
        }
    }

  /** Silently remove the most recent text edit, if any. Used to make the INSERTED
   *  transient command of probe_transient (and a failed/timed-out chunk in
   *  verify_chunk / step_chunk_report) leave no trace in the document or the rollback
   *  chain: the command is inserted and evaluated, its result is read, then this drops
   *  it. (The state queries — subgoals / facts / sledgehammer / proof state — are
   *  overlays since 2026-09-30 and never insert anything; when they were `ML_val`
   *  inserts, a missed discard left a trailing probe edit that user `rollback` peeled
   *  first, needing two calls to undo one line.) Unlike `rollback_last_text_edit`,
   *  this never writes to Repl_Output (safe to call outside `build_result`). */
  def discard_last_edit(): Unit =
    current_thy_info.foreach { thy_info =>
      thy_info.last_text_edit.foreach { last_edit =>
        update_session_with_edits(
          List(Edit_Utils.edit_from_text_edit(Edit_Utils.remove_insert_edit(last_edit)))
        )
        thy_info.rollback_last_text_edit_if_exists()
      }
    }

  def save_state(): EnvStateID = {
    val env_state_id = state_id_manager.generate()
    session_thys.foreach { case (_, thy_info) => thy_info.save_current_state(env_state_id) }
    env_state_id
  }

  /** Restore checkpoint `state_id` in every entered theory. ALL-OR-NOTHING: false
   *  (and no edit applied) unless the id was issued by save_state AND every
   *  theory still holds it — an id is dropped by reset_to_fresh_base after an
   *  incremental document replace (Repl_Session.replace_document), and unknown
   *  ids used to restore nothing while reporting true (Bug 22 / REPL-4). */
  def restore_state(state_id: EnvStateID): Boolean =
    if (!state_id_manager.valid(state_id)) false
    else if (!session_thys.values.forall(_.has_saved_state(state_id))) false
    else {
      session_thys.foreach { case (_, thy_info) =>
        val thy_node_name = Document_Utils.thy_node_name(thy_info.name, thy_info.master_dir)
        thy_info.restore_state(state_id).foreach { text_edits_required =>
          val edits = List(Edit_Utils.edit_from_text_edits(text_edits_required))
          update_session_with_edits(edits, node_name = Some(thy_node_name))
        }
      }
      true
    }

  def reset_with_cache(): Unit = {
    val new_session_data = session_manager.get_new_session(initial_theories, current_field, session_dirs)
    
    session_thys = Map.empty
    current_thy_info = None
    vector_env = None
    
    session_data = new_session_data
  }
  def stop(): Unit = session_manager.remove_session_async(session_data)
  

  def stop_with_cache(): Unit = {

    session_manager.release_session_to_cache(session_data, initial_theories)
  }
  def vectorise(size: Int): Unit = {
    require(size > 0, "Vector size must be positive")
    vector_env = Some(Vector_Env(size, current_thy_info))
  }

  def scalarise(env_to_keep: Int): Unit = {
    vector_env
      .getOrElse(error("can't scalarise when already in scalar mode"))
      .scalarise(env_to_keep)
  }

}
