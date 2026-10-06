from datetime import date, timedelta
from decimal import Decimal
import os
from pathlib import Path

import pytest

from agentpms.agent import LocalRulesDecisionModel
from agentpms.config import Settings
from agentpms.contracts import (
    AgentRunStatus,
    ToolCall,
    ToolStatus,
)
from agentpms.pms.models import PMSStatus
from agentpms.pms.qloapps import QloAppsAdapter, QloAppsClient
from agentpms.runtime import build_orchestrator, build_registry
from agentpms.tools.base import ToolContext


pytestmark = pytest.mark.integration


def _client():
    api_key = os.getenv("AGENTPMS_QLOAPPS_API_KEY", "")
    if not api_key:
        pytest.skip("AGENTPMS_QLOAPPS_API_KEY non configuree")
    return QloAppsClient(
        base_url=os.getenv(
            "AGENTPMS_QLOAPPS_BASE_URL", "http://127.0.0.1:8088/api"
        ),
        api_key=api_key,
    )


def _write_client():
    api_key = os.getenv("AGENTPMS_QLOAPPS_WRITE_API_KEY", "")
    if not api_key:
        pytest.skip("AGENTPMS_QLOAPPS_WRITE_API_KEY non configuree")
    return QloAppsClient(
        base_url=os.getenv(
            "AGENTPMS_QLOAPPS_BASE_URL", "http://127.0.0.1:8088/api"
        ),
        api_key=api_key,
    )


def test_local_qloapps_resource_contract():
    resources = _client().discover_resources()

    assert {
        "bookings",
        "customer_messages",
        "customer_threads",
        "hotel_ari",
        "hotel_rooms",
        "order_details",
        "order_invoices",
        "orders",
        "room_types",
    } <= resources
    assert "employees" not in resources


def test_local_qloapps_write_key_is_restricted_to_bounded_write_resources():
    assert _write_client().discover_resources() == {
        "bookings",
        "customer_messages",
        "room_bookings",
    }


def test_local_qloapps_availability_contract():
    tomorrow = date.today() + timedelta(days=1)
    result = _client().query_availability(
        hotel_id=1,
        arrival_date=tomorrow,
        departure_date=tomorrow + timedelta(days=2),
    )

    assert result["id_hotel"] in {1, "1"}
    assert isinstance(result.get("room_types"), list)


def test_local_qloapps_demo_fixture_scenarios():
    if os.getenv("AGENTPMS_QLOAPPS_EXPECT_DEMO_FIXTURES") != "1":
        pytest.skip("Jeu de demonstration QloApps non requis")
    adapter = QloAppsAdapter(_client())

    jean = adapter.search_reservations(guest_name="Jean Dupont")
    dupont = adapter.search_reservations(guest_name="Dupont")
    marie = adapter.search_reservations(guest_name="Marie Martin")
    arrivals = adapter.list_arrivals(arrival_date=date(2026, 10, 10))
    occupancy = adapter.get_occupancy(stay_date=date(2026, 10, 10))
    occupancy_range = adapter.get_occupancy_range(
        start_date=date(2026, 10, 10),
        end_date=date(2026, 10, 17),
    )
    availability = adapter.check_availability(
        arrival_date=date(2026, 10, 10),
        departure_date=date(2026, 10, 12),
    )
    customer_requests = adapter.list_customer_requests(status="open")
    invoice = adapter.get_invoice(reservation_id="1")

    assert jean.status == PMSStatus.SUCCESS
    assert dupont.status == PMSStatus.AMBIGUOUS
    assert len(dupont.data["reservations"]) == 2
    assert marie.status == PMSStatus.SUCCESS
    assert marie.data["reservation"]["extras"][0]["code"] == "SERVICE-9"
    assert arrivals.status == PMSStatus.SUCCESS
    assert "Jean Dupont" in [
        item["guest_name"] for item in arrivals.data["reservations"]
    ]
    assert occupancy.status == PMSStatus.SUCCESS
    assert occupancy.data["total_rooms"] == 20
    assert 1 <= occupancy.data["occupied_rooms"] <= 20
    assert occupancy.data["occupancy_rate_percent"] == (
        occupancy.data["occupied_rooms"] / occupancy.data["total_rooms"] * 100
    )
    assert occupancy_range.status == PMSStatus.SUCCESS
    occupied_by_day = [
        day["occupied_rooms"] for day in occupancy_range.data["days"]
    ]
    assert all(
        actual >= baseline
        for actual, baseline in zip(
            occupied_by_day,
            [1, 2, 1, 0, 0, 1, 1, 0],
            strict=True,
        )
    )
    assert availability.status == PMSStatus.SUCCESS
    assert 0 < len(availability.data["rooms"]) <= 18
    assert customer_requests.status == PMSStatus.SUCCESS
    assert customer_requests.verified is True
    assert customer_requests.data["requests"][0]["subject"] == "Arrivee tardive"
    assert customer_requests.data["requests"][0]["messages"][0]["sender"] == "customer"
    assert invoice.status == PMSStatus.NOT_FOUND
    assert invoice.verified is True
    assert invoice.error_code == "invoice_not_issued"


def test_local_qloapps_demo_write_is_available_but_not_executed_without_confirmation():
    if os.getenv("AGENTPMS_QLOAPPS_EXPECT_DEMO_FIXTURES") != "1":
        pytest.skip("Jeu de demonstration QloApps non requis")
    adapter = QloAppsAdapter(_client(), write_client=_write_client())

    preview = adapter.preview_add_extra(
        reservation_id="1",
        extra_code="BREAKFAST",
        label="Petit-dejeuner",
        quantity=1,
        unit_price=None,
    )
    booking_preview = adapter.preview_create_reservation(
        guest_first_name="Pierre",
        guest_last_name="Delafitte",
        email="pierre.delafitte@agentpms.test",
        phone="0600000000",
        arrival_date=date(2026, 11, 2),
        departure_date=date(2026, 11, 10),
        adults=2,
        children=0,
        room_type_id="1",
        room_id="1",
    )

    assert "total 840.00 EUR TTC" in preview
    assert "12000.00 EUR TTC" in booking_preview
    assert "add_extra" in adapter.capabilities()
    assert "create_reservation" in adapter.capabilities()
    assert "issue_invoice" in adapter.capabilities()
    assert "reply_customer_request" in adapter.capabilities()


def test_local_qloapps_builds_the_requested_quote_without_writing():
    if os.getenv("AGENTPMS_QLOAPPS_EXPECT_DEMO_FIXTURES") != "1":
        pytest.skip("Jeu de demonstration QloApps non requis")
    adapter = QloAppsAdapter(_client())

    quote = adapter.quote_stay(
        arrival_date=date(2026, 9, 8),
        departure_date=date(2026, 9, 12),
        adults=2,
        children=0,
        include_breakfast=True,
    )

    assert quote.status == PMSStatus.SUCCESS
    assert quote.verified is True
    assert quote.data["breakfast"]["per_night_with_tax"] == "840.00"
    assert quote.data["room_quotes"]
    first = quote.data["room_quotes"][0]
    total_per_night = Decimal(first["total_per_night_excluding_tourist_tax"])
    assert total_per_night == (
        Decimal(first["room_per_night_with_tax"]) + Decimal("840.00")
    )
    assert Decimal(first["total_stay_excluding_tourist_tax"]) == (
        total_per_night * 4
    )
    assert quote.data["tourist_tax"]["included_in_totals"] is False


def test_local_qloapps_agent_stops_before_write_for_exact_confirmation():
    if os.getenv("AGENTPMS_QLOAPPS_EXPECT_DEMO_FIXTURES") != "1":
        pytest.skip("Jeu de demonstration QloApps non requis")
    adapter = QloAppsAdapter(_client(), write_client=_write_client())
    settings = Settings(
        rag_project=Path("RAGpms"),
        knowledge_url="http://127.0.0.1:8765/knowledge/query",
        llm_base_url="http://127.0.0.1:8000/v1",
        llm_api_key="EMPTY",
        llm_model="model",
        max_steps=6,
        confirmation_ttl_seconds=300,
    )
    before = adapter.get_reservation("1").data["reservation"]["extras"]
    orchestrator = build_orchestrator(
        settings=settings,
        decision_model=LocalRulesDecisionModel(),
        pms=adapter,
    )

    pending = orchestrator.start(
        "Ajoute un petit-dejeuner a la reservation de Jean Dupont.",
        actor_id="integration-user",
    )

    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert "total 840.00 EUR TTC" in pending.message
    assert adapter.get_reservation("1").data["reservation"]["extras"] == before


def test_local_qloapps_demo_is_reachable_through_agent_tool_registry():
    if os.getenv("AGENTPMS_QLOAPPS_EXPECT_DEMO_FIXTURES") != "1":
        pytest.skip("Jeu de demonstration QloApps non requis")
    registry = build_registry(pms=QloAppsAdapter(_client()))
    call = ToolCall(
        name="search_reservations",
        arguments={"guest_name": "Jean Dupont"},
    )
    context = ToolContext(
        request_id="integration-demo",
        call_id=call.call_id,
        actor_id="test-user",
        role="reception",
    )

    result = registry.invoke(call, context)

    assert result.status == ToolStatus.SUCCESS
    assert result.verified is True
    assert result.data["reservation"]["guest_name"] == "Jean Dupont"
