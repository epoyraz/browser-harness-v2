"""Application perception: apply links, apply controls, ATS routes, the verdict.

Moved from `test_forms.py`. What stayed there is factual — which controls exist, what they
are labelled, whether the page has a submit. Scoring a link as an apply link, and deciding
a form is an application rather than a login, is knowledge about recruiting software.
"""

from applications.document import (
    application_route_candidates,
    prepare_document,
)
from tests.unit.conftest import _evaluates


def test_application_route_candidates_encodes_ashby_capability():
    posting = "https://jobs.ashbyhq.com/acme/ebd97901-59be-4655-ad13-fcfa8ca17987"
    assert application_route_candidates(posting) == [posting + "/application"]
    assert application_route_candidates(posting + "/application") == []
    assert application_route_candidates("https://example.com/acme/123") == []


def test_prepare_document_batches_metadata_schema_and_file_refs(tab):
    browser, t = tab
    payload = {"schema": {"verdict": {"is_form": True}, "fields": [], "files": ["cv"]},
               "url": "https://a.test/apply", "title": "Apply", "language": "en",
               "file_inputs": [{"ref": "e1", "name": "cv", "accept": ".pdf"}],
               "apply_link": None}
    browser.eval_hook = lambda expression: payload
    before = len(_evaluates(browser))
    assert prepare_document(t) == payload
    assert len(_evaluates(browser)) - before == 1



def test_prepare_source_has_a_bounded_structured_application_route_tier():
    from applications.document import _PREPARE_JS

    assert "applicationUrls.slice(0, 12)" in _PREPARE_JS
    assert "visited++ > 5000" in _PREPARE_JS
    assert "const urlShaped" in _PREPARE_JS
    assert "if (!raw || /[<>\"'\\s]/.test(raw)) return" in _PREPARE_JS


def test_prepare_source_reports_file_input_requirement():
    from applications.document import _PREPARE_JS

    assert "required: !!el.required || labelText.includes('*')" in _PREPARE_JS
