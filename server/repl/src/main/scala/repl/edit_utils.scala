package repl

import isabelle._

import io.bullet.spliff.Diff

/** Construction of PIDE document edits: text insert/remove edits, node
 *  perspective edits, and theory-header processing (parsing the accumulated
 *  header, importing its dependencies, emulating the REPL helper import).
 *  Shared infrastructure used by [[Repl_Session]] for every edit path in both
 *  workflows. */
type Edit = Document.Node.Edit[Text.Edit, Text.Perspective]

object Edit_Utils {
  def insert_text_edit(
      string: String,
      thy_info: Thy_Info
  ): Text.Edit = {
    val formatted_string = string + "\n"
    Text.Edit.insert(thy_info.insertion_point, formatted_string)
  }

  def remove_insert_edit(insert_edit: Text.Edit): Text.Edit =
    Text.Edit.remove(insert_edit.start, insert_edit.text)

  def edit_from_text_edit(text_edit: Text.Edit): Edit =
    edit_from_text_edits(List(text_edit))

  def edit_from_text_edits(text_edits: List[Text.Edit]): Edit =
    Document.Node.Edits[Text.Edit, Text.Perspective](
      text_edits
    )

  /** Convert a spliff diff of `base` → `target` into a SEQUENTIAL PIDE edit list:
   *  PIDE (Thy_Syntax.edit_text) applies the edits one after another, each
   *  against the text as left by the previous ones, so every edit must carry
   *  its offset in that EVOLVING text. spliff's `delInsOpsSorted` instead
   *  describes a SIMULTANEOUS script in base coordinates: `Delete(b, c)` removes
   *  base[b, b+c); `Insert(b, t, k)` puts target[t, t+k) before base[b] — and
   *  `b` may lie INSIDE or at the end of a preceding deleted range, which means
   *  "at the start of that (now removed) range" (e.g. `(simp)` → `auto` comes
   *  out as Delete(69,6) Insert(74,…,3) Insert(75,…,1)). The old cumulative
   *  shift (`offset = b + shift`, shift updated by every op) mapped such inserts
   *  one deletion-width too early and CORRUPTED the node text — `by simp`
   *  became `imp  by s` (docs/ISSUES.md Bug 18 / tracker SYNC-1).
   *
   *  Mapping rule: a base position after all processed ops is `b + delta`; a
   *  position inside/at the end of the last deleted range collapses to that
   *  range's start in the current text, and successive collapsed inserts chain
   *  after one another. `origin` is added to every position (the node offset at
   *  which `base`/`target` start; 0 for whole-node texts). Returns the edits and
   *  the base offset (origin-relative) of the first op, None when identical.
   *  (`.nn` on substring: the RC0-track compiler types it nullable.) */
  def diff_edits(
      base: String,
      target: String,
      origin: Int = 0
  ): (List[Text.Edit], Option[Text.Offset]) =
    if (base == target) (List(), None)
    else {
      val ops = Diff(base, target).delInsOpsSorted.toList
      var delta = 0                                   // current = base + delta past all ops
      var del: Option[(Int, Int, Int)] = None         // last delete: (base start, base end, next current insert pos)
      var first_change: Option[Text.Offset] = None
      val edits = ops.map { op =>
        val b = op match {
          case Diff.Op.Insert(baseIx, _, _) => baseIx
          case Diff.Op.Delete(baseIx, _)    => baseIx
        }
        if (first_change.isEmpty) first_change = Some(origin + b)
        op match {
          case Diff.Op.Delete(baseIx, count) =>
            val pos = baseIx + delta
            delta -= count
            del = Some((baseIx, baseIx + count, pos))
            Text.Edit.remove(origin + pos, base.substring(baseIx, baseIx + count).nn)
          case Diff.Op.Insert(baseIx, targetIx, count) =>
            val pos = del match {
              case Some((s, e, next)) if baseIx >= s && baseIx <= e =>
                del = Some((s, e, next + count))     // chain the next collapsed insert after this one
                next
              case _ =>
                del = None
                baseIx + delta
            }
            delta += count
            Text.Edit.insert(origin + pos, target.substring(targetIx, targetIx + count).nn)
        }
      }
      (edits, first_change)
    }

  /** Minimal insert/remove edit sequence transforming `old_text` into
   *  `new_text` (see `diff_edits`; whole-node texts, origin 0). Also returns
   *  the offset in `old_text` of the FIRST change (None when identical).
   *  Backs Repl_Session.replace_document (incremental PIDE document sync).
   *  Only the texts after their common prefix are diffed: a character diff may
   *  place an equivalent edit inside unchanged text (e.g. start a line inserted
   *  after `begin` inside the word `begin`), and replace_document reads the
   *  first change as a header edit and restarts the prover. */
  def text_diff_edits(
      old_text: String,
      new_text: String
  ): (List[Text.Edit], Option[Text.Offset]) = {
    val n = old_text.length min new_text.length
    var p = 0
    while (p < n && old_text.charAt(p) == new_text.charAt(p)) p += 1
    if (p > 0 && Character.isHighSurrogate(old_text.charAt(p - 1))) p -= 1  // keep pairs whole
    diff_edits(old_text.substring(p).nn, new_text.substring(p).nn, origin = p)
  }

  def set_required_edit(required: Boolean): Edit =
    Document.Node.Perspective[Text.Edit, Text.Perspective](
      required,
      Text.Perspective.empty,
      Document.Node.Overlays.empty
    )

  private def dependencies_edit(
      session: Headless.Session,
      node_name: Document.Node.Name,
      thy_header: Thy_Header
  ): Option[Edit] = {
    val imports = thy_header.imports.map { case (s, pos) =>
      val name = session.resources.import_name(node_name, s)
      (name, pos)
    }
    val illegal_imports =
      imports.filter { case (name, _) =>
        Sessions.illegal_theory(name.theory_base_name)
      }
    illegal_imports.foreach { case (name, pos) =>
      Repl_Output.add_error(
        "Illegal theory name " + quote(name.theory_base_name) + Position
          .here(pos)
      )
    }
    if (illegal_imports.nonEmpty) None
    else {
      // Isabelle 2026 (RC1+): the node header edit is `Document.Node.Thy`
      // carrying a `Resources.Thy` (what `Resources.check_thy` builds from a
      // file). `Document.Node.Header`/`Node.Deps` (2025-2 … 2026-RC0) are gone.
      // `eval_conditions` is mandatory: `Resources.Thy.encode` (protocol side)
      // reads `condition_bad`, which errors on unevaluated conditions and would
      // inject "Unevaluated conditions for theory …" into the node.
      val thy =
        Resources.Thy(
          name = node_name,
          pos = thy_header.pos,
          imports = imports,
          options = thy_header.options,
          keywords = thy_header.keywords,
          abbrevs = thy_header.abbrevs
        ).eval_conditions(session.conditions)
      Some(Document.Node.Thy[Text.Edit, Text.Perspective](thy))
    }
  }

  private def import_all_theories(
      session: Headless.Session,
      all_import_names: List[String]
  ): Boolean =
    all_import_names.forall { local_import_name =>
      val error_msg = s"Failed to import theory: $local_import_name"
      val import_successful =
        try {
          val result = session.use_theories(List(local_import_name))
          if (!result.ok) Repl_Output.add_error(error_msg)
          result.ok
        } catch {
          case e: Exception =>
            Repl_Output.add_error(s"$error_msg\n${e.getMessage}")
            false
        }
      import_successful
    }

  def get_thy_header_edits(
      thy_info: Thy_Info,
      session: Headless.Session,
      node_name: Document.Node.Name,
      additional_imports_to_emulate: List[String] = Nil
  ): Option[List[Edit]] =
    Thy_Parsing.extract_thy_header_from_tokens(thy_info.accumulated_thy_header_tokens) match {
      case None =>
        Repl_Output.add_error("Invalid theory header."); None
      case Some(thy_header) =>
        val all_imports_successful = import_all_theories(
          session,
          thy_header.imports.map(_._1)
        )
        if (!all_imports_successful) return None

        val first_import = thy_header.imports.headOption.getOrElse(
          error("Cannot have theory header with no imports.")
        )
        def emulate_thy_import(thy: String): Edit = {
          val emulated_import = (thy, first_import._2)
          val emulated_import_header =
            thy_header.copy(imports = List(emulated_import))
          dependencies_edit(
            session,
            node_name,
            emulated_import_header
          ).getOrElse(error(s"Could not emulate header for import of theory $thy."))
        }
        val emulated_imports_edits = additional_imports_to_emulate.map(emulate_thy_import)

        dependencies_edit(session, node_name, thy_header).map { true_deps_edit =>
          thy_info.set_header_processed(true)
          // LOAD-BEARING (heap-pool model, Stage 3): the emulated wrapper-import
          // header edit (Node.Thy; Node.Deps before Isabelle 2026-RC1) is applied
          // AFTER the true-deps edit and REPLACES the header (the PIDE keeps the
          // last header edit). Consequence: the document's own
          // import list only gates LOADING (import_all_theories above); the visible
          // parent context comes from the wrapper alone. Therefore the wrapper must
          // state every import whose facts/ML environment the document needs.
          // Do NOT merge the two Deps edits — the design relies on the overwrite
          // (heap-pool research, internal working notes, spike 3).
          true_deps_edit :: emulated_imports_edits
        }
    }
}
