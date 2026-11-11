package repl

import isabelle._
import py4j.GatewayServer
import scala.jdk.CollectionConverters._

/** JVM entrypoint of the shared gateway: `Gateway_App.main` starts the Py4J
 *  GatewayServer (launched by repl_backend_gateway.py via `isabelle scala`),
 *  and [[ReplBackendGateway]] is the factory object Python calls to obtain
 *  [[ReplBackend]] instances — one per HTTP-server session, all in this ONE
 *  JVM. Shared infrastructure serving both MCP workflows. */
object ReplBackendGateway {
  /** Functional liveness probe for the "Timer already cancelled" wedge
   *  (docs/ISSUES.md Bug 9): schedule a no-op on the JVM-global Event_Timer.
   *  A wedged timer throws IllegalStateException at schedule time, so this
   *  answers false instead of 500ing — letting the Python side distinguish
   *  a wedged-but-alive JVM from a healthy one and trigger crash recovery. */
  def alive(): Boolean =
    fatal.isEmpty &&
    (try { Event_Timer.request(new Console_Logger(), isabelle.Time.now()) { () }; true }
     catch { case _: IllegalStateException => false })

  /* Fatal JVM errors.

     A thread that dies of an OutOfMemoryError leaves its session broken for good:
     when it is `Isabelle.Session.manager`, the session can neither work nor be
     stopped normally. On 2026-11-11 such sessions kept the heap full, so the JVM
     spent its CPU in GC and every new session start hung, while the liveness probe
     still answered true. Once a thread has died of a VirtualMachineError (or a
     session start failed with one), `alive` answers false, and the Python side
     replaces the gateway (`_ensure_gateway` / the cleanup sweep). */

  @volatile private var fatal: Option[String] = None

  /** Why the gateway counts as dead ("" while it does not). */
  def fatal_error(): String = fatal.getOrElse("")

  def note_fatal(where: String, exn: Throwable): Unit =
    if (fatal.isEmpty && fatal_on_vm_error) {
      fatal = Some(s"$where: ${exn.getClass.getName}: ${exn.getMessage}")
      Output.error_message(s"Gateway marked dead: ${fatal.get}")
    }

  /** ISABELLE_GATEWAY_FATAL_ON_VM_ERROR=false keeps the old behaviour (diagnostics). */
  private val fatal_on_vm_error: Boolean =
    !sys.env.get("ISABELLE_GATEWAY_FATAL_ON_VM_ERROR").map(_.trim.toLowerCase).exists(
      Set("0", "false", "no", "off"))

  /** Records a thread that died of a VirtualMachineError; prints every uncaught
   *  exception as the JVM default handler does. */
  val uncaught_handler: Thread.UncaughtExceptionHandler =
    (thread: Thread, exn: Throwable) => {
      exn match {
        case _: VirtualMachineError => note_fatal(s"thread \"${thread.getName}\" died", exn)
        case _ =>
      }
      System.err.print(s"Exception in thread \"${thread.getName}\" ")
      exn.printStackTrace(System.err)
    }

  def get_repl_backend(show_states: Boolean): ReplBackend = new ReplBackend(show_states)
  def get_repl_backend_with_cache(show_states: Boolean, enable_cache: Boolean): ReplBackend = 
    new ReplBackend(show_states, enable_cache)
    
  def get_repl_backend_with_full_cache_config(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, field: String = "HOL"): ReplBackend = 
    new ReplBackend(show_states, enable_cache, max_cache_size, field = field)

  def get_repl_backend_with_memory_management(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, field: String = "HOL"): ReplBackend =
    new ReplBackend(show_states, enable_cache, max_cache_size, field = field)

  def get_repl_backend_with_initial_theories(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, initial_thys: java.util.List[String], field: String = "HOL", session_dirs: java.util.List[String] = java.util.List.of[String]().nn): ReplBackend =
    make_backend(None, show_states, enable_cache, max_cache_size, initial_thys, field, session_dirs)

  /* Tagged backends and the reaper.

     The Python server tags every backend with its session id. The tag is
     registered BEFORE the backend starts its prover, so a backend whose creation
     the Python side gave up on (timeout, error) can still be found and stopped
     (close_backend), and the periodic reaper (reap_backends) stops every tagged
     backend that the Python registry no longer knows. Stopping = shutdown of the
     backend's Session_Manager: every Isabelle session of it is stopped, which
     terminates its prover process group. */

  private val registry_lock = new Object
  private var registry_seq = 0L
  private val registry = scala.collection.mutable.Map.empty[String, (Long, Session_Manager)]

  private def make_backend(tag: Option[String], show_states: Boolean, enable_cache: Boolean,
      max_cache_size: Int, initial_thys: java.util.List[String], field: String,
      session_dirs: java.util.List[String]): ReplBackend = {
    val manager = new Session_Manager(show_states, enable_cache, max_cache_size)
    tag.foreach { t =>
      val previous = registry_lock.synchronized {
        registry_seq += 1
        registry.put(t, (registry_seq, manager))
      }
      previous.foreach { case (_, m) => stop_manager_async(m) }
    }
    try {
      new ReplBackend(show_states, enable_cache, max_cache_size, initial_thys.asScala.toList,
        Some(manager), field, session_dirs.asScala.toList)
    } catch {
      case exn: Throwable =>
        tag.foreach(t => registry_lock.synchronized {
          if (registry.get(t).exists(_._2 eq manager)) registry.remove(t)
        })
        manager.shutdown()
        throw exn
    }
  }

  def get_repl_backend_tagged(tag: String, show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, initial_thys: java.util.List[String], field: String, session_dirs: java.util.List[String]): ReplBackend =
    make_backend(Some(tag), show_states, enable_cache, max_cache_size, initial_thys, field, session_dirs)

  private def stop_manager_async(manager: Session_Manager): Unit =
    Isabelle_Thread.fork(name = "reap_backend") {
      try manager.shutdown()
      catch {
        case e: Throwable => Output.error_message(s"Error reaping backend: ${Exn.message(e)}")
      }
    }

  /** Stop the backend tagged `tag` (also one still starting) in the background.
   *  False when no such backend is registered. */
  def close_backend(tag: String): Boolean =
    registry_lock.synchronized(registry.remove(tag)) match {
      case Some((_, manager)) => stop_manager_async(manager); true
      case None => false
    }

  /** The registration counter now: every backend registered so far has a number <= it. */
  def reap_fence(): Long = registry_lock.synchronized(registry_seq)

  /** Stop, in the background, every tagged backend registered at or before `fence`
   *  whose tag is not in `live`; forget backends that are already shut down.
   *  Returns the tags it stops. The caller takes `fence` BEFORE it reads its live
   *  set, so a backend registered after that read is never reaped by this call. */
  def reap_backends(live: java.util.List[String], fence: Long): java.util.List[String] = {
    val keep = live.asScala.toSet
    val reaped = registry_lock.synchronized {
      registry.filterInPlace { case (_, (_, m)) => !m.is_closed }
      val victims = registry.toList.collect {
        case (t, (seq, m)) if seq <= fence && !keep.contains(t) => (t, m)
      }
      victims.foreach { case (t, _) => registry.remove(t) }
      victims
    }
    reaped.foreach { case (_, m) => stop_manager_async(m) }
    reaped.map(_._1).asJava
  }

  /** Tags of the registered backends that are not shut down (diagnostics, tests). */
  def registered_backends(): java.util.List[String] =
    registry_lock.synchronized(registry.collect { case (t, (_, m)) if !m.is_closed => t }.toList).asJava

  private var shared_session_manager: Option[Session_Manager] = None

  def get_shared_session_manager(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int): Session_Manager = {
    if (shared_session_manager.isEmpty) {
      shared_session_manager = Some(new Session_Manager(show_states, enable_cache, max_cache_size))
    }
    shared_session_manager.get
  }

  def get_repl_backend_with_shared_cache(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, initial_thys: java.util.List[String], field: String = "HOL"): ReplBackend = {
    val session_manager = get_shared_session_manager(show_states, enable_cache, max_cache_size)
    new ReplBackend(show_states, enable_cache, max_cache_size, initial_thys.asScala.toList, Some(session_manager), field)
  }
  
  def clear_shared_session_manager(): Unit = {
    shared_session_manager.foreach(_.shutdown())
    shared_session_manager = None
  }
}

object Gateway_App {
  def main(args: Array[String]): Unit = {
    Thread.setDefaultUncaughtExceptionHandler(ReplBackendGateway.uncaught_handler)
    val gateway = new GatewayServer(ReplBackendGateway, 0)
    gateway.start()
    val port = gateway.getListeningPort
    Output.writeln(port.toString, stdout = true)
  }
}
