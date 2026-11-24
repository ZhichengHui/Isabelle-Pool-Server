package repl

import isabelle._

/** Memory guard support (efficiency plan stage D2): the ML heap of a session read
 *  WITHOUT the prover's worker farm, and the cancellation of the file's running
 *  commands. Both may be called from any thread; neither waits for the session's
 *  worker queue on the Python side nor for a worker thread of the prover.
 *
 *  The heap figures come from Isabelle's own runtime statistics: every PIDE session
 *  starts a monitor process (ML_Statistics.Handler, a service of the distribution)
 *  that reads the prover's statistics from outside (`--exportstats`,
 *  PolyML.Statistics.getRemoteStats) every 0.5 s and posts them on
 *  `session.runtime_statistics`. [[Heap_Watch]] keeps the latest post of each
 *  session. The overlay probe (`mem_stats`, Backend_Probes) needs a worker of the
 *  prover, which candidates or a long command may occupy. */
object Heap_Watch {
  private val latest = new java.util.WeakHashMap[Session, (Long, Properties.T)]

  /** Subscribe to the session's runtime statistics (once, at session creation). */
  def attach(session: Session): Unit =
    session.runtime_statistics += Session.Consumer[Session.Runtime_Statistics]("ips_heap_watch") {
      stats => latest.synchronized { latest.put(session, (System.currentTimeMillis(), stats.props)) }
    }

  def get(session: Session): Option[(Long, Properties.T)] =
    latest.synchronized { Option(latest.get(session)).map(_.nn) }

  val KEYS: List[String] =
    List("size_heap", "size_heap_free_last_GC", "size_heap_free_last_full_GC",
      "full_GCs", "partial_GCs", "now")
}

trait Backend_Memory { this: ReplBackend =>

  /** The latest runtime statistics of this session's ML process, one `key=value`
   *  line each (Heap_Watch.KEYS, in bytes / counts) plus `age_ms` (how long ago the
   *  monitor posted them). Empty when nothing was posted yet. */
  def ml_heap(): String =
    Heap_Watch.get(repl_session.pide_session) match {
      case None => ""
      case Some((at, props)) =>
        val lines = Heap_Watch.KEYS.flatMap(k => Properties.get(props, k).map(v => k + "=" + v))
        (("age_ms=" + (System.currentTimeMillis() - at)) :: lines).mkString("\n")
    }

  /** Cancel every command of the current node that is running now
   *  (`Session.cancel_exec` on each of its execs; PIDE then marks it failed with an
   *  interrupt). JSON array of `{line, kind}` (1-based start line) of the commands
   *  cancelled; `[]` when none runs or no theory is entered. */
  def cancel_running_commands(): String =
    repl_session.with_current_node { (session, node) =>
      val snapshot = session.snapshot(node)
      val state = session.get_state()
      val assignment = scala.util.Try(state.the_assignment(snapshot.version)).toOption
      val running = Backend_Attempts.command_lines(snapshot).filter { case (command, _, _) =>
        scala.util.Try(state.command_status(snapshot.version, command)).toOption
          .exists(s => s.is_running && !s.is_failed)
      }
      val cancelled = running.flatMap { case (command, start_line, _) =>
        val execs = assignment.map(_.get(command.id)).getOrElse(Nil)
        execs.foreach(session.cancel_exec)
        if (execs.isEmpty) None
        else Some(JSON.Object("line" -> start_line, "kind" -> command.span.name))
      }
      JSON.Format(cancelled)
    }.getOrElse("[]")
}
