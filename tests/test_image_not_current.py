"""Session start does not build a missing or outdated image (ISABELLE_SESSION_START_BUILD=false):
the failure reaches the client as 409 image_not_current with the build command in the text."""
from server.app.errors import ImageNotCurrent, SessionError, image_not_current_text

JAVA_MSG = (
    "An error occurred while calling z:repl.ReplBackendGateway.get_repl_backend_tagged.\n"
    "*** Session start failed for session='BVSmt_Smt': Session image BVSmt_Smt is missing or not up "
    "to date, and ISABELLE_SESSION_START_BUILD=false forbids building it during session start. "
    "Build it beforehand (isabelle build -b BVSmt_Smt), or find out why it is outdated "
    "(isabelle build -n -v -o build_debug BVSmt_Smt lists the differing sources).\n"
    "\tat isabelle.Exn$ERROR$.apply(exn.scala:27)"
)


def test_text_is_extracted_without_the_stack():
    text = image_not_current_text(JAVA_MSG)
    assert text.startswith("Session image BVSmt_Smt is missing or not up to date")
    assert "isabelle build -b BVSmt_Smt" in text and "exn.scala" not in text


def test_other_failures_are_not_taken_for_it():
    assert image_not_current_text("Session start failed: out of memory") is None
    assert image_not_current_text("Session image X is missing or not up to date, building it") is None


def test_error_is_a_session_error():
    assert issubclass(ImageNotCurrent, SessionError)
