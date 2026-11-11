package repl

import scala.language.unsafeNulls

import isabelle._

import scala.collection.mutable

/** Forced teardown of an Isabelle session whose `Session.stop()` failed.
 *
 *  `Session.stop()` first sends `Stop` to the session's manager thread. When that
 *  thread is dead (an OutOfMemoryError in `Isabelle.Session.manager` kills it), the
 *  send throws "Consumer thread not active" and `stop()` returns before it shuts
 *  down the other threads of the session (`change_parser`, `nodes_status_buffer`,
 *  `change_buffer`'s flush delay, `dispatcher`) and before the prover is
 *  terminated. Those threads stay alive and reference the session, and through its
 *  manager the whole `Document.State` (every command result of every loaded
 *  theory). The session can never be collected, and a second `stop()` blocks for
 *  ever (its phase is now `Shutdown`, which `stop()` waits on). Observed on the
 *  deployment 2026-11-11: the gateway heap stayed full with no session registered.
 *
 *  `force` does the part of `stop()` that did not run. The session members are
 *  private, so it reaches them by reflection (names as compiled from Isabelle2026's
 *  session.scala). Every step is bounded and independent; failures are returned as
 *  messages, never thrown. */
object Session_Teardown {
  /** Wall time one step may take (s). */
  val STEP_WAIT_S: Double =
    sys.env.get("ISABELLE_REPL_TEARDOWN_STEP_WAIT").flatMap(_.toDoubleOption).getOrElse(10.0)

  private val session_class = classOf[isabelle.Session]

  /** A field of class Session by name; Scala may compile a private member under a
   *  mangled name (`isabelle$Session$$name`). */
  private def field(session: isabelle.Session, name: String): AnyRef = {
    val f = session_class.getDeclaredFields.find(f =>
      f.getName == name || f.getName.endsWith("$$" + name)).getOrElse(
        throw new NoSuchFieldException(name))
    f.setAccessible(true)
    f.get(session)
  }

  /** The instance of a lazily initialized member object (`private object prover`). */
  private def member_object(session: isabelle.Session, name: String): AnyRef = {
    val m = session_class.getDeclaredMethod(name)
    m.setAccessible(true)
    m.invoke(session)
  }

  private def call(obj: AnyRef, name: String): AnyRef = {
    val m = obj.getClass.getMethod(name)
    m.setAccessible(true)
    m.invoke(obj)
  }

  /** The JVM thread of a Consumer_Thread (to interrupt it when it does not stop). */
  private def consumer_jvm_thread(consumer: AnyRef): Option[Thread] =
    try {
      val f = classOf[Consumer_Thread[?]].getDeclaredField("thread")
      f.setAccessible(true)
      Option(f.get(consumer)).collect { case t: Thread => t }
    } catch { case _: Throwable => None }

  /** Run `body` on its own thread for at most STEP_WAIT_S; on timeout interrupt
   *  `stuck` (if given) and report. */
  private def bounded(what: String, problems: mutable.ListBuffer[String],
      stuck: => Option[Thread] = None)(body: => Unit): Unit = {
    val error = Synchronized[Option[String]](None)
    val worker = new Thread(new Runnable { def run(): Unit =
      try body
      catch {
        case e: java.lang.reflect.InvocationTargetException if e.getCause != null =>
          error.change(_ => Some(Exn.message(e.getCause.nn)))
        case e: Throwable => error.change(_ => Some(Exn.message(e)))
      } }, "session_teardown:" + what)
    worker.setDaemon(true)
    worker.start()
    worker.join((STEP_WAIT_S * 1000).toLong)
    if (worker.isAlive) {
      stuck.foreach(_.interrupt())
      worker.interrupt()
      problems += s"$what: did not finish within ${STEP_WAIT_S}s"
    }
    else error.value.foreach(msg => problems += s"$what: $msg")
  }

  /** Cancel the session's pending consolidation and history-prune delays, as the
   *  manager does on Stop. A scheduled delay sits in the JVM-global Event_Timer
   *  queue and holds the session until it fires (~30 s). Also needed after a stop
   *  that completed because the prover had already exited: then stop() sends no
   *  Stop, and the delays stay armed. */
  def cancel_delays(session: isabelle.Session,
      problems: mutable.ListBuffer[String] = new mutable.ListBuffer[String]): List[String] = {
    bounded("consolidation", problems) {
      call(member_object(session, "isabelle$Session$$consolidation"), "exit")
    }
    bounded("delay_prune", problems) {
      field(session, "delay_prune$lzy1") match {
        case delay: Delay => delay.revoke()
        case _ =>  // never initialized
      }
    }
    problems.toList
  }

  /** Finish the shutdown of `session` that `Session.stop()` left undone. Returns
   *  the steps that failed (empty: the session holds no thread and no prover). */
  def force(session: isabelle.Session): List[String] = {
    val problems = new mutable.ListBuffer[String]

    // 0. What the manager does on Stop before the prover goes.
    cancel_delays(session, problems)

    // 1. The prover: its reader threads deliver to the session, and the process
    //    must not outlive the session (Prover.terminate: close input, then
    //    INT/TERM/KILL to the process group).
    bounded("prover", problems) {
      val prover = member_object(session, "prover")
      if (call(prover, "defined") == java.lang.Boolean.TRUE) {
        call(call(prover, "get"), "terminate")
        call(prover, "reset")
      }
    }

    // 2. What the manager does when the prover has exited (Session.handle_exit):
    //    protocol handlers stop their threads and processes (Naproche server,
    //    Simplifier_Trace manager), file formats end their session, and the phase
    //    becomes Terminated. The phase also wakes any thread blocked in stop().
    bounded("protocol_handlers", problems) {
      field(session, "protocol_handlers") match {
        case handlers: Protocol_Handlers => handlers.exit(Document.State.init)
        case other => error("unexpected " + other)
      }
    }
    bounded("file_formats", problems) {
      field(session, "file_formats$lzy1") match {
        case formats: File_Format.Session => formats.stop_session()
        case _ =>  // never initialized
      }
    }
    bounded("phase", problems) {
      field(session, "_phase") match {
        case phase: Synchronized[?] =>
          phase.asInstanceOf[Synchronized[isabelle.Session.Phase]].change {
            case terminated: isabelle.Session.Terminated => terminated
            case _ => isabelle.Session.Terminated(Process_Result(1))
          }
        case other => error("unexpected " + other)
      }
    }

    // 3. The threads Session.stop() did not reach, in its order.
    bounded("nodes_status_buffer", problems) {
      call(member_object(session, "nodes_status_buffer"), "shutdown")
    }
    for (name <- List("change_parser")) {
      val consumer = try Some(field(session, name)) catch { case _: Throwable => None }
      consumer match {
        case Some(c: Consumer_Thread[?]) =>
          bounded(name, problems, consumer_jvm_thread(c)) { c.shutdown() }
        case _ => problems += s"$name: not found"
      }
    }
    bounded("change_buffer", problems) {
      call(member_object(session, "change_buffer"), "shutdown")
    }
    for (name <- List("isabelle$Session$$manager", "isabelle$Session$$dispatcher")) {
      val consumer = try Some(field(session, name)) catch { case _: Throwable => None }
      consumer match {
        case Some(c: Consumer_Thread[?]) =>
          bounded(name.stripPrefix("isabelle$Session$$"), problems, consumer_jvm_thread(c)) {
            c.shutdown()
          }
        case _ => problems += s"$name: not found"
      }
    }
    problems.toList
  }
}
