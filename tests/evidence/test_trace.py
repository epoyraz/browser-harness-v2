"""Trace renderer tests. The one that earns its keep: cdp=61 on the line, no benchmark."""
import pytest

from evidence.trace import render
from harness.core.journal import Journal
from harness.core.outcome import NavigationFailed


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "s.jsonl", session="s1")


def test_waste_is_visible_without_a_benchmark(j):
    """TODO 26's done-when: v1's fill_input spent 61 round trips on 20 characters and
    nothing surfaced it. Here the count sits on the line."""
    with j.call("fill_input", selector="#email"):
        for _ in range(61):
            j.cdp("Input.dispatchKeyEvent")
    line = render(j.entries())[0]
    assert "cdp=61" in line and "fill_input" in line


def test_nested_spans_render_as_an_indented_tree(j):
    with j.call("fill_form", n=7), j.call("js", expression="((plan)=>{...})"):
        j.cdp("Runtime.evaluate")
    lines = render(j.entries())
    assert lines[0].startswith("fill_form") and "cdp=0" in lines[0]
    assert lines[1].startswith("  js") and "cdp=1" in lines[1]
    # the tree names the layer that spent the round trip — parent 0, child 1


def test_a_failure_renders_its_class_and_detail(j):
    with pytest.raises(NavigationFailed), j.call("goto", url="https://x/careers"):
        raise NavigationFailed("net::ERR_HTTP_RESPONSE_CODE_FAILURE",
                               landed="chrome-error://chromewebdata/")
    line = render(j.entries())[0]
    assert "FAIL navigation_failed" in line
    assert "ERR_HTTP_RESPONSE_CODE_FAILURE" in line


def test_tail_keeps_the_last_n_top_level_spans(j):
    for i in range(5):
        with j.call(f"step{i}"):
            pass
    lines = render(j.entries(), tail=2)
    assert len(lines) == 2
    assert lines[0].startswith("step3") and lines[1].startswith("step4")


def test_spans_render_in_allocation_order_not_file_order(j):
    """Entries close inner-first, so file order is not tree order."""
    with j.call("outer"), j.call("inner"):
        pass
    with j.call("later"):
        pass
    lines = render(j.entries())
    assert [ln.split()[0] for ln in lines] == ["outer", "inner", "later"]


def test_non_call_entries_are_ignored(j):
    j.write("note", msg="chatter")
    j.write("daemon", event="attached")
    with j.call("goto", url="x"):
        pass
    assert len(render(j.entries())) == 1


def test_success_is_silent_shaped_ok_not_prose(j):
    with j.call("snapshot"):
        j.cdp("Runtime.evaluate")
    line = render(j.entries())[0]
    assert line.rstrip().endswith("ok")
