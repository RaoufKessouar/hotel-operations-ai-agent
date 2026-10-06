from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from agentpms.agent import LocalRulesDecisionModel, QueuedDecisionModel
from agentpms.api import create_app
from agentpms.config import Settings
from agentpms.contracts import AgentDecision, DecisionKind
from agentpms.observability import InMemoryTraceSink
from agentpms.runtime import build_orchestrator


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


def _client(pms):
    trace = InMemoryTraceSink()
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(),
        pms=pms,
        trace_sink=trace,
    )
    app = create_app(
        orchestrator=orchestrator,
        pms=pms,
        trace_sink=trace,
        decision_mode="local_rules",
    )
    return TestClient(app)


def _confirmation_client(pms):
    trace = InMemoryTraceSink()
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=QueuedDecisionModel(
            [
                AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="add_extra",
                    arguments={
                        "reservation_id": "R-4587",
                        "extra_code": "BREAKFAST",
                        "label": "Petit-dejeuner",
                        "quantity": 1,
                        "unit_price": "15.00",
                    },
                ),
                AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="Service ajoute et verifie.",
                ),
            ]
        ),
        pms=pms,
        trace_sink=trace,
    )
    app = create_app(
        orchestrator=orchestrator,
        pms=pms,
        trace_sink=trace,
        decision_mode="test",
    )
    return TestClient(app)


def test_interface_and_health_are_served_locally(pms):
    client = _client(pms)

    page = client.get("/")
    health = client.get("/api/health")

    assert page.status_code == 200
    assert "Poste de réception" in page.text
    assert health.json()["status"] == "ready"
    assert "add_extra" in health.json()["capabilities"]


def test_api_runs_a_natural_language_request_and_exposes_safe_trace(pms):
    client = _client(pms)

    response = client.post(
        "/api/requests",
        json={
            "message": "Trouve la réservation de Jean Dupont",
            "actor_id": "browser-user",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert "Jean Dupont" in payload["message"]
    trace = client.get(f"/api/requests/{payload['request_id']}/trace").json()
    assert {event["event_type"] for event in trace["events"]} >= {
        "request_started",
        "decision",
        "tool_finished",
    }


def test_api_understands_arrivals_of_the_day(pms):
    trace = InMemoryTraceSink()
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 12)
        ),
        pms=pms,
        trace_sink=trace,
    )
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            pms=pms,
            trace_sink=trace,
            decision_mode="local_rules",
        )
    )

    response = client.post(
        "/api/requests",
        json={
            "message": "quelle sont toutes les arrivées du jours?",
            "actor_id": "browser-user",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert "Jean Dupont" in payload["message"]
    assert payload["tool_results"][0]["tool_name"] == "list_arrivals"


def test_api_continues_an_occupancy_conversation_after_a_completed_turn(pms):
    trace = InMemoryTraceSink()
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 2)
        ),
        pms=pms,
        trace_sink=trace,
    )
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            pms=pms,
            trace_sink=trace,
            decision_mode="local_rules",
        )
    )
    first = client.post(
        "/api/requests",
        json={
            "message": "quel est le taux d occupation ce soir?",
            "actor_id": "browser-user",
        },
    ).json()

    continued = client.post(
        f"/api/requests/{first['request_id']}/messages",
        json={
            "message": "et pour le reste du mois de septembre?",
            "actor_id": "browser-user",
        },
    ).json()

    assert first["status"] == "completed"
    assert continued["status"] == "completed"
    assert continued["tool_results"][-1]["tool_name"] == "get_occupancy_range"
    assert "2026-09-30" in continued["message"]


def test_api_recognizes_booking_creation_and_requests_missing_details(pms):
    trace = InMemoryTraceSink()
    orchestrator = build_orchestrator(
        settings=_settings(),
        decision_model=LocalRulesDecisionModel(
            today_provider=lambda: date(2026, 9, 2)
        ),
        pms=pms,
        trace_sink=trace,
    )
    client = TestClient(
        create_app(
            orchestrator=orchestrator,
            pms=pms,
            trace_sink=trace,
            decision_mode="local_rules",
        )
    )

    response = client.post(
        "/api/requests",
        json={
            "message": (
                "cree une resa pour pierre delafitte de ce soir jusqua 10/09 "
                "pour 2 personnes avec petit dej"
            ),
            "actor_id": "browser-user",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_clarification"
    assert "adresse email" not in payload["message"]
    assert payload["tool_results"][-1]["tool_name"] == "check_availability"


def test_api_continues_the_same_request_after_clarification(pms):
    client = _client(pms)
    first = client.post(
        "/api/requests",
        json={"message": "Cherche les réservations Dupont", "actor_id": "browser-user"},
    ).json()

    continued = client.post(
        f"/api/requests/{first['request_id']}/messages",
        json={"message": "La réservation R-4587", "actor_id": "browser-user"},
    ).json()

    assert first["status"] == "needs_clarification"
    assert continued["status"] == "completed"
    assert "Jean Dupont" in continued["message"]


def test_api_requires_then_accepts_an_exact_confirmation(pms):
    client = _confirmation_client(pms)
    pending = client.post(
        "/api/requests",
        json={"message": "Ajoute le petit-dejeuner", "actor_id": "browser-user"},
    ).json()

    assert pending["status"] == "needs_confirmation"
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []
    completed = client.post(
        f"/api/requests/{pending['request_id']}/confirmation",
        json={
            "confirmation_id": pending["confirmation"]["confirmation_id"],
            "approved": True,
            "actor_id": "browser-user",
        },
    ).json()

    assert completed["status"] == "completed"
    assert completed["tool_results"][-1]["verified"] is True
    assert len(pms.get_reservation("R-4587").data["reservation"]["extras"]) == 1


def test_api_cancellation_performs_no_write(pms):
    client = _confirmation_client(pms)
    pending = client.post(
        "/api/requests",
        json={"message": "Ajoute le petit-dejeuner", "actor_id": "browser-user"},
    ).json()

    denied = client.post(
        f"/api/requests/{pending['request_id']}/confirmation",
        json={
            "confirmation_id": pending["confirmation"]["confirmation_id"],
            "approved": False,
            "actor_id": "browser-user",
        },
    ).json()

    assert denied["status"] == "denied"
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []


def test_api_rejects_confirmation_from_another_browser_identity(pms):
    client = _confirmation_client(pms)
    pending = client.post(
        "/api/requests",
        json={"message": "Ajoute le petit-dejeuner", "actor_id": "browser-user"},
    ).json()

    rejected = client.post(
        f"/api/requests/{pending['request_id']}/confirmation",
        json={
            "confirmation_id": pending["confirmation"]["confirmation_id"],
            "approved": True,
            "actor_id": "another-browser",
        },
    ).json()

    assert rejected["status"] == "error"
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []
