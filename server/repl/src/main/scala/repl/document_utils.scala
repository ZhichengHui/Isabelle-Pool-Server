package repl

import isabelle._

/** PIDE document snapshot utilities beneath [[Repl_Session]]: node snapshots (never
 *  waiting for evaluation) and WALL-BOUNDED settle barriers, per-command output extraction, source retrieval, and the
 *  wall-bounded per-command status report (`node_status_report` /
 *  [[Chunk_Report]]) that backs both `verify_chunk` (chunk-centric MCP) and
 *  `step_chunk_report` (LSP-like file-sync via PUT .../document). Shared
 *  infrastructure consumed by BOTH workflows. */

/** Result of a wall-bounded chunk verification: the JSON report `fields` plus a `success`
 *  flag computed under the SAME rule the server uses (router.py): not timed out, at least
 *  one reported command, and every reported command `ok`. `verify_chunk` uses `success` to
 *  decide whether to keep the chunk in the node or roll it back transactionally, and may
 *  enrich `fields` (e.g. with `proof_open`) before serialising. `timed_out` is exposed
 *  separately (not just inside `fields`) so callers like `step_chunk_report` can cancel
 *  runaway commands without re-parsing the JSON. */
case class Chunk_Report(success: Boolean, fields: JSON.Object.T, timed_out: Boolean = false) {
  def json: String = JSON.Format(fields)
}

object Document_Utils {
  def thy_node_name(thy_name: String): Document.Node.Name = {
    val qualifier = Sessions.DRAFT
    Document.Node.Name(s"$qualifier.$thy_name", theory = thy_name)
  }

  /** Fresh snapshot of the node: waits only until the session is STABLE (no pending
   *  edits — the latest version is assigned), NEVER for command evaluation. Use it
   *  for every syntactic or read-what-is-there query (command structure, source,
   *  markup, results already present): those must not block behind a running command. */
  private def node_snapshot(session: Headless.Session, node_name: Document.Node.Name): Document.Snapshot =
    session.await_stable_snapshot().switch(node_name)

  /** Snapshot after waiting — at most `budget_ms` — for EVERY command in the node to be
   *  consolidated. Returns (snapshot, settled). Never unbounded: a still-running command
   *  (a looping `metis`, or a runaway left in place by a timed-out sync_document) makes
   *  this return settled=false at the deadline instead of wedging the session's single
   *  worker thread for the command's whole runtime (docs/ISSUES.md Bug 15 / audit REPL-2).
   *  Callers that PRINT a command's results use this; on settled=false they must not
   *  assume the results are final. */
  private def settled_node_snapshot(
      session: Headless.Session,
      node_name: Document.Node.Name,
      budget_ms: Long
  ): (Document.Snapshot, Boolean) = {
    val deadline = System.currentTimeMillis() + budget_ms
    var snapshot = node_snapshot(session, node_name)

    def all_processed: Boolean = {
      snapshot = node_snapshot(session, node_name)
      val version = snapshot.version
      val state   = session.get_state()
      snapshot.node.commands.forall { command =>
        scala.util.Try(state.command_states(version, command))
          .fold(
            _   => true,  // version no longer tracked → PIDE advanced → done
            sts => sts.exists(st => st.maybe_consolidated || st.consolidated)
          )
      }
    }

    var settled = all_processed
    while (!settled && System.currentTimeMillis() < deadline) {
      session.output_delay.sleep()
      settled = all_processed
    }
    (snapshot, settled)
  }

  private def pretty_print_results(
      command: Command,
      results: Command.Results,
      hide_state_messages: Boolean
  ): Unit = {

    def output_pretty_if_non_empty(body: XML.Body, output_f: String => Unit): Unit = {
      val pretty_string = Pretty.string_of(body)
      if (pretty_string.nonEmpty) output_f(pretty_string)
    }
    results.iterator
      .foreach(_._2 match {
        case XML.Elem(Markup(markup_type, _), body) =>
          markup_type match {
            case Markup.WRITELN_MESSAGE =>
              if (!hide_state_messages)
                output_pretty_if_non_empty(body, Repl_Output.add_output)
            case Markup.STATE_MESSAGE =>
              if (!(command.is_ignored || hide_state_messages))
                output_pretty_if_non_empty(body, Repl_Output.add_output)
            case Markup.ERROR_MESSAGE | Markup.WARNING_MESSAGE =>
              output_pretty_if_non_empty(body, Repl_Output.add_error)
            case _ => output_pretty_if_non_empty(body, Repl_Output.add_output)
          }
      })
  }

  /** Results of ONE transient query command (probe_transient), into Repl_Output: its
   *  writeln output as output, its errors as errors. Its warnings go to the output with a
   *  "Warning:" prefix (a warning of the query is not a failure of the query), and proof
   *  state messages are left out (a query shows what it was asked for, not the state). */
  private def print_query_results(results: Command.Results): Unit =
    results.iterator.foreach(_._2 match {
      case XML.Elem(Markup(markup_type, _), body) =>
        val text = Pretty.string_of(body)
        if (text.nonEmpty)
          markup_type match {
            case Markup.WRITELN_MESSAGE => Repl_Output.add_output(text)
            case Markup.ERROR_MESSAGE   => Repl_Output.add_error(text)
            case Markup.WARNING_MESSAGE => Repl_Output.add_output("Warning: " + text)
            case Markup.STATE_MESSAGE   => ()
            case _                      => Repl_Output.add_output(text)
          }
      case _ => ()
    })

  /** Like output_node_results, but ONLY for the commands starting at/after
   *  `since_line` — the transient query command appended at the end of the node. The
   *  messages of the commands before it (warnings of the proof script, the state of
   *  the last proof step) are not part of the query's answer. Same wait and return
   *  value as output_node_results. */
  def output_query_results_since(
      session: Headless.Session,
      node_name: Document.Node.Name,
      since_line: Int,
      budget_ms: Long
  ): Boolean = {
    val (node_snapshot, settled) = settled_node_snapshot(session, node_name, budget_ms)
    node_snapshot.node.commands.foreach { command =>
      val start_line = node_snapshot.node.command_start_line(command).getOrElse(1)
      if (start_line >= since_line) print_query_results(node_snapshot.command_results(command))
    }
    settled
  }

  /** Print every command's results into Repl_Output (state messages only for commands
   *  at/after `last_insertion_start_line`), after waiting at most `budget_ms` for the
   *  node to settle. Returns false when the budget expired with a command still running
   *  — the printed output is then partial and the caller decides what to do with the
   *  runaway (step rolls it back). */
  def output_node_results(
      session: Headless.Session,
      node_name: Document.Node.Name,
      last_insertion_start_line: Int,
      budget_ms: Long
  ): Boolean = {
    val (node_snapshot, settled) = settled_node_snapshot(session, node_name, budget_ms)
    val node_commands = node_snapshot.node.commands

    node_commands.foreach { command =>
      val command_results = node_snapshot.command_results(command)
      val start_line = node_snapshot.node.command_start_line(command).getOrElse(1)
      val hide_state_messages = start_line < last_insertion_start_line
      pretty_print_results(
        command,
        command_results,
        hide_state_messages = hide_state_messages
      )
    }
    settled
  }

  /** Absolute start offset of the node's LAST `end` command (theory or block end),
   *  if any. Used to place probes before a trailing theory `end`. */
  def last_end_offset(session: Headless.Session, node_name: Document.Node.Name): Option[Text.Offset] = {
    val snapshot = node_snapshot(session, node_name)  // syntactic: no evaluation wait
    snapshot.node.command_iterator().toList
      .collect { case (command, offset) if command.span.name == "end" => offset }
      .lastOption
  }

  /** End offset (exclusive) of the node's theory-HEADER command (`theory …
   *  begin` is ONE command span named "theory"), if the node has one. Used by
   *  Repl_Session.replace_document to detect diffs that touch the header —
   *  those are NOT applied incrementally (the caller falls back to reset). */
  def header_end_offset(session: Headless.Session, node_name: Document.Node.Name): Option[Text.Offset] = {
    val snapshot = node_snapshot(session, node_name)  // syntactic: no evaluation wait
    snapshot.node.command_iterator().toList
      .collectFirst { case (command, offset) if command.span.name == "theory" =>
        offset + command.length
      }
  }

  /** True when the node's LAST non-ignored command is `end` (theory or block end —
   *  either way no proof can be open immediately after it, and commands appended past
   *  a trailing theory `end` never execute). Drives the post-`end` probe handling in
   *  Backend_Probes / Backend_File_Ops. */
  def node_ends_with_end(session: Headless.Session, node_name: Document.Node.Name): Boolean = {
    // Syntactic question (command structure only): no evaluation wait, or a
    // still-running command would block every state query behind it.
    val snapshot = node_snapshot(session, node_name)
    snapshot.node.commands.reverse.iterator
      .find(command => !command.is_ignored)
      .exists(_.span.name == "end")
  }

  /** Output the results of the single command STARTING AT `offset` — for mid-document
   *  inserts (the diagnostic probe placed before a trailing `end`), where
   *  output_node_results' last-insertion-line filter would hide them. Waits at most
   *  `budget_ms` for the node to settle; returns false on expiry (output partial). */
  def output_command_at_offset(
      session: Headless.Session,
      node_name: Document.Node.Name,
      offset: Text.Offset,
      budget_ms: Long
  ): Boolean = {
    val (snapshot, settled) = settled_node_snapshot(session, node_name, budget_ms)
    snapshot.node.command_iterator().toList
      .find { case (_, command_offset) => command_offset == offset }
      .foreach { case (command, _) => print_query_results(snapshot.command_results(command)) }
    settled
  }

  /**
   * Status-aware, WALL-BOUNDED per-command report for a freshly inserted chunk.
   *
   * Unlike `output_node_results` (which collapses everything into a flat buffer and waits
   * on an UNBOUNDED whole-node barrier), this:
   *   - polls until every command is consolidated/failed OR `wall_budget_ms` elapses
   *     (exactly ONE timeout; no per-command timeouts);
   *   - classifies each command (at/after `since_line`, i.e. the inserted chunk) as
   *     ok | failed | running | unprocessed via `Document_Status.Command_Status`;
   *   - on budget expiry returns the PARTIAL status (the still-`running` line is the loop),
   *     never throws.
   * Parallel proof checking (parallel_proofs) stays on underneath; the report is just
   * enumerated in source order.
   *
   * Each command also carries a `range` — its 1-based line+column start/end (columns are
   * UTF-16 units, i.e. LSP columns). Message-level position offsets do NOT exist for our
   * Sessions.DRAFT nodes (PIDE span tokens are position-less; jEdit underlines at command
   * granularity for the same reason), so diagnostics carry their OWNING COMMAND's range —
   * that is the design, not a workaround.
   *
   * Returns a JSON string: {"timed_out":bool,"elapsed_ms":int,
   *   "commands":[{"i":int,"line":int (chunk-relative, 1-based),"node_line":int (absolute),
   *                "kind":str,"status":str,
   *                "range":{"start":{"line":int,"col":int},"end":{"line":int,"col":int}},
   *                "messages":[{"sev":"error|warning","text":str}]}]}
   *
   * With `absolute_lines = true` (Repl_Session.replace_document's incremental
   * sync) the `line` field is the ABSOLUTE node line instead of chunk-relative —
   * the sync has no contiguous "chunk" to relativize against. The backend caller
   * marks such reports with "line_semantics": "absolute".
   */
  def node_status_report(
      session: Headless.Session,
      node_name: Document.Node.Name,
      since_line: Int,
      wall_budget_ms: Long,
      absolute_lines: Boolean = false
  ): Chunk_Report = {
    val start_ms = System.currentTimeMillis()
    val deadline = start_ms + wall_budget_ms

    def snap(): Document.Snapshot = session.await_stable_snapshot().switch(node_name)

    def all_settled(snapshot: Document.Snapshot): Boolean = {
      val version = snapshot.version
      val state = session.get_state()
      snapshot.node.commands.forall { command =>
        scala.util.Try(state.command_status(version, command)).fold(
          _ => true, // version no longer tracked -> PIDE advanced -> done
          st => st.maybe_consolidated || st.is_failed
        )
      }
    }

    var snapshot = snap()
    var timed_out = false
    while (!timed_out && !all_settled(snapshot)) {
      if (System.currentTimeMillis() >= deadline) timed_out = true
      else { session.output_delay.sleep(); snapshot = snap() }
    }

    val version = snapshot.version
    val state = session.get_state()

    def message_objs(command: Command): List[JSON.T] = {
      val results = snapshot.command_results(command)
      results.iterator.toList.flatMap {
        case (_, XML.Elem(Markup(markup_type, _), body)) =>
          val sev = markup_type match {
            case Markup.ERROR_MESSAGE   => Some("error")
            case Markup.WARNING_MESSAGE => Some("warning")
            case _                      => None
          }
          sev.flatMap { s =>
            val text = Pretty.string_of(body)
            if (text.nonEmpty) Some(JSON.Object("sev" -> s, "text" -> text): JSON.T)
            else None
          }
        case _ => None
      }
    }

    // One Line.Document for the whole node: converts each command's absolute char-offset
    // extent into a 1-based line/column range (columns = UTF-16 units = LSP columns).
    val line_doc = Line.Document(snapshot.node.source)

    // Per-command range JSON; None if offset/range conversion fails for a command
    // (shouldn't happen — the range is simply omitted rather than failing the report).
    def range_obj(start_offset: Text.Offset, length: Int): Option[JSON.T] =
      try {
        val r = line_doc.range(Text.Range(start_offset, start_offset + length))
        Some(JSON.Object(
          "start" -> JSON.Object("line" -> r.start.line1, "col" -> r.start.column1),
          "end" -> JSON.Object("line" -> r.stop.line1, "col" -> r.stop.column1)))
      } catch { case _: Exception => None }

    def command_status_name(command: Command): String =
      scala.util.Try(state.command_status(version, command)).toOption match {
        case Some(s) if s.is_failed          => "failed"
        case Some(s) if s.maybe_consolidated => "ok"
        case Some(s) if s.is_running         => "running"
        case Some(_)                         => "unprocessed"
        case None                            => "ok" // PIDE advanced past this version
      }

    // Status per reported command, paired with its JSON object. We keep `status` alongside
    // the JSON so `success` can be computed without re-parsing the JSON we just built.
    // command_iterator preserves source order and yields the same command objects as
    // node.commands, plus each command's node-absolute char-offset start.
    val cmd_pairs: List[(String, JSON.T)] =
      snapshot.node.command_iterator().toList.zipWithIndex.flatMap { case ((command, start_offset), i) =>
        val start_line = snapshot.node.command_start_line(command).getOrElse(1)
        if (start_line < since_line || command.is_ignored) None
        else {
          val status = command_status_name(command)
          val base_fields: JSON.Object.T = JSON.Object(
            // chunk-relative line (1-based within the submitted chunk), so stuck_line /
            // failed line maps to the text the caller sent — not the absolute line in the
            // accumulated node. `node_line` keeps the absolute line for debugging. With
            // absolute_lines (incremental document sync), `line` IS the absolute line:
            // the replace diff has no contiguous chunk to relativize against.
            "i" -> i,
            "line" -> (if (absolute_lines) start_line else start_line - since_line + 1),
            "node_line" -> start_line,
            "kind" -> command.span.name,
            "status" -> status,
            "messages" -> message_objs(command)
          )
          val obj: JSON.T =
            range_obj(start_offset, command.length) match {
              case Some(range_json) => base_fields + ("range" -> range_json)
              case None             => base_fields
            }
          Some((status, obj))
        }
      }

    // Match the server's success rule (router.py): not timed out, at least one reported
    // command, and every reported command `ok`.
    val success = !timed_out && cmd_pairs.nonEmpty && cmd_pairs.forall(_._1 == "ok")

    // Authoritative `sorry`/`oops` detection: scan the chunk's PARSED commands (not the raw
    // text) so a `sorry` in a comment or string literal is NOT a false positive, while a real
    // `sorry`/`oops` command IS caught regardless of spacing. A theorem closed via sorry/oops
    // is not actually proved, so callers must treat used_sorry=true as "not proved".
    val used_sorry = snapshot.node.commands.exists { command =>
      val start_line = snapshot.node.command_start_line(command).getOrElse(1)
      start_line >= since_line && !command.is_ignored &&
        (command.span.name == "sorry" || command.span.name == "oops")
    }

    // Whole-node counterpart of used_sorry (independent of `since_line`, so an incremental
    // report that re-processed only the tail still knows about earlier ones): the absolute
    // start lines of the `sorry`/`oops` commands that were EXECUTED (status ok), i.e. not
    // failed, still running or unprocessed. Callers split them by line (prefix vs candidate).
    val sorry_lines: List[Int] =
      snapshot.node.commands.toList.flatMap { command =>
        if (!command.is_ignored && (command.span.name == "sorry" || command.span.name == "oops") &&
            command_status_name(command) == "ok")
          snapshot.node.command_start_line(command)
        else None
      }.sorted

    val fields: JSON.Object.T = JSON.Object(
      "timed_out" -> timed_out,
      "success" -> success,
      "used_sorry" -> used_sorry,
      "sorry_lines" -> sorry_lines,
      "elapsed_ms" -> (System.currentTimeMillis() - start_ms).toInt,
      "commands" -> cmd_pairs.map(_._2)
    )
    Chunk_Report(success, fields, timed_out)
  }

  // -----------------------------------------------------------------------
  // Read-only line-based queries (command_at_line / goals_at_line), consumed
  // via GET .../command_at_line and GET .../goals by the LSP-like file-sync
  // mode. Fully read-only: immutable snapshot, no edits, no ML probes — so
  // they work at ANY document position, including past a trailing theory `end`
  // (where the channel probes of Backend_Probes would hang).
  // -----------------------------------------------------------------------

  /** Shared core: a fresh stable snapshot of the node plus the command containing
   *  `line` (1-based, matching the chunk-report convention), resolved with jEdit
   *  cursor semantics (Snapshot.current_command: the command at the line's first
   *  non-blank character, else the nearest non-ignored command before it — comment
   *  and blank lines map to the preceding command). The first non-blank character,
   *  not column 0: the whitespace in front of an indented command belongs to the
   *  PRECEDING command's span, so column 0 would answer with the previous command.
   *  None when `line` is out of range. */
  private def command_containing_line(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): Option[(Document.Snapshot, Line.Document, Command, Text.Offset)] =
    if (line < 1) None
    else {
      val snapshot = node_snapshot(session, node_name)  // read-only: results as they are now
      val line_doc = Line.Document(snapshot.node.source)
      val indent =
        line_doc.lines.lift(line - 1).map(_.text.takeWhile(c => c == ' ' || c == '\t').length).getOrElse(0)
      for {
        offset <- line_doc.offset(Line.Position(line = line - 1, column = indent))
        command <- snapshot.current_command(node_name, offset)
        start_offset <- snapshot.node.command_start(command)
      } yield (snapshot, line_doc, command, start_offset)
    }

  /** JSON {kind, source, range} for one command; `range` omitted if the offset/range
   *  conversion fails (shouldn't — the offset comes from the node itself). */
  private def command_info_json(
      line_doc: Line.Document,
      command: Command,
      start_offset: Text.Offset
  ): JSON.Object.T = {
    val base: JSON.Object.T = JSON.Object(
      "kind" -> command.span.name,
      "source" -> command.source)
    try {
      val r = line_doc.range(Text.Range(start_offset, start_offset + command.length))
      base + ("range" -> JSON.Object(
        "start" -> JSON.Object("line" -> r.start.line1, "col" -> r.start.column1),
        "end" -> JSON.Object("line" -> r.stop.line1, "col" -> r.stop.column1)))
    } catch { case _: Exception => base }
  }

  /** Last STATE_MESSAGE (rendered proof state) a command produced, if any. State
   *  messages only exist when the `show_states` PIDE option is on
   *  (ISABELLE_SHOW_STATES). */
  private def last_state_message(snapshot: Document.Snapshot, command: Command): Option[String] =
    snapshot.command_results(command).iterator.toList.collect {
      case (_, XML.Elem(Markup(markup_type, _), body)) if markup_type == Markup.STATE_MESSAGE =>
        Pretty.string_of(body)
    }.lastOption

  /** The command containing `line` (1-based) of the node, as JSON
   *  {found, kind, source, range}; {found:false, error} when out of range. */
  def command_at_line_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): String =
    command_containing_line(session, node_name, line) match {
      case Some((_, line_doc, command, start_offset)) =>
        JSON.Format(command_info_json(line_doc, command, start_offset) + ("found" -> true))
      case None =>
        Json_Reports.line_query_not_found(s"no command at line $line")
    }

  /** Rendered goal state before/after the command containing `line` (1-based), as JSON
   *  {found, command: {kind, source, range}, goals_before: [str], goals_after: [str]}.
   *
   *  `goals_after` is the command's LAST state message as ONE raw text element (no
   *  subgoal splitting — the numbered-subgoal format is not parsed). `goals_before`
   *  is the last state message of the immediately preceding non-ignored command, or
   *  [] when that command produced none (e.g. outside a proof). Both are empty when
   *  `show_states` is off. */
  def goals_at_line_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int
  ): String =
    command_containing_line(session, node_name, line) match {
      case Some((snapshot, line_doc, command, start_offset)) =>
        val goals_after = last_state_message(snapshot, command).toList
        val commands = snapshot.node.command_iterator().toList
        val index = commands.indexWhere { case (c, _) => c == command }
        val previous_command =
          if (index <= 0) None
          else commands.take(index).reverse.collectFirst { case (c, _) if !c.is_ignored => c }
        val goals_before = previous_command.flatMap(last_state_message(snapshot, _)).toList
        JSON.Format(JSON.Object(
          "found" -> true,
          "command" -> command_info_json(line_doc, command, start_offset),
          "goals_before" -> goals_before,
          "goals_after" -> goals_after))
      case None =>
        Json_Reports.line_query_not_found(s"no command at line $line")
    }

  // -----------------------------------------------------------------------
  // Hover / go-to-definition (fresh stable snapshot + Rendering; NO evaluation,
  // NO overlays). Consumed via GET .../hover and GET .../definition by the
  // LSP-like file-sync mode. Positions are 1-based line/col, UTF-16 columns
  // (LSP conventions, same as the Stage-2.1 chunk-report ranges).
  // -----------------------------------------------------------------------

  private def offset_at(line_doc: Line.Document, line: Int, col: Int): Option[Text.Offset] =
    if (line < 1 || col < 1) None
    else line_doc.offset(Line.Position(line = line - 1, column = col - 1))

  private def range_fields(r: Line.Range): JSON.Object.T =
    JSON.Object(
      "start" -> JSON.Object("line" -> r.start.line1, "col" -> r.start.column1),
      "end" -> JSON.Object("line" -> r.stop.line1, "col" -> r.stop.column1))

  /** Expand a leading `~~/` (Isabelle system prefix) to $ISABELLE_HOME so clients
   *  get an openable absolute path. */
  private def expand_isabelle_home(name: String): String =
    if (name.startsWith("~~/")) Isabelle_System.getenv("ISABELLE_HOME") + name.stripPrefix("~~")
    else name

  /** Hover info at a 1-based line/col: {found, range, contents: [str]}; the contents
   *  are the tooltip entries (entity kinds, types, documentation) as pretty text. */
  def hover_at_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int,
      col: Int
  ): String = {
    val snapshot = node_snapshot(session, node_name)  // read-only: markup as it is now
    val line_doc = Line.Document(snapshot.node.source)
    offset_at(line_doc, line, col) match {
      case None => Json_Reports.line_query_not_found(s"no position at line $line col $col")
      case Some(off) =>
        // Isabelle 2026: Rendering is abstract; instantiate an anonymous
        // subclass with the upstream-default gui_style (as Find_Facts does).
        val rendering = new Rendering(snapshot, session.resources.options, session) {
          override def gui_style: GUI.Style = GUI.Style_Symbol_Decoded
        }
        rendering.tooltips(Rendering.tooltip_elements, Text.Range(off, off + 1)) match {
          case Some(info) =>
            JSON.Format(JSON.Object(
              "found" -> true,
              "range" -> range_fields(line_doc.range(info.range)),
              "contents" -> info.info.map(e => Pretty.string_of(List(e)))))
          case None =>
            Json_Reports.line_query_not_found(s"no hover info at line $line col $col")
        }
    }
  }

  private val hyperlink_elements: Markup.Elements =
    Markup.Elements(Markup.ENTITY, Markup.PATH, Markup.POSITION)

  /** A file target's position as 1-based line/col: when the entity carries a
   *  symbol range, decode it against the target file's text (jEdit's approach);
   *  otherwise fall back to the entity's 1-based line. */
  private def file_target_json(name: String, line: Int, range: Symbol.Range): JSON.T = {
    val file = expand_isabelle_home(name)
    val base: JSON.Object.T = JSON.Object("kind" -> "file", "file" -> file)
    if (range.start > 0) {
      try {
        val text = File.read(Path.explode(file))
        val r = Line.Document(text).range(Symbol.Text_Chunk(text).decode(range))
        base ++ JSON.Object("line" -> r.start.line1, "col" -> r.start.column1,
          "end_line" -> r.stop.line1, "end_col" -> r.stop.column1)
      } catch { case _: Exception => base + ("line" -> line) }
    } else base + ("line" -> line)
  }

  /** Resolve a command-id target to its node's line range. */
  private def id_target_json(snapshot: Document.Snapshot, id: Long, range: Symbol.Range): JSON.T =
    (for {
      start <- snapshot.find_command_position(id, range.start)
      stop <- snapshot.find_command_position(id, range.stop)
    } yield JSON.Object(
      "kind" -> "node",
      "node" -> start.name,
      "start_line" -> start.pos.line1,
      "end_line" -> stop.pos.line1): JSON.T)
      .getOrElse(JSON.Object("kind" -> "command_id", "id" -> id.toString))

  /** Go-to-definition at a 1-based line/col: {found, targets: [...]}. Heap/source
   *  entities resolve to {kind:"file", file, line, col, end_line, end_col}; entities
   *  defined in the DRAFT entry node itself resolve to {kind:"node", node,
   *  start_line, end_line} (the client maps node lines back to its buffer). */
  def definition_at_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int,
      col: Int
  ): String = {
    val snapshot = node_snapshot(session, node_name)  // read-only: markup as it is now
    val line_doc = Line.Document(snapshot.node.source)
    offset_at(line_doc, line, col) match {
      case None => Json_Reports.line_query_not_found(s"no position at line $line col $col")
      case Some(off) =>
        val links = snapshot.cumulate[List[JSON.T]](
          Text.Range(off, off + 1), Nil, hyperlink_elements, _ =>
            {
              case (acc, Text.Info(_, XML.Elem(Markup.Path(name), _))) =>
                Some(JSON.Object("kind" -> "path", "file" -> expand_isabelle_home(name)) :: acc)
              case (acc, Text.Info(_, XML.Elem(Markup(Markup.ENTITY, props), _))) =>
                val target: Option[JSON.T] = props match {
                  case Position.Item_Def_File(name, l, range) =>
                    Some(file_target_json(name, l, range))
                  case Position.Item_Def_Id(id, range) =>
                    Some(id_target_json(snapshot, id, range))
                  case _ => None
                }
                target.map(_ :: acc)
              case (acc, Text.Info(_, XML.Elem(Markup(Markup.POSITION, props), _))) =>
                val target: Option[JSON.T] = props match {
                  case Position.Item_File(name, l, range) =>
                    Some(file_target_json(name, l, range))
                  case Position.Item_Id(id, range) =>
                    Some(id_target_json(snapshot, id, range))
                  case _ => None
                }
                target.map(_ :: acc)
              case _ => None
            })
        val targets = links match {
          case Text.Info(_, l) :: _ => l.reverse
          case _                    => Nil
        }
        if (targets.isEmpty)
          Json_Reports.line_query_not_found(s"no definition at line $line col $col")
        else JSON.Format(JSON.Object("found" -> true, "targets" -> targets))
    }
  }

  // -----------------------------------------------------------------------
  // Overlay query machinery (style-4): attach a registered print function to an
  // EXISTING host command, poll for the instance's finished status marker,
  // collect instance-tagged results, remove the overlay. No text edits — the
  // document, rollback chain, and history are untouched. Used by the LSP-like
  // sledgehammer_at (host = command at a line) AND, since 2026-09-30, by every
  // "current state" query of Backend_Probes (host = current_state_host).
  // -----------------------------------------------------------------------

  /** Host command for a "current state" overlay query: the node's LAST non-ignored
   *  command — its result state is the current toplevel state, exactly what a
   *  command appended at the tip would see. With `skip_end` a trailing theory `end`
   *  is skipped, so the query observes the state the theory closed with (after `end`
   *  there is no theory context at all). None when the node has no command. Uses a
   *  fresh stable snapshot WITHOUT waiting for consolidation: the overlay's own
   *  deadline loop bounds the wait for a still-running host. */
  def current_state_host(
      session: Headless.Session,
      node_name: Document.Node.Name,
      skip_end: Boolean
  ): Option[Command] = {
    val snapshot = node_snapshot(session, node_name)
    snapshot.node.commands.reverse.iterator
      .find(command => !command.is_ignored && !(skip_end && command.span.name == "end"))
  }

  /** Result of an overlay query: (finished-in-budget, content lines, error lines).
   *  Each `writeln_result` on the ML side is one content line, in call order. */
  def overlay_query(
      session: Headless.Session,
      node_name: Document.Node.Name,
      host: Command,
      print_fn: String,
      args: List[String],
      budget_ms: Long
  ): (Boolean, List[String], List[String]) = {
    val instance = Document_ID.make().toString
    val overlays = Document.Node.Overlays.empty.insert(host, print_fn, instance :: args)
    session.update(Document.Blobs.empty, List(
      node_name -> Document.Node.Perspective[Text.Edit, Text.Perspective](
        true, Text.Perspective.full, overlays)))
    try {
      val deadline = System.currentTimeMillis() + budget_ms
      var done = false
      while (!done && System.currentTimeMillis() < deadline) {
        session.output_delay.sleep()
        val snap = session.await_stable_snapshot().switch(node_name)
        done = snap.command_results(host).iterator.toList.exists {
          case (_, XML.Elem(Markup(Markup.RESULT, props), List(XML.Elem(m, _))))
            if Markup.Instance.unapply(props).contains(instance) => m.name == Markup.FINISHED
          case _ => false
        }
      }
      val snap = session.await_stable_snapshot().switch(node_name)
      var content = List.empty[String]
      var errors = List.empty[String]
      for (case (_, XML.Elem(Markup(Markup.RESULT, props), body)) <- snap.command_results(host).iterator.toList
           if Markup.Instance.unapply(props).contains(instance)) {
        body match {
          // status markers (running/finished): no content
          case List(XML.Elem(m, _)) if m.name == Markup.RUNNING || m.name == Markup.FINISHED => ()
          case List(XML.Elem(m, b)) if m.name == Markup.ERROR =>
            errors = errors :+ Pretty.string_of(b)
          case _ =>
            val text = Pretty.string_of(body)
            if (text.nonEmpty) content = content :+ text
        }
      }
      (done, content, errors)
    } finally {
      // ALWAYS remove the overlay (restore the node's original empty perspective)
      session.update(Document.Blobs.empty, List(
        node_name -> Document.Node.Perspective[Text.Edit, Text.Perspective](
          true, Text.Perspective.empty, Document.Node.Overlays.empty)))
    }
  }

  /** Sledgehammer at a 1-based line via the `isabelle_pool_server_sledgehammer` overlay op
   *  (REPL.ML). Host = the command containing the line (jEdit cursor semantics);
   *  the op normalizes Forward-mode proof states via Proof.enter_backward, so any
   *  in-proof position works. Clean {found:false, error} when the line has no open
   *  goal. Consumed via POST .../sledgehammer_at by both MCPs. */
  def sledgehammer_at_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      line: Int,
      subgoal: Int,
      timeout_s: Int
  ): String =
    command_containing_line(session, node_name, line) match {
      case None => Json_Reports.line_query_not_found(s"no command at line $line")
      case Some((_, _, command, _)) =>
        val (done, content, errors) = overlay_query(
          session, node_name, command,
          "isabelle_pool_server_sledgehammer_query",
          List(timeout_s.toString, subgoal.toString),
          (timeout_s + 30).toLong * 1000L)
        if (!done)
          JSON.Format(JSON.Object(
            "found" -> false,
            "error" -> s"sledgehammer timed out (budget ${timeout_s}s + grace)"))
        else if (errors.nonEmpty)
          JSON.Format(JSON.Object(
            "found" -> false,
            "error" -> (if (errors.head.contains("Unknown proof context"))
                          s"no open goal at line $line"
                        else errors.head)))
        else
          JSON.Format(JSON.Object("found" -> true, "results" -> content))
    }

  /** Sledgehammer for the server-side `fill_sorries` job: hosted on the command with
   *  the given INDEX in `command_iterator()` order — the same numbering the chunk
   *  report emits as `i` — so the goal BEFORE a `sorry` can be targeted even when
   *  `lemma … sorry` share one line (a line-based lookup resolves to the `sorry`).
   *  Runs the `isabelle_pool_server_sledgehammer_fill` op (max_proofs, provers, and a
   *  leading "subgoals=N" line). JSON: {found, subgoals, results:[str]} or
   *  {found:false, subgoals, error}. */
  def sledgehammer_at_command_json(
      session: Headless.Session,
      node_name: Document.Node.Name,
      index: Int,
      subgoal: Int,
      timeout_s: Int,
      max_proofs: Int,
      provers: String
  ): String = {
    val snapshot = node_snapshot(session, node_name)
    val commands = snapshot.node.command_iterator().toList
    if (index < 0 || index >= commands.length)
      Json_Reports.line_query_not_found(s"no command with index $index")
    else {
      val (command, _) = commands(index)
      if (command.is_ignored)
        JSON.Format(JSON.Object("found" -> false, "subgoals" -> 0,
          "error" -> s"command $index is not a proper command"))
      else {
        val (done, content, errors) = overlay_query(
          session, node_name, command,
          "isabelle_pool_server_sledgehammer_fill_query",
          List(timeout_s.toString, subgoal.toString, max_proofs.toString, provers),
          (timeout_s + 30).toLong * 1000L)
        val (subgoals, results) = content match {
          case head :: rest if head.startsWith("subgoals=") =>
            (head.stripPrefix("subgoals=").filter(_.isDigit).toIntOption.getOrElse(0), rest)  // explicit nulls: no trim
          case other => (-1, other)  // header missing: the op did not get to run
        }
        val error_line = results.collectFirst { case l if l.startsWith("error=") => l.stripPrefix("error=") }
        if (!done)
          JSON.Format(JSON.Object("found" -> false, "subgoals" -> subgoals,
            "error" -> s"sledgehammer timed out (budget ${timeout_s}s + grace)"))
        else if (errors.nonEmpty)
          JSON.Format(JSON.Object("found" -> false, "subgoals" -> subgoals,
            "error" -> (if (errors.head.contains("Unknown proof context"))
                          s"no open goal at command $index"
                        else errors.head)))
        else if (error_line.isDefined)
          JSON.Format(JSON.Object("found" -> false, "subgoals" -> subgoals, "error" -> error_line.get))
        else
          JSON.Format(JSON.Object("found" -> true, "subgoals" -> subgoals, "results" -> results))
      }
    }
  }

  def node_source(session: Headless.Session, node_name: Document.Node.Name): String =
    node_snapshot(session, node_name).node.source
}
