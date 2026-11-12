package repl

import isabelle._
import scala.collection.mutable

/** Mutable per-theory bookkeeping: insertion point, theory-header
 *  accumulation, the chain of [[Thy_Status]] snapshots behind rollback, and
 *  saved states behind checkpoints. Shared infrastructure used by
 *  [[Repl_Session]] for both workflows. */
class Thy_Info(val name: String, status: Option[Thy_Status] = None) {
  /** Directory the document comes from ("" = unknown): bare-name imports resolve
   *  against it (see Edit_Utils). */
  var master_dir: String = ""
  private var current_status: Thy_Status = status.getOrElse(Thy_Status())
  private val saved_states: mutable.Map[EnvStateID, Thy_Status] = mutable.Map.empty

  def header_processed: Boolean = current_status.header_processed
  def insertion_point: Int = current_status.insertion_point
  def accumulated_thy_header_tokens: List[Token] = current_status.accumulated_thy_header_tokens

  private def previous_status: Option[Thy_Status] = current_status.parent
  def last_insertion_line: Int = previous_status.map(_.insertion_line).getOrElse(1)
  def last_text_edit: Option[Text.Edit] = current_status.last_text_edit

  private def has_accumulated_full_thy_header: Boolean =
    current_status.accumulated_thy_header_tokens.exists(_.is_begin)

  def need_header_processing: Boolean =
    !header_processed && has_accumulated_full_thy_header

  def set_header_processed(header_processed: Boolean): Unit =
    current_status = current_status.copy(header_processed = header_processed)

  def update_given_text_edit(text_edit: Text.Edit): Boolean = {
    var new_status = current_status.copy(parent = Some(current_status))
    if (!header_processed) {
      val tokens_to_add =
        Thy_Parsing.get_thy_header_tokens(
          Scan.char_reader(text_edit.text),
          drop_tokens_before_thy_command = current_status.accumulated_thy_header_tokens.isEmpty
        )
      new_status = new_status.add_thy_header_tokens(tokens_to_add)
      if (!new_status.input_thy_name_verified) {
        val header_name_does_not_match_entered_name =
          new_status.input_thy_name match {
            case Some(input_thy_name) =>
              new_status = new_status.copy(input_thy_name_verified = true)
              name != input_thy_name
            case None => false
          }
        if (header_name_does_not_match_entered_name) {
          Repl_Output.add_error(
            "Name of theory in header must match name of current theory."
          )
          return false
        }
      }
    }
    val new_insertion_point = current_status.insertion_point + text_edit.text.length
    val new_insertion_line = current_status.insertion_line + text_edit.text.count(_ == '\n')
    new_status = new_status.copy(
      last_text_edit = Some(text_edit),
      insertion_point = new_insertion_point,
      insertion_line = new_insertion_line
    )
    current_status = new_status
    true
  }

  def rollback_last_text_edit_if_exists(): Unit =
    current_status = current_status.parent.getOrElse(current_status)

  /** Re-base the append-only bookkeeping after a wholesale incremental REPLACE
   *  (Repl_Session.replace_document): the parent/rollback chain and the saved
   *  states encode the OLD text, so both are cut (unsound after a replace); the
   *  fresh base describes a document consisting of `text` alone, ready for
   *  further tip appends. This is the targeted equivalent of starting a new
   *  Thy_Info on reset — deliberately NOT a generalization of the insert-only
   *  update_given_text_edit chain. */
  def reset_to_fresh_base(text: String): Unit = {
    current_status = Thy_Status(
      accumulated_thy_header_tokens = current_status.accumulated_thy_header_tokens,
      input_thy_name_verified = current_status.input_thy_name_verified,
      header_processed = current_status.header_processed,
      insertion_point = text.length,
      insertion_line = text.count(_ == '\n') + 1
    )
    saved_states.clear()
  }

  def save_current_state(state_id: EnvStateID): Unit =
    saved_states.addOne((state_id, current_status))

  def has_saved_state(state_id: EnvStateID): Boolean = saved_states.contains(state_id)

  /** The edits that bring the node from the current status back to checkpoint
   *  `state_id`; None when this theory does not hold that checkpoint (never
   *  saved here, or wiped by reset_to_fresh_base) — the caller must treat None
   *  as failure, not as "nothing to do". */
  def restore_state(state_id: EnvStateID): Option[List[Text.Edit]] =
    saved_states.get(state_id).map { status_to_restore =>
      val edits_required = status_to_restore.difference_edits(current_status)
      current_status = status_to_restore
      edits_required
    }

  def duplicate(duplicate_name: String): Thy_Info = new Thy_Info(duplicate_name, status)
}
