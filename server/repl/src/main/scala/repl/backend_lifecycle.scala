package repl

import scala.jdk.CollectionConverters._

/** Session lifecycle surface of [[ReplBackend]]: theory entry, session reset /
 *  teardown / health, and session-cache introspection. Consumed by the HTTP
 *  server's enter_theory, document (PUT — reset + re-enter + one edit), and
 *  session-close paths; the cache/health methods back stats and the gateway
 *  crash-recovery flow. Shared infrastructure for BOTH MCP workflows. */
trait Backend_Lifecycle { this: ReplBackend =>

  /** Name of the currently entered theory ("" if none). */
  def current_thy_name_string: String = repl_session.current_thy_name_string

  /** Human-readable session-cache status line; also used internally by `reset`
   *  to pick the cache vs. no-cache teardown path. */
  def get_cache_status(): String = session_manager_instance.get_cache_status()

  /** Session-cache counters (hits / misses / creates / evictions) as a Java map. */
  def get_cache_stats(): java.util.Map[String, Int] = {
    val stats = session_manager_instance.get_cache_stats()
    stats.asJava
  }

  /** Enter (creating if needed) a theory by name; consumed via the HTTP server's
   *  enter_theory endpoint by both MCP workflows. */
  def enter_thy(input_thy_name: String): Repl_Result = build_result {
    Thy_Parsing.extract_thy_name(input_thy_name) match {
      case None           => Repl_Output.add_error(s"Invalid theory name: $input_thy_name")
      case Some(thy_name) => repl_session.enter_thy(thy_name)
    }
  }

  /** Tear down the current Isabelle session and start a fresh one (through the
   *  cache when enabled), PRESERVING this backend's initial theories, field, and
   *  session dirs — load_document resets before every whole-document replace, so
   *  a heap-backed session must keep its heap (field + dirs) across resets.
   *  Used by the whole-document replace primitive (PUT .../document) and by
   *  session recovery. */
  def reset(): Repl_Result = build_result {
    forget_ml_pid()  // the process the memory probe saw is about to stop
    if (session_manager_instance.get_cache_status().contains("Enabled: true")) {
      // with cache
      repl_session.stop_with_cache()
      repl_session = new Repl_Session(session_manager_instance, initial_thys, field, session_dirs)
    } else {
      // cache disabled
      repl_session.stop()
      repl_session = new Repl_Session(session_manager_instance, initial_thys, field, session_dirs)
    }
  }

  /** Shut down this backend: stop the session manager (and with it this
   *  backend's Isabelle session and its prover process group). Called on
   *  session close. Unlike every other backend call it is NOT serialized
   *  through the session's worker thread (ThreadedBackend.close runs it on its
   *  own thread): the worker may be blocked in a call that outlived its
   *  request, and Session_Manager.shutdown is idempotent and thread-safe. */
  def exit(): Unit =
    session_manager_instance.shutdown()

  // validate session
  /** Liveness check: true if the underlying Isabelle session still responds. */
  def is_session_valid(): Boolean = {
    try {

      repl_session.current_thy_name_string
      true
    } catch {
      case _: Throwable => false
    }
  }

  /** Recreate the Isabelle session (same initial theories, field, dirs) if
   *  `is_session_valid` is false — the gateway crash-recovery path. */
  def recreate_session_if_needed(): Unit = {
    if (!is_session_valid()) {
      println("Invalid session found, recreating...")
      repl_session = new Repl_Session(session_manager_instance, initial_thys, field, session_dirs)
    }
  }
}
