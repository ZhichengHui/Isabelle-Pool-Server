package repl

import scala.collection.mutable

import isabelle._

/** Candidate proofs tried in the file's own session (isabelle_multi_attempt, efficiency
 *  plan stage C). Each candidate runs as an overlay query (`isabelle_pool_server_attempt`,
 *  Attempt.ML) on the HOST: the last command that ends before the attempt's `line`. Its
 *  result state is the state the candidate continues from, so neither a new session nor
 *  a re-check of the file before `line` is needed.
 *
 *  - at most `max_parallel` overlays at once, the others queued (print functions of
 *    pri 0 run in parallel: Command.parallel_print);
 *  - the ML side bounds each candidate by `timeout_s` (Timeout.apply); an overlay still
 *    unfinished `timeout_s` + GRACE_MS after it was attached is removed, which cancels it;
 *  - `cancel_attempts(reason)` removes every overlay of the running attempt at once; it
 *    may be called from any thread (not only the session's worker thread);
 *  - the file text before `line` (the prefix) is judged from the session's snapshot at
 *    the end: commands that failed or still run, executed sorry/oops lines.
 *
 *  Fallbacks (the caller then checks the candidate on a scratch session, as before):
 *  `multiline_command` (`line` is inside a command), `no_host` (no command ends before
 *  `line`), `host_end` (the host is `end`), and per candidate `candidate_command` (it
 *  contains `end`, `theory` or a command that loads files). */
object Backend_Attempts {
  val OP = "isabelle_pool_server_attempt_query"
  val GRACE_MS = 15000L

  def unescape(s: String): String = {
    val b = new StringBuilder
    var i = 0
    while (i < s.length) {
      val c = s.charAt(i)
      if (c == '\\' && i + 1 < s.length) {
        s.charAt(i + 1) match {
          case 'n' => b += '\n'
          case 't' => b += '\t'
          case other => b += other
        }
        i += 2
      }
      else { b += c; i += 1 }
    }
    b.toString
  }

  /** (start line, end line of the last proper token), both 1-based, of every proper
   *  command of the node, with the command. Trailing white space and comments do not
   *  count: a command ends where its text ends. */
  def command_lines(snapshot: Document.Snapshot): List[(Command, Int, Int)] = {
    val line_doc = Line.Document(snapshot.node.source)
    snapshot.node.command_iterator().toList.flatMap { case (command, start) =>
      if (command.is_ignored) None
      else {
        val toks = command.span.content
        val last = toks.lastIndexWhere(_.is_proper)
        val len = if (last < 0) command.length else toks.take(last + 1).map(_.source.length).sum
        val start_line = line_doc.position(start).line1
        val end_line = line_doc.position(start + (len max 1) - 1).line1
        Some((command, start_line, end_line))
      }
    }
  }

  /** The candidate's first command that the session cannot run as an overlay, if any. */
  def unsupported_command(syntax: Outer_Syntax, candidate: String): Option[String] =
    syntax.parse_spans(candidate).iterator.map(_.name).find { name =>
      name == "end" || name == "theory" ||
        syntax.keywords.kinds.get(name).contains(Keyword.THY_LOAD)
    }
}

trait Backend_Attempts { this: ReplBackend =>
  import Backend_Attempts._

  @volatile private var current_run: Option[Attempt_Run] = None

  /** Try the candidates (`candidates_json`: a JSON array of strings; Py4J passes no
   *  Python lists) at 1-based `line` of the current document; JSON (see Attempt_Run.run). */
  def attempt(line: Int, candidates_json: String, timeout_s: Int, max_parallel: Int): String = {
    Repl_Output.reset()
    val candidates = JSON.parse(candidates_json) match {
      case list: List[?] => list.map(_.toString)
      case _ => error("attempt: candidates must be a JSON array of strings")
    }
    repl_session.with_current_node { (session, node) =>
      val run = new Attempt_Run(session, repl_session.overlays, node, line,
        candidates, timeout_s, max_parallel max 1)
      current_run = Some(run)
      try run.run() finally current_run = None
    }.getOrElse(JSON.Format(JSON.Object("fallback" -> "no_host", "candidates" -> Nil)))
  }

  /** Cancel the running attempt, if any: every candidate not yet finished is removed
   *  (its overlay withdrawn, which interrupts its ML) and reported with `cancelled` =
   *  `reason`. Callable from any thread. Returns the number of candidates cancelled. */
  def cancel_attempts(reason: String): Int = current_run.map(_.cancel(reason)).getOrElse(0)
}

/** One call of Backend_Attempts.attempt. */
final class Attempt_Run(
  session: Headless.Session,
  registry: Overlay_Registry,
  node: Document.Node.Name,
  line: Int,
  candidates: List[String],
  timeout_s: Int,
  max_parallel: Int
) {
  import Backend_Attempts._

  private val n = candidates.length
  private val instances = Vector.fill(n)(Document_ID.make().toString)
  private val results = new Array[JSON.T](n)
  private val started = new Array[Long](n)  // 0 = not attached
  @volatile private var cancelled: Option[String] = None
  private val budget_ms = timeout_s.toLong * 1000L + GRACE_MS

  private def snapshot(): Document.Snapshot = session.await_stable_snapshot().switch(node)

  def cancel(reason: String): Int = {
    cancelled = Some(reason)
    val open = (0 until n).filter(i => results(i) == null)
    registry.remove(session, open.map(instances(_)))
    open.length
  }

  /** The result of candidate `i` from its output lines. `stopped`: it did not finish
   *  (time limit + grace passed, or cancelled). */
  private def candidate_json(i: Int, content: List[String], errors: List[String], stopped: Boolean): JSON.T = {
    var commands = List.empty[JSON.T]
    var goals = List.empty[String]
    var running: Option[(Int, String)] = None
    var finished = false
    var timed_out = stopped
    var in_proof = false
    var elapsed_ms = if (started(i) > 0) System.currentTimeMillis() - started(i) else 0L
    for (text <- content) {
      val f = space_explode('\t', text)
      f match {
        case "start" :: l :: kind :: _ => running = Some((l.toIntOption.getOrElse(0), kind))
        case "cmd" :: l :: kind :: status :: sorry :: msg :: _ =>
          running = None
          commands ::= JSON.Object("line" -> l.toIntOption.getOrElse(0), "kind" -> kind,
            "status" -> status, "sorry" -> (sorry == "1"), "messages" -> unescape(msg))
        case "goal" :: g :: _ => goals ::= unescape(g)
        case "end" :: t :: p :: _ :: ms :: _ =>
          finished = true
          timed_out = t == "1"
          in_proof = p == "1"
          elapsed_ms = ms.toLongOption.getOrElse(elapsed_ms)
        case _ =>
      }
    }
    for ((l, kind) <- running if !finished)
      commands ::= JSON.Object("line" -> l, "kind" -> kind, "status" -> "running",
        "sorry" -> false, "messages" -> "")
    JSON.Object(
      "index" -> i,
      "finished" -> finished,
      "timed_out" -> timed_out,
      "in_proof" -> in_proof,
      "goals" -> goals.reverse,
      "commands" -> commands.reverse,
      "elapsed_ms" -> elapsed_ms,
      "cancelled" -> cancelled.orNull,
      "error" -> errors.headOption.orNull)
  }

  private def fallback_json(reason: String): String =
    JSON.Format(JSON.Object("fallback" -> reason, "candidates" -> Nil))

  def run(): String = {
    val t0 = System.currentTimeMillis()
    val snap0 = snapshot()
    val cmds = command_lines(snap0)
    val straddles = cmds.exists { case (_, s, e) => s < line && e >= line }
    val before = cmds.filter { case (_, s, _) => s < line }
    if (straddles) return fallback_json("multiline_command")
    val host = before.lastOption.map(_._1) match {
      case None => return fallback_json("no_host")
      case Some(h) if h.span.name == "end" => return fallback_json("host_end")
      case Some(h) => h
    }
    val syntax = session.recent_syntax(node)
    val unsupported = candidates.map(unsupported_command(syntax, _))
    for (i <- 0 until n; name <- unsupported(i))
      results(i) = JSON.Object("index" -> i, "fallback" -> "candidate_command", "command" -> name)

    val queue = mutable.Queue.from((0 until n).filter(i => results(i) == null))
    val active = mutable.LinkedHashSet.empty[Int]
    try {
      while ((queue.nonEmpty || active.nonEmpty) && cancelled.isEmpty) {
        val adds = mutable.ListBuffer.empty[Int]
        while (queue.nonEmpty && active.size + adds.size < max_parallel) adds += queue.dequeue()
        if (adds.nonEmpty) {
          val now = System.currentTimeMillis()
          adds.foreach(started(_) = now)
          registry.add(session, node,
            adds.toList.map(i => (instances(i), host, OP, List(timeout_s.toString, candidates(i)))))
          active ++= adds
        }
        session.output_delay.sleep()
        val snap = snapshot()
        val now = System.currentTimeMillis()
        val done = active.toList.filter { i =>
          val (fin, content, errors) = Overlay_Queries.instance_results(snap, host, instances(i))
          if (fin || now - started(i) > budget_ms) {
            results(i) = candidate_json(i, content, errors, stopped = !fin)
            true
          }
          else false
        }
        if (done.nonEmpty) {
          registry.remove(session, done.map(instances(_)))
          active --= done
        }
      }
      if (cancelled.isDefined) {
        val snap = snapshot()
        for (i <- 0 until n if results(i) == null) {
          val (_, content, errors) =
            if (started(i) > 0) Overlay_Queries.instance_results(snap, host, instances(i))
            else (false, Nil, Nil)
          results(i) = candidate_json(i, content, errors, stopped = true)
        }
      }
    }
    finally { registry.remove(session, instances) }

    // the prefix: wait (within the budget) until its commands are settled, then judge them
    val prefix_deadline = t0 + budget_ms
    def prefix_status(snap: Document.Snapshot): List[(Command, Int, String)] = {
      val state = session.get_state()
      before.map { case (command, start_line, _) =>
        val status = scala.util.Try(state.command_status(snap.version, command)).toOption match {
          case Some(s) if s.is_failed          => "failed"
          case Some(s) if s.maybe_consolidated => "ok"
          case Some(s) if s.is_running         => "running"
          case Some(_)                         => "unprocessed"
          case None                            => "ok"  // PIDE advanced past this version
        }
        (command, start_line, status)
      }
    }
    var snap = snapshot()
    var prefix = prefix_status(snap)
    while (prefix.exists(p => p._3 == "running" || p._3 == "unprocessed") &&
           System.currentTimeMillis() < prefix_deadline && cancelled.isEmpty) {
      session.output_delay.sleep()
      snap = snapshot()
      prefix = prefix_status(snap)
    }
    def messages(command: Command): List[JSON.T] =
      snap.command_results(command).iterator.toList.flatMap {
        case (_, XML.Elem(Markup(kind, _), body))
            if kind == Markup.ERROR_MESSAGE || kind == Markup.WARNING_MESSAGE =>
          val text = Pretty.string_of(body)
          if (text.isEmpty) None
          else Some(JSON.Object("sev" -> (if (kind == Markup.ERROR_MESSAGE) "error" else "warning"),
            "text" -> text))
        case _ => None
      }
    val prefix_bad = prefix.collect {
      case (command, l, status) if status == "failed" || status == "running" =>
        JSON.Object("node_line" -> l, "kind" -> command.span.name, "status" -> status,
          "messages" -> messages(command))
    }
    val sorry_lines = prefix.collect {
      case (command, l, "ok") if command.span.name == "sorry" || command.span.name == "oops" => l
    }
    JSON.Format(JSON.Object(
      "fallback" -> null,
      "host_line" -> before.last._2,
      "prefix" -> JSON.Object(
        "all_ok" -> prefix.forall(_._3 == "ok"),
        "settled" -> prefix.forall(p => p._3 == "ok" || p._3 == "failed"),
        "failed" -> prefix_bad,
        "sorry_lines" -> sorry_lines),
      "cancelled" -> cancelled.orNull,
      "candidates" -> results.toList))
  }
}
