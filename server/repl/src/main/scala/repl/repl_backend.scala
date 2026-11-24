package repl

import isabelle._

/** Py4J entrypoint facade: one `ReplBackend` instance per HTTP-server session, all
 *  sharing ONE gateway JVM (see repl_backend_gateway.scala). Python addresses this
 *  exact class via Py4J and the `ReplBackend` Protocol in
 *  repl/src/python/repl_backend_gateway.py — the public method set and signatures
 *  ARE the wire contract and must stay in sync with that Protocol.
 *
 *  The class itself only holds the constructor state and the shared result
 *  plumbing (`build_result`). The public surface is split into one trait per
 *  consuming workflow, each in its own file:
 *    - Backend_Lifecycle (backend_lifecycle.scala) — session lifecycle and cache.
 *    - Backend_Probes    (backend_probes.scala)    — read-only state queries (BOTH MCPs):
 *                                                   overlay queries, no document edits.
 *    - Backend_Chunk_Ops (backend_chunk_ops.scala) — chunk-centric execution surface.
 *    - Backend_File_Ops  (backend_file_ops.scala)  — LSP-like file-sync surface.
 *    - Backend_Attempts  (backend_attempts.scala)  — candidate proofs as overlays in the file session.
 *
 *  All backends share the gateway JVM but each owns its own Isabelle session
 *  (own poly process, own PIDE document), so no per-backend routing of ML
 *  replies is needed: query results are read from that document's own
 *  command_results (see Backend_Probes). */
class ReplBackend(show_states: Boolean, enable_cache: Boolean = false, max_cache_size: Int = 10, protected val initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), session_manager: Option[Session_Manager] = None, protected val field: String = "HOL", protected val session_dirs: List[String] = Nil)
    extends Backend_Lifecycle
    with Backend_Probes
    with Backend_Chunk_Ops
    with Backend_File_Ops
    with Backend_Attempts {
  // protected (not private) so the mixed-in traits can reach them via the
  // self-type; still off the Py4J-callable public surface.
  protected val session_manager_instance = session_manager.getOrElse(new Session_Manager(show_states, enable_cache, max_cache_size))
  protected var repl_session = new Repl_Session(session_manager_instance, initial_thys, field, session_dirs)

  /** Reset the per-thread result buffer, run `command_logic`, and return the
   *  accumulated Repl_Result. The standard wrapper for output-producing methods. */
  def build_result[A](command_logic: => A): Repl_Result = {
    Repl_Output.reset()
    command_logic
    Repl_Output.result
  }
}
