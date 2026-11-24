(* Attempt.ML — run a candidate proof text on the result state of a host command
   (isabelle_multi_attempt in the file's own session, efficiency plan stage C).

   Loaded by the generated wrapper theories right after REPL.ML (server/repl/thys/).
   The Scala side (backend_attempts.scala) attaches the Query_Operation
   `isabelle_pool_server_attempt` as a document OVERLAY on the last command that ends
   before the attempt's line; each candidate is its own overlay, so several candidates
   run in parallel (print functions of pri 0 are forked: Command.parallel_print), and
   removing an overlay cancels its print task.

   Arguments: [timeout_s, candidate text]. The candidate is split into commands with
   the host theory's outer syntax and run command by command as PIDE runs a document
   (Command.eval_state): Toplevel.reset_state, then Toplevel.command_errors; a failed
   command leaves the state as it was, and the next command runs on that state. The
   whole run is bounded by Timeout.apply_physical (timeout_s < 1: no bound): wall time,
   since Timeout.apply does not count GC time (Event_Timer moves a non-physical request
   by the GC time since it was made) and an allocation-heavy candidate overran its limit
   several times over in the prototype.

   Proofs are NOT forked here (parallel_proofs = 0 for the run): a forked `by` would
   report its error to the future, outside the candidate's result, possibly after the
   time limit. Run inline, every error of the candidate is an error of its command.

   Output, one writeln_result per line, fields separated by TAB; text fields escaped
   (backslash, newline and TAB as \\ \n \t):
     start <line> <kind>                          before a command runs
     cmd   <line> <kind> <status> <sorry> <msg>   after it: status ok | failed | running
                                                  (running = stopped by the time limit),
                                                  sorry 1 for an executed sorry/oops
     goal  <text>                                 one per open subgoal (Repl.open_subgoals)
     end   <timed_out> <in_proof> <subgoals> <elapsed_ms>
   <line> is the 1-based line within the candidate. `end` is the last line. *)

signature ATTEMPT =
sig
  val run: int -> string -> Toplevel.state -> (string -> unit) -> unit
end;

structure Attempt : ATTEMPT =
struct

fun escape s =
  String.translate (fn #"\\" => "\\\\" | #"\n" => "\\n" | #"\t" => "\\t" | c => String.str c) s;

fun bool01 b = if b then "1" else "0";

fun field_line fields = space_implode "\t" fields;

fun line_of tr = the_default 0 (Position.line_of (Toplevel.pos_of tr));

(* the commands Toplevel.reset_state replaces the state for, with its error message *)
fun reset_needed tr st =
  let val kind = Toplevel.kind_of tr in
    (Symset.member Keyword.theory_body_kinds kind andalso not (Toplevel.is_theory st)) orelse
    (Symset.member Keyword.proof_kinds kind andalso not (Toplevel.is_proof st))
  end;

fun clean_string s = XML.content_of (YXML.parse_body s);

fun messages errs = cat_lines (map (fn ((_, msg), _) => clean_string msg) errs);

fun run timeout_s text state0 out =
  let
    val start_time = Time.now ();
    fun elapsed_ms () = Time.toMilliseconds (Time.now () - start_time);
    val thy = Proof_Context.theory_of (Toplevel.context_of state0);
    val init = fn () => error "a theory header cannot be attempted";
    val trs =
      Outer_Syntax.parse_text thy init (Position.line 1) text
      |> filter_out Toplevel.is_ignored;
    val current = Unsynchronized.ref (NONE : Toplevel.transition option);
    val state = Unsynchronized.ref state0;

    fun report tr status sorry msg =
      out (field_line
        ["cmd", string_of_int (line_of tr), Toplevel.name_of tr, status, bool01 sorry,
          escape msg]);

    fun step tr =
      let
        val _ = current := SOME tr;
        val _ = out (field_line ["start", string_of_int (line_of tr), Toplevel.name_of tr]);
        val st = ! state;
        val reset = reset_needed tr st;
        val st' = Toplevel.reset_state tr st;
        val reset_msg = if reset then [Toplevel.type_error tr ^ " -- using reset state"] else [];
        val (errs, result) = Toplevel.command_errors tr st';
        val _ =
          (case result of
            NONE =>
              if null errs then Isabelle_Thread.raise_interrupt ()  (*cancelled or timed out*)
              else (state := st'; report tr "failed" false (cat_lines (reset_msg @ [messages errs])))
          | SOME st'' =>
              let
                val name = Toplevel.name_of tr;
                val sorry = name = "sorry" orelse name = "oops";
              in
                state := st'';
                if reset then report tr "failed" false (cat_lines reset_msg)
                else report tr "ok" sorry ""
              end);
        val _ = current := NONE;
      in () end;

    fun run_all () = Interactive.setmp_parallel_proofs 0 (List.app step) trs;

    val timed_out =
      (if timeout_s < 1 then run_all () else Timeout.apply_physical (Time.fromSeconds timeout_s) run_all ();
       false)
      handle Timeout.TIMEOUT _ =>
        ((case ! current of SOME tr => report tr "running" false "" | NONE => ()); true);

    val final = ! state;
    val in_proof = not timed_out andalso Toplevel.is_proof final;
    val goals = if in_proof then Repl.open_subgoals final else [];
    val _ = List.app (fn g => out (field_line ["goal", escape g])) goals;
  in
    out (field_line
      ["end", bool01 timed_out, bool01 in_proof, string_of_int (length goals),
        string_of_int (elapsed_ms ())])
  end;

end;

val _ =
  Query_Operation.register {name = "isabelle_pool_server_attempt", pri = 0}
    (fn {state = st, args, writeln_result, ...} =>
      (case args of
        [timeout_s, text] => Attempt.run (the_default 30 (Int.fromString timeout_s)) text st writeln_result
      | _ => error "isabelle_pool_server_attempt: arguments [timeout_s, text] expected"));
