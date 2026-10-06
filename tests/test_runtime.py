from pathlib import Path

from agentpms.agent import QueuedDecisionModel
from agentpms.config import Settings
from agentpms.pms.qloapps import QloAppsAdapter
from agentpms.runtime import build_orchestrator, build_registry


def _settings():
    return Settings(
        rag_project=Path("RAGpms"),
        knowledge_url="http://127.0.0.1:8765/knowledge/query",
        llm_base_url="http://127.0.0.1:8000/v1",
        llm_api_key="EMPTY",
        llm_model="model",
        max_steps=6,
        confirmation_ttl_seconds=300,
    )


def test_runtime_exposes_only_explicit_initial_tools(pms):
    registry = build_registry(pms=pms)

    assert {spec.name for spec in registry.specs()} == {
        "add_extra",
        "check_availability",
        "check_in_reservation",
        "check_out_reservation",
        "create_reservation",
        "create_reservations",
        "get_occupancy",
        "get_occupancy_range",
        "get_reservation",
        "get_reservations",
        "query_stays",
        "get_invoice",
        "issue_invoice",
        "issue_invoices",
        "list_customer_requests",
        "get_customer_request",
        "reply_customer_request",
        "list_arrivals",
        "list_departures",
        "search_reservations",
        "update_reservation",
    }


def test_viewer_cannot_use_write_tool(pms):
    runtime = build_orchestrator(
        settings=_settings(),
        decision_model=QueuedDecisionModel([]),
        pms=pms,
    )

    assert runtime.permissions.allowed_tools["viewer"] == {
        "check_availability",
        "get_occupancy",
        "get_occupancy_range",
        "get_reservation",
        "get_reservations",
        "query_stays",
        "get_invoice",
        "list_customer_requests",
        "get_customer_request",
        "list_arrivals",
        "list_departures",
        "search_reservations",
    }
    assert "add_extra" not in runtime.permissions.allowed_tools["viewer"]
    assert "create_reservation" not in runtime.permissions.allowed_tools["viewer"]
    assert "update_reservation" not in runtime.permissions.allowed_tools["viewer"]
    assert "check_in_reservation" not in runtime.permissions.allowed_tools["viewer"]
    assert "check_out_reservation" not in runtime.permissions.allowed_tools["viewer"]
    assert "issue_invoice" not in runtime.permissions.allowed_tools["viewer"]
    assert "reply_customer_request" not in runtime.permissions.allowed_tools["viewer"]


def test_registry_omits_unavailable_qloapps_writes():
    registry = build_registry(pms=QloAppsAdapter(object()))

    assert {spec.name for spec in registry.specs()} == {
        "check_availability",
        "get_occupancy",
        "get_occupancy_range",
        "get_reservation",
        "get_reservations",
        "query_stays",
        "get_invoice",
        "list_customer_requests",
        "get_customer_request",
        "list_arrivals",
        "list_departures",
        "quote_stay",
        "plan_room_allocation",
        "search_reservations",
    }
