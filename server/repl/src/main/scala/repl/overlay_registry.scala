package repl

import isabelle._

/** The overlays of one session's document: every query that runs as an overlay (a
 *  registered Query_Operation attached to a host command, REPL.ML / Attempt.ML) joins
 *  and leaves on its own, and each change pushes the UNION of the current overlays to
 *  PIDE. (Until 2026-11-24 each query replaced the node's whole overlay set with its own
 *  and cleared it when done, so two overlay queries at once removed each other.)
 *
 *  A node with overlays gets the full perspective (print functions run only on visible
 *  commands), a node without them the empty one, as before. Thread-safe: the attempt
 *  loop runs on the session's worker thread, `remove` may come from another thread
 *  (Backend_Attempts.cancel_attempts). The lock is held across the push, so the PIDE
 *  updates go out in the order of the registry's changes. */
final class Overlay_Registry {
  private case class Entry(node: Document.Node.Name, host: Command, fn: String, args: List[String])
  private var entries: Map[String, Entry] = Map.empty  // instance -> entry

  /** Add overlays (instance, host, print function, args WITHOUT the instance). */
  def add(
      session: Headless.Session,
      node: Document.Node.Name,
      items: List[(String, Command, String, List[String])]
  ): Unit = synchronized {
    if (items.nonEmpty) {
      for ((instance, host, fn, args) <- items) entries += instance -> Entry(node, host, fn, args)
      push(session, node)
    }
  }

  /** Remove overlays by instance; unknown instances are ignored. True if any was removed. */
  def remove(session: Headless.Session, instances: Iterable[String]): Boolean = synchronized {
    val gone = instances.flatMap(i => entries.get(i).map(i -> _)).toList
    if (gone.isEmpty) false
    else {
      entries --= gone.map(_._1)
      gone.map(_._2.node).distinct.foreach(push(session, _))
      true
    }
  }

  def instances: Set[String] = synchronized { entries.keySet }

  /** Forget everything without pushing (the session the overlays belonged to is gone). */
  def clear(): Unit = synchronized { entries = Map.empty }

  private def push(session: Headless.Session, node: Document.Node.Name): Unit = {
    val overlays =
      entries.iterator.filter(_._2.node == node).foldLeft(Document.Node.Overlays.empty) {
        case (ovs, (instance, e)) => ovs.insert(e.host, e.fn, instance :: e.args)
      }
    val perspective = if (overlays.is_empty) Text.Perspective.empty else Text.Perspective.full
    session.update(Document.Blobs.empty, List(
      node -> Document.Node.Perspective[Text.Edit, Text.Perspective](true, perspective, overlays)))
  }
}

/** Overlay query machinery (moved here from Document_Utils 2026-11-24): attach a
 *  registered print function to an EXISTING host command, poll for the instance's
 *  finished status marker, collect instance-tagged results, remove the overlay. No text
 *  edits — the document, rollback chain, and history are untouched. Used by the LSP-like
 *  sledgehammer_at (host = command at a line), by every "current state" query of
 *  Backend_Probes (host = current_state_host), and, with several overlays at once, by
 *  Backend_Attempts. */
object Overlay_Queries {
  /** Instance-tagged results of one overlay instance on `host`, in output order:
   *  (finished marker seen, content lines, error lines). */
  def instance_results(
      snapshot: Document.Snapshot,
      host: Command,
      instance: String
  ): (Boolean, List[String], List[String]) = {
    var finished = false
    var content = List.empty[String]
    var errors = List.empty[String]
    for (case (_, XML.Elem(Markup(Markup.RESULT, props), body)) <- snapshot.command_results(host).iterator.toList
         if Markup.Instance.unapply(props).contains(instance)) {
      body match {
        case List(XML.Elem(m, _)) if m.name == Markup.FINISHED => finished = true
        case List(XML.Elem(m, _)) if m.name == Markup.RUNNING => ()
        case List(XML.Elem(m, b)) if m.name == Markup.ERROR => errors = errors :+ Pretty.string_of(b)
        case _ =>
          val text = Pretty.string_of(body)
          if (text.nonEmpty) content = content :+ text
      }
    }
    (finished, content, errors)
  }

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
    val snapshot = session.await_stable_snapshot().switch(node_name)
    snapshot.node.commands.reverse.iterator
      .find(command => !command.is_ignored && !(skip_end && command.span.name == "end"))
  }

  /** The node's first non-ignored command whose evaluation is over (finished or failed):
   *  a host for queries that do not read the state (the memory probe), so a long
   *  command still running at the end of the document does not delay them. */
  def first_finished_host(
      session: Headless.Session,
      node_name: Document.Node.Name
  ): Option[Command] = {
    val snapshot = session.await_stable_snapshot().switch(node_name)
    val state = session.get_state()
    snapshot.node.commands.iterator.find { command =>
      !command.is_ignored &&
        scala.util.Try(state.command_status(snapshot.version, command))
          .toOption.exists(st => st.is_finished || st.is_failed)
    }
  }

  /** Run one overlay query: (finished-in-budget, content lines, error lines). Each
   *  `writeln_result` on the ML side is one content line, in call order. The overlay
   *  is always removed (which cancels the print task if it is still running). */
  def overlay_query(
      session: Headless.Session,
      registry: Overlay_Registry,
      node_name: Document.Node.Name,
      host: Command,
      print_fn: String,
      args: List[String],
      budget_ms: Long
  ): (Boolean, List[String], List[String]) = {
    val instance = Document_ID.make().toString
    registry.add(session, node_name, List((instance, host, print_fn, args)))
    try {
      val deadline = System.currentTimeMillis() + budget_ms
      var done = false
      while (!done && System.currentTimeMillis() < deadline) {
        session.output_delay.sleep()
        done = instance_results(session.await_stable_snapshot().switch(node_name), host, instance)._1
      }
      val (_, content, errors) =
        instance_results(session.await_stable_snapshot().switch(node_name), host, instance)
      (done, content, errors)
    }
    finally { registry.remove(session, List(instance)) }
  }
}
