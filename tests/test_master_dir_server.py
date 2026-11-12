"""Server side of the master directory: DocumentLoadRequest.master_dir -> Session.load_document
-> backend.enter_thy_in. Without master_dir nothing changes (old backend entry point)."""
from __future__ import annotations

import pytest

from server.app.api.v1.schemas.API_models import DocumentLoadRequest
from tests.test_document_sync import _DOC_V1, _make_session


def test_master_dir_reaches_the_backend_entry():
    s = _make_session()
    raw = s.backend.raw
    raw.entered_in = []
    raw.enter_thy_in = lambda name, d: raw.entered_in.append((name, d))
    s.load_document(_DOC_V1, master_dir="/work/top")
    assert raw.entered_in == [("Foo", "/work/top")]
    assert raw.entered == []


def test_without_master_dir_the_old_entry_is_used():
    s = _make_session()
    s.load_document(_DOC_V1)
    assert s.backend.raw.entered == ["Foo"]


def test_same_theory_other_directory_does_not_sync_incrementally():
    s = _make_session()
    raw = s.backend.raw
    raw.enter_thy_in = lambda name, d: raw.entered.append(name)
    s.load_document(_DOC_V1, report=True, master_dir="/a")
    s.load_document(_DOC_V1 + "\n", report=True, master_dir="/b")
    assert raw.sync_calls == []  # re-entered with the new directory instead


@pytest.mark.parametrize("bad", ["relative/dir", "/a\nb", '/a"b', "/a b/../", ""])
def test_request_rejects_a_malformed_master_dir(bad):
    with pytest.raises(ValueError):
        DocumentLoadRequest(text="theory T imports Main begin end", master_dir=bad)


def test_request_accepts_an_absolute_directory():
    r = DocumentLoadRequest(text="theory T imports Main begin end", master_dir="/work/a-b_c.d/top")
    assert r.master_dir == "/work/a-b_c.d/top"
