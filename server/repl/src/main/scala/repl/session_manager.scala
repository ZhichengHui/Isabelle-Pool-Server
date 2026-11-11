package repl

import isabelle._

import scala.collection.mutable

/** Owns the Isabelle server process and the headless sessions created from it:
 *  session start/stop, an optional LRU session cache (with refcounting and
 *  eviction), and per-session PIDE options (parallel_proofs, capped threads —
 *  read from the gateway JVM's environment at session creation). Shared
 *  infrastructure underneath every [[Repl_Session]]; consumed indirectly by
 *  both MCP workflows. */
case class Session_Data(id: UUID.T, session: Headless.Session)

class Session_Manager(show_states: Boolean, enable_cache: Boolean = false, max_cache_size: Int = 10) {
  private val (server_info, server) = Server_Utils.start_server()
  private val running_sessions: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)
  private val pending_removals: Synchronized[mutable.Set[Future[Unit]]] =
    Synchronized(mutable.Set.empty)

  private val session_cache: Synchronized[mutable.Map[List[String], mutable.Queue[Session_Data]]] =
    Synchronized(mutable.Map.empty)
  
  private val lru_order: Synchronized[mutable.LinkedHashMap[List[String], Long]] = 
    Synchronized(mutable.LinkedHashMap.empty)
  
  private val cache_stats: Synchronized[mutable.Map[String, Int]] =
    Synchronized(mutable.Map("hits" -> 0, "misses" -> 0, "creates" -> 0, "evictions" -> 0))
  
  private val session_refs: Synchronized[mutable.Map[UUID.T, Int]] =
    Synchronized(mutable.Map.empty)
  
  private val evicted_sessions: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)

  def get_cache_stats(): Map[String, Int] = cache_stats.value.toMap

  def get_cache_status(): String = {
    val stats = cache_stats.value
    val cache_size = session_cache.value.values.map(_.size).sum
    s"Cache: ${cache_size} sessions, Hits: ${stats("hits")}, Misses: ${stats("misses")}, Creates: ${stats("creates")}, Evictions: ${stats("evictions")}, Enabled: ${enable_cache}, MaxSize: ${max_cache_size}"
  }

  private def increment_session_ref(session_id: UUID.T): Unit = {
    session_refs.change(refs => {
      refs.update(session_id, refs.getOrElse(session_id, 0) + 1)
      refs
    })
  }
  
  private def decrement_session_ref(session_id: UUID.T): Unit = {
    session_refs.change(refs => {
      val current_refs = refs.getOrElse(session_id, 0)
      if (current_refs > 1) {
        refs.update(session_id, current_refs - 1)
      } else {
        refs.remove(session_id)
      }
      refs
    })
  }
  
  def is_session_evicted(session_id: UUID.T): Boolean = {
    evicted_sessions.value.contains(session_id)
  }
  
  private def mark_session_evicted(session_id: UUID.T): Unit = {
    evicted_sessions.change(evicted => {
      evicted += session_id
      evicted
    })
  }


  private def get_cache_key(initial_thys: List[String]): List[String] = {
    initial_thys.sorted 
  }
  
  // ISSUE
  private def try_get_from_cache(key: List[String]): Option[Session_Data] = {
    if (!enable_cache) return None
    
    var session_data: Option[Session_Data] = None
    
    session_cache.change { cache =>
      cache.get(key) match {
        case Some(queue) if queue.nonEmpty =>

          val cached_session = queue.head // get but does not remove
          session_data = Some(cached_session)
          lru_order.change(lru => { lru.put(key, System.currentTimeMillis()); lru })
          cache
        case _ =>
          session_data = None
          cache
      }
    }
    
    if (session_data.isDefined) {
      cache_stats.change(stats => { stats.update("hits", stats("hits") + 1); stats })
      session_data.foreach(sd => increment_session_ref(sd.id))
    } else {
      cache_stats.change(stats => { stats.update("misses", stats("misses") + 1); stats })
    }
    
    session_data
  }
  
  private def put_to_cache(key: List[String], session_data: Session_Data): Unit = {
    if (!enable_cache) return
    
    session_cache.change { cache =>
      val queue = cache.getOrElseUpdate(key, mutable.Queue.empty)
      
      if (!queue.exists(_.id == session_data.id)) {
        queue.enqueue(session_data)
        lru_order.change(lru => { lru.put(key, System.currentTimeMillis()); lru })
        
        evict_if_needed()
      }
      cache
    }
  }

  // ISSUE 
  private def evict_if_needed(): Unit = {
    if (!enable_cache) return
    
    val total_sessions = session_cache.value.values.map(_.size).sum
    if (total_sessions > max_cache_size) {

      lru_order.change { lru =>
        val sorted_keys = lru.toSeq.sortBy(_._2) 
        val keys_to_remove = sorted_keys.take(total_sessions - max_cache_size).map(_._1)
        
        keys_to_remove.foreach { key =>
          session_cache.change { cache =>
            cache.get(key) match {
              case Some(queue) if queue.nonEmpty =>
                val session_to_evict = queue.dequeue()
                mark_session_evicted(session_to_evict.id)
                remove_session_async(session_to_evict.id)
                cache_stats.change(stats => { stats.update("evictions", stats("evictions") + 1); stats })
                println(s"Evicted session for theories: ${key.mkString(", ")}")
                
                if (queue.isEmpty) {
                  cache.remove(key)
                }
                cache
              case _ => cache
            }
          }
        }
        
        keys_to_remove.foreach(key => lru.remove(key))
        lru
      }
    }
  }


  def release_session(session_data: Session_Data, theories: List[String]): Unit = {
    decrement_session_ref(session_data.id)
    
    val current_refs = session_refs.value.getOrElse(session_data.id, 0)
    
    if (current_refs > 0) {
      println(s"Session ${session_data.id} still has ${current_refs} references, keeping alive")
      return
    }
    
    if (!enable_cache || is_session_evicted(session_data.id)) {
      remove_session_async(session_data.id)
      println(s"Session ${session_data.id} destroyed (cache disabled or evicted)")
    } else {
      println(s"Session ${session_data.id} released but kept in cache for theories: ${theories.mkString(", ")}")
    }
  }
  
  def release_session_to_cache(session_data: Session_Data, theories: List[String]): Unit = {
    release_session(session_data, theories)
  }
  def get_session_with_cache(initial_thys: List[String], field: String = "HOL"): Session_Data = {
    val key = get_cache_key(initial_thys)
    
    try_get_from_cache(key) match {
      case Some(session_data) =>
        println(s"Cache hit for theories: ${key.mkString(", ")} - sharing session ${session_data.id}")
        session_data 
      case None =>
        println(s"Cache miss for theories: ${key.mkString(", ")}. Creating new session.")
        val session_data = create_new_session_internal(initial_thys, field)
        put_to_cache(key, session_data) 
        session_data
    }
  }

  // Per-session parallelism (configurable; conservative defaults to bound peak heap
  // under concurrent sessions — see the parallel-proof research and parallel-sessions notes, internal working notes).
  //   parallel_proofs=2 forks nested `have`/`show` proof bodies (independent haves run
  //     concurrently); 1 = top-level proofs only; 0 = sequential.
  //   threads is CAPPED (default 4, not 0=auto) so M concurrent sessions do not each
  //     grab all cores and oversubscribe / spike peak heap into an OOM (cf. Bug 6).
  private val parallel_proofs_opt: String =
    sys.env.getOrElse("ISABELLE_PARALLEL_PROOFS", "2")
  private val session_threads_opt: String =
    sys.env.getOrElse("ISABELLE_SESSION_THREADS", "4")

  private def create_new_session_internal(initial_thys: List[String], field: String = "HOL", dirs: List[String] = Nil): Session_Data = {
    val session_delay_options_to_minimise =
      List("headless_consolidate_delay", "headless_check_delay", "headless_nodes_status_delay")
    val min_delay = "0.1"
    val session_option_pairs =
      ("show_states", show_states.toString) ::
        ("parallel_proofs", parallel_proofs_opt) ::
        ("threads", session_threads_opt) ::
        session_delay_options_to_minimise.map(option_name => (option_name, min_delay))
    val session_options = session_option_pairs.map { case (name, value) => s"${name}=${value}" }
    if (closed) error("Session manager is shut down: no new session")
    val session_id = Server_Utils.start_session(server_info, server, session_options, field, dirs)
    // A shutdown that ran while the prover started has not seen this session: stop
    // it here, or its prover (bash_process -> bash -> poly) outlives every handle.
    val accepted = lifecycle_lock.synchronized {
      if (!closed) running_sessions.change(_ += session_id)
      !closed
    }
    if (!accepted) {
      stop_session_quietly(session_id)
      error(s"Session manager was shut down while session $session_id started; session stopped")
    }
    val session = server.the_session(session_id)
    // A failed initial load (theory error, interrupt, shutdown) must not leave the
    // started prover behind: the caller never receives a handle to it.
    try {
      if (initial_thys.nonEmpty) {
        session.use_theories(initial_thys)
      }
    } catch {
      case exn: Throwable =>
        remove_session_sync(session_id)
        throw exn
    }
    cache_stats.change(stats => { stats.update("creates", stats("creates") + 1); stats })
    increment_session_ref(session_id)
    Session_Data(session_id, session)
  }
  
  def get_new_session(initial_thys: List[String], field: String = "HOL", dirs: List[String] = Nil): Session_Data = {
    // dirs (heap sessions) bypass the cache: the cache key is theories-only,
    // and heap sessions are identified by their field+dirs (heap name + ROOT dir).
    if (enable_cache && dirs.isEmpty) {
      get_session_with_cache(initial_thys, field)
    } else {
      create_new_session_internal(initial_thys, field, dirs)
    }
  }

  /** Stop one session and forget it. A session whose stop FAILED stays in
   *  `running_sessions` (and registered with the Isabelle server), so that
   *  [[shutdown]] retries it and reports it if it still fails. */
  private def remove_session_sync(session_id: UUID.T): Boolean = {
    val ok = stop_session_quietly(session_id)
    if (ok) running_sessions.change(_ -= session_id)
    ok
  }

  /** Stop one session: Session.stop terminates its prover through Isabelle's
   *  Bash.Process.terminate, which signals the whole process group (INT, TERM,
   *  KILL). Errors are logged and answered with false, never thrown. */
  private def stop_session_quietly(session_id: UUID.T): Boolean =
    try { Server_Utils.stop_session(server_info, server, session_id); true }
    catch {
      case e: Server_Utils.Teardown_Incomplete =>
        // unregistered already, so a retry cannot reach it: remember it for the report
        Output.error_message(s"Error stopping session ${session_id}: ${Exn.message(e)}")
        teardown_incomplete.change(_ += session_id)
        true
      case e: Throwable =>
        Output.error_message(s"Error stopping session ${session_id}: ${Exn.message(e)}")
        false
    }

  /** Sessions whose stop failed and whose forced teardown was incomplete. */
  private val teardown_incomplete: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)

  def remove_session_async(session_id: UUID.T): Unit = {
    val removal_future = Future.fork[Unit] { remove_session_sync(session_id) }
    pending_removals.change(_ += removal_future)
    removal_future.map(_ => pending_removals.change(_ -= removal_future))
  }

  def remove_session_async(session_data: Session_Data): Unit =
    remove_session_async(session_data.id)

  /** Set once by [[shutdown]]: a closed manager starts no session and stops any
   *  session whose start was in flight when it closed. */
  private val lifecycle_lock = new Object
  @volatile private var closed = false
  def is_closed: Boolean = closed

  /** Stop every session of this manager and its Isabelle server. Idempotent and
   *  safe to call from any thread, also while another thread is inside a call on
   *  this manager (a session start, a reset, a long use_theories): the Python
   *  side calls it outside the session's worker thread when that thread is
   *  blocked, and the gateway's reaper calls it for abandoned backends. */
  def shutdown(): Unit = {
    val first = lifecycle_lock.synchronized { val was = closed; closed = true; !was }
    // Every call returns only when the stop is over: a later call (the reaper,
    // a second close) waits for the first one, so a return always means that
    // the provers are stopped (or the failure below is thrown).
    if (first) {
      try shutdown_now()
      finally shutdown_done.countDown()
    }
    else if (!shutdown_done.await(Session_Manager.SHUTDOWN_WAIT_S, java.util.concurrent.TimeUnit.SECONDS)) {
      error(s"Session manager still stopping after ${Session_Manager.SHUTDOWN_WAIT_S} s")
    }
    shutdown_failure.foreach(msg => error(msg))
  }

  private val shutdown_done = new java.util.concurrent.CountDownLatch(1)
  @volatile private var shutdown_failure: Option[String] = None

  private def shutdown_now(): Unit = {
    def apply_foreach[A](mutable_set: Synchronized[mutable.Set[A]])(f: A => Unit): Unit =
      mutable_set.guarded_access(set => Some((set.toList, set))).foreach(f)

    apply_foreach(running_sessions)(remove_session_async)

    apply_foreach(pending_removals) { removal_future =>
      try removal_future.join
      catch {
        case e: Throwable =>
          Output.error_message(s"Error during session removal: ${Exn.message(e)}")
      }
    }
    // Every cached session is also in running_sessions, so it was stopped above
    // (or is retried below): only forget the cache, start no second removal.
    session_cache.change { cache => cache.clear(); cache }
    
    lru_order.change(lru => { lru.clear(); lru })

    // Retry the sessions whose stop failed (they are still registered).
    apply_foreach(running_sessions)(id => remove_session_sync(id))
    val failed = running_sessions.value.toList ++ teardown_incomplete.value.toList

    // Server shutdown also stops every session still registered with the server.
    try Server_Utils.stop_server(server_info)
    catch {
      case e: Throwable =>
        Output.error_message(s"Error stopping server ${server_info.name}: ${Exn.message(e)}")
    }
    if (failed.nonEmpty) {
      // Reported to the caller (ThreadedBackend.close logs it; the server then
      // ends a known prover pid itself).
      shutdown_failure = Some(s"Failed to stop session(s) ${failed.mkString(", ")}; " +
        "their provers may still run")
    }
  }
}

object Session_Manager {
  /** How long a second [[Session_Manager.shutdown]] waits for the first one (s). */
  val SHUTDOWN_WAIT_S: Long =
    sys.env.get("ISABELLE_REPL_SHUTDOWN_WAIT").flatMap(_.toLongOption).getOrElse(120L)
}

