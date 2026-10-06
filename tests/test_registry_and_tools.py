from __future__ import annotations

from agentpms.contracts import ToolCall, ToolResult, ToolStatus
from agentpms.tools.base import ToolContext
from agentpms.tools.pms_tools import SearchReservationsTool
from agentpms.tools.registry import ToolRegistry


def _context(call_id: str) -> ToolContext:
    return ToolContext(
        request_id="request",
        call_id=call_id,
        actor_id="user",
        role="reception",
    )


def test_registry_rejects_unknown_tool():
    registry = ToolRegistry()
    call = ToolCall(name="delete_everything", arguments={})

    result = registry.invoke(call, _context(call.call_id))

    assert result.status == ToolStatus.ERROR
    assert result.error_code == "unknown_tool"


def test_registry_validates_arguments_before_api_call(pms):
    registry = ToolRegistry([SearchReservationsTool(pms)])
    call = ToolCall(name="search_reservations", arguments={})

    result = registry.invoke(call, _context(call.call_id))

    assert result.status == ToolStatus.ERROR
    assert result.error_code == "invalid_arguments"


def test_registry_refuses_duplicate_tool_names(pms):
    registry = ToolRegistry([SearchReservationsTool(pms)])

    try:
        registry.register(SearchReservationsTool(pms))
    except ValueError as exc:
        assert "deja enregistre" in str(exc)
    else:
        raise AssertionError("Le doublon aurait du etre refuse")

