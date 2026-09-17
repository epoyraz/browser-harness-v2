"""Check the MCP protocol envelope, not just JSON inside its text content."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from mcp_types import CallToolRequestParams

import mcp_server
from harness.core.outcome import Class, ElementGone, fail


@pytest.mark.parametrize("failure", ["typed", "unexpected", "dict", "outcome", "success"])
def test_operational_result_sets_protocol_error_and_keeps_evidence(monkeypatch, failure):
    def click(*args, **kwargs):
        if failure == "typed":
            raise ElementGone("missing test element", ref="e999")
        if failure == "unexpected":
            raise ValueError("invalid test input")
        if failure == "dict":
            return {"ok": False, "class": "value_rejected"}
        if failure == "outcome":
            return fail(Class.VALUE_REJECTED, "rejected")
        return {"ok": True, "observed": {"clicked": True}}

    monkeypatch.setattr(mcp_server, "_ns", lambda: {"click_ref": click})
    monkeypatch.setattr(mcp_server, "_session", lambda: SimpleNamespace(
        _bound_agent_value=lambda name, value: value))
    result = asyncio.run(mcp_server.SERVER._handle_call_tool(
        None, CallToolRequestParams(name="browser_click", arguments={"ref": "e999"})))
    value = json.loads(result.content[0].text)
    assert result.is_error is (failure != "success")
    assert value["ok"] is (failure == "success")
    if failure == "typed":
        assert value["class"] == "element_gone"
        assert value["observed"] == {"ref": "e999"}
        assert value["retryable"] is False and value["recovery"]
    if failure == "unexpected":
        assert value["class"] == "tool_error"
