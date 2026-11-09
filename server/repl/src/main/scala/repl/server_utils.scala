package repl

import scala.language.unsafeNulls

import isabelle._

/** Low-level Isabelle server lifecycle: start/stop the `isabelle server`
 *  process and start/stop individual headless sessions on it (via
 *  Server_Commands, with reflective access to Server.Context). `start_session`'s
 *  `dirs` adds session ROOT directories (like `isabelle build -d`) so a session
 *  can start on a USER HEAP (heap-pool model — Stage 3). Shared infrastructure
 *  used only by [[Session_Manager]]; neither MCP workflow touches it directly. */
object Server_Utils {
  private def withServerContext(
      server_info: Server.Info,
      server: Server
  )(f: Server.Context => JSON.Object.T): JSON.Object.T =
    using(server_info.connection()) { connection =>

      val ctxClass = Class.forName("isabelle.Server$Context")
      val ctor = ctxClass.nn.getDeclaredConstructors.head
      ctor.setAccessible(true)
      val ctx =
        ctor.newInstance(server, connection).asInstanceOf[Server.Context]
      f(ctx)
    }

  /** Server.init registers the server in the user's servers.db (SQLite). Session backends are
   *  created concurrently (ISABELLE_PARALLEL_SESSION_CREATE), and concurrent registrations
   *  fail with SQLITE_BUSY: serialize them in this JVM. Another process using the same
   *  database (a second pool server) can still collide, so a failed attempt is retried. */
  private val init_lock = new Object

  def start_server(): (Server.Info, Server) = {
    def attempt_start_server(): Option[(Server.Info, Server)] = {
      val server_name = UUID.random_string()
      // Isabelle 2026: Server.init takes a mandatory Logger as its first arg.
      try {
        val (server_info, server_opt) =
          init_lock.synchronized { Server.init(new Console_Logger(), name = server_name) }
        server_opt.map(server => (server_info, server))
      } catch {
        case exn: java.sql.SQLException =>
          Output.warning(s"Server.init failed (${exn.getMessage}); retrying")
          Time.seconds(0.5).sleep()
          None
      }
    }

    def retry_attempt_start_server(max_retries: Int): Option[(Server.Info, Server)] =
      LazyList.continually(attempt_start_server()).take(max_retries).find(_.isDefined).flatten

    val max_retries = sys.env.get("ISABELLE_SERVER_START_RETRIES").flatMap(_.toIntOption).getOrElse(5)
    retry_attempt_start_server(max_retries) match {

      case None => error(s"Unable to start server after ${max_retries} attempts.")
      case Some((server_info, server)) =>
        Output.writeln(s"Started server ${server_info.name}.")
        (server_info, server)
    }
  }

  def stop_server(server_info: Server.Info): Unit =
    Server.exit(server_info.name) match {
      case true  => Output.writeln(s"Stopped server ${server_info.name}.")
      case false => error(s"Failed to stop server ${server_info.name}.")
    }

  /** Session start builds a session image that is missing or not up to date, inside the
   *  request (Session_Start runs Build.build). ISABELLE_SESSION_START_BUILD=false forbids that:
   *  such a build can take minutes and bypasses the server's build gates. */
  private val session_start_build: Boolean =
    !sys.env.get("ISABELLE_SESSION_START_BUILD").map(_.trim.toLowerCase).exists(
      Set("0", "false", "no", "off"))

  /** Fail unless the image of `session` is up to date: the check Build.build_logic makes
   *  (no_build = true), with the options and dirs Session_Start would use. */
  private def check_image_current(session: String, options: List[String], dirs: List[String]): Unit = {
    val opts = Options.init(prefs = Server_Commands.default_preferences,
      update = options.map(Options.Spec.make))
    val results = Build.build(opts, selection = Sessions.Selection.session(session),
      build_heap = true, no_build = true, dirs = dirs.map(Path.explode))
    if (!results.ok) {
      error(s"Session image $session is missing or not up to date, and " +
        "ISABELLE_SESSION_START_BUILD=false forbids building it during session start. " +
        s"Build it beforehand (isabelle build -b $session), or find out why it is outdated " +
        s"(isabelle build -n -v -o build_debug $session lists the differing sources).")
    }
  }

  def start_session(server_info: Server.Info, server: Server, options: List[String], field: String = "HOL", dirs: List[String] = Nil): UUID.T = {

     val session_start_json = withServerContext(server_info, server) { context =>

    val result_cell = Synchronized[Option[JSON.Object.T]](None)
    val error_cell  = Synchronized[Option[Throwable]](None)

    Isabelle_Thread.fork(name = "session_start") {
      try {
        if (!session_start_build) check_image_current(field, options, dirs)
        val args = Server_Commands.Session_Start.Args(
          build = Server_Commands.Session_Build.Args(session = field, options = options, dirs = dirs)
        )
        val (res, entry) =
          Server_Commands.Session_Start.command(
            args, progress = context.progress(), log = context.server.log
          )
        context.server.add_session(entry)
        result_cell.change(_ => Some(res))
      } catch {
        case t: Throwable =>
          error_cell.change(_ => Some(t))
          Output.error_message(s"Session_Start failed for session='$field' options=${options.mkString(",")}: ${Exn.message(t)}")
      }
    }.join()

    error_cell.value match {
      case Some(t) => error(s"Session start failed for session='$field': ${Exn.message(t)}")
      case None    => result_cell.value.getOrElse(error("Session start failed (no result and no captured error)"))
    }
  }

  val session_id = JSON.uuid(session_start_json, "session_id")
    .getOrElse(error("Unable to retrieve session id."))
  Output.writeln(s"Started session ${session_id}.")
  session_id
  }

  def stop_session(
      server_info: Server.Info,
      server: Server,
      session_id: UUID.T
  ): Unit = {
    // grab session, run command, then unregister
    val session = server.remove_session(session_id)
    val (session_stop_json, _) = Server_Commands.Session_Stop.command(session)

    val return_code = session_stop_json.getOrElse(
      "return_code",
      error("Unable to retrieve session stop return code.")
    )
    Output.writeln(s"Stopped session ${session_id} with return code $return_code.")
  }
}
