from datetime import date
import json
from types import SimpleNamespace

from openai import APIError

from agentpms.agent.state import AgentState
from agentpms.contracts import RiskLevel, ToolResult, ToolStatus
from agentpms.llm import OpenAICompatibleDecisionModel
from agentpms.tools.base import ToolSpec


class FakeCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=output)
                )
            ]
        )


def _model(outputs):
    model = OpenAICompatibleDecisionModel(
        base_url="http://127.0.0.1:8000/v1",
        api_key="EMPTY",
        model="Qwen/Qwen2.5-14B-Instruct",
        today_provider=lambda: date(2026, 9, 3),
    )
    completions = FakeCompletions(outputs)
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return model, completions


def test_decision_payload_contains_the_current_operational_date():
    model, completions = _model(
        ['{"kind":"tool","tool_name":"get_occupancy",'
         '"arguments":{"stay_date":"2026-09-04"}}']
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="et pour demain ?",
        messages=[{"role": "user", "content": "et pour demain ?"}],
    )

    decision = model.decide(
        state,
        [
            ToolSpec(
                name="get_occupancy",
                description="Occupation d'une nuit.",
                risk=RiskLevel.READ,
                input_schema={"type": "object"},
            )
        ],
    )

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload["current_date"] == "2026-09-03"
    assert payload["timezone"] == "Europe/Paris"
    assert decision.arguments["stay_date"] == "2026-09-04"


def test_invalid_first_output_is_repaired_once():
    model, completions = _model(
        [
            "Je choisirais probablement un outil.",
            '{"kind":"clarify","response":"Quelle date souhaitez-vous ?"}',
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Fais le necessaire",
        messages=[{"role": "user", "content": "Fais le necessaire"}],
    )

    decision = model.decide(state, [])

    assert decision.kind.value == "clarify"
    assert len(completions.calls) == 2
    assert completions.calls[1]["messages"][-2]["role"] == "assistant"
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


def test_first_valid_json_decision_is_extracted_from_multiple_objects():
    model, completions = _model(
        [
            'Texte inutile {"exemple":true}\n'
            '{"kind":"clarify","response":"Quel type de chambre ?"}'
            '\n{"note":"fin"}'
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Prepare la reservation",
        messages=[{"role": "user", "content": "Prepare la reservation"}],
    )

    decision = model.decide(state, [])

    assert decision.kind.value == "clarify"
    assert decision.response == "Quel type de chambre ?"
    assert len(completions.calls) == 1


def test_payload_preserves_tool_observation_order_and_compacts_room_inventory():
    model, completions = _model(
        ['{"kind":"clarify","response":"Quel type de chambre ?"}']
    )
    rooms = [
        {
            "room_id": str(index),
            "room_type_id": str((index - 1) // 5 + 1),
            "available": True,
        }
        for index in range(1, 21)
    ]
    result = ToolResult(
        call_id="availability-1",
        tool_name="check_availability",
        status=ToolStatus.SUCCESS,
        message="20 chambres disponibles.",
        data={
            "arrival_date": "2026-09-08",
            "departure_date": "2026-09-12",
            "rooms": rooms,
            "room_types": [
                {"room_type_id": str(index), "name": f"Type {index}"}
                for index in range(1, 5)
            ],
        },
        verified=True,
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Prends la moins chere",
        messages=[
            {"role": "user", "content": "Prepare une reservation"},
            {"role": "tool", "content": result.model_dump_json()},
            {"role": "user", "content": "Prends la moins chere"},
        ],
        tool_results=[result],
    )

    model.decide(state, [])

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert [message["role"] for message in payload["conversation"]] == [
        "user",
        "tool",
        "user",
    ]
    observation = payload["conversation"][1]["observation"]
    assert len(observation["data"]["rooms"]) == 4
    assert observation["call_id"] == "availability-1"
    assert completions.calls[0]["max_tokens"] == 450


def test_payload_exposes_public_workflow_progress_to_adapt_the_next_decision():
    model, completions = _model(
        [
            '{"kind":"tool","tool_name":"issue_invoice",'
            '"arguments":{"reservation_id":"42"},'
            '"plan":["issue_invoice"]}'
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Edite la facture de la reservation 42",
        messages=[
            {"role": "user", "content": "Edite la facture de la reservation 42"}
        ],
        workflow_plan=["get_reservation", "issue_invoice"],
        completed_plan_steps=["get_reservation"],
    )

    decision = model.decide(
        state,
        [
            ToolSpec(
                name="issue_invoice",
                description="Emet la facture.",
                risk=RiskLevel.SENSITIVE_WRITE,
                input_schema={"type": "object"},
            )
        ],
    )

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload["workflow"]["plan"] == [
        "get_reservation",
        "issue_invoice",
    ]
    assert payload["workflow"]["completed_tools"] == ["get_reservation"]
    assert payload["workflow"]["goal_revision"] == 0
    assert decision.plan == ["issue_invoice"]


def test_only_remaining_planned_tools_keep_their_full_schema_after_a_step():
    model, completions = _model(
        [
            '{"kind":"tool","tool_name":"issue_invoice",'
            '"arguments":{"reservation_id":"42"}}'
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Emets la facture.",
        messages=[{"role": "user", "content": "Emets la facture."}],
        workflow_plan=["get_reservation", "issue_invoice"],
        completed_plan_steps=["get_reservation"],
        step_count=1,
    )
    tools = [
        ToolSpec(
            name="get_reservation",
            description="Charge une reservation.",
            risk=RiskLevel.READ,
            input_schema={"title": "Read", "type": "object"},
        ),
        ToolSpec(
            name="issue_invoice",
            description="Emet la facture.",
            risk=RiskLevel.SENSITIVE_WRITE,
            input_schema={
                "title": "Invoice",
                "type": "object",
                "properties": {
                    "reservation_id": {"title": "Id", "type": "string"}
                },
            },
        ),
    ]

    model.decide(state, tools)

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert {tool["name"] for tool in payload["available_tools"]} == {
        "get_reservation",
        "issue_invoice",
    }
    assert [tool["name"] for tool in payload["tools_with_input_schemas"]] == [
        "issue_invoice"
    ]
    assert "title" not in payload["tools_with_input_schemas"][0]["input_schema"]
    assert "title" not in payload["tools_with_input_schemas"][0]["input_schema"][
        "properties"
    ]["reservation_id"]


def test_a_failed_tool_uses_the_compact_catalog_for_replanning():
    model, completions = _model(
        [
            '{"kind":"tool","tool_name":"issue_invoice",'
            '"arguments":{"reservation_id":"42"}}'
        ]
    )
    failed = ToolResult(
        call_id="missing-invoice",
        tool_name="get_invoice",
        status=ToolStatus.NOT_FOUND,
        message="Facture inexistante.",
        error_code="invoice_not_issued",
        verified=True,
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Emets la facture.",
        messages=[
            {"role": "user", "content": "Emets la facture."},
            {"role": "tool", "content": failed.model_dump_json()},
        ],
        tool_results=[failed],
        workflow_plan=["get_invoice"],
        step_count=1,
    )
    tools = [
        ToolSpec(
            name="get_invoice",
            description="Consulte une facture.",
            risk=RiskLevel.READ,
            input_schema={
                "type": "object",
                "properties": {"reservation_id": {"type": "string"}},
                "required": ["reservation_id"],
            },
        ),
        ToolSpec(
            name="issue_invoice",
            description="Emet une facture.",
            risk=RiskLevel.SENSITIVE_WRITE,
            input_schema={
                "type": "object",
                "properties": {"reservation_id": {"type": "string"}},
                "required": ["reservation_id"],
            },
        ),
    ]

    model.decide(state, tools)

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload["tools_with_input_schemas"] == []
    catalog = {tool["name"]: tool for tool in payload["available_tools"]}
    assert set(catalog) == {
        "get_invoice",
        "issue_invoice",
    }
    assert catalog["issue_invoice"]["arguments"] == {
        "fields": {"reservation_id": {"type": "string"}},
        "required": ["reservation_id"],
    }


def test_first_decision_of_a_followup_keeps_only_remaining_full_schemas():
    model, completions = _model(
        [
            '{"kind":"tool","tool_name":"create_reservation",'
            '"arguments":{"guest_first_name":"Bruno"}}'
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Prends la plus chere.",
        messages=[{"role": "user", "content": "Prends la plus chere."}],
        workflow_plan=["check_availability", "quote_stay", "create_reservation"],
        completed_plan_steps=["check_availability", "quote_stay"],
        step_count=0,
    )
    tools = [
        ToolSpec(
            name="check_availability",
            description="Verifie les chambres.",
            risk=RiskLevel.READ,
            input_schema={"type": "object"},
        ),
        ToolSpec(
            name="quote_stay",
            description="Prepare un devis.",
            risk=RiskLevel.READ,
            input_schema={"type": "object"},
        ),
        ToolSpec(
            name="create_reservation",
            description="Cree une reservation.",
            risk=RiskLevel.SENSITIVE_WRITE,
            input_schema={
                "type": "object",
                "properties": {"guest_first_name": {"type": "string"}},
            },
        ),
    ]

    model.decide(state, tools)

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert [tool["name"] for tool in payload["tools_with_input_schemas"]] == [
        "create_reservation"
    ]
    assert {tool["name"] for tool in payload["available_tools"]} == {
        "check_availability",
        "quote_stay",
        "create_reservation",
    }


def test_provider_memory_error_is_retried_with_a_compact_payload():
    provider_error = APIError(
        "Generation impossible.",
        request=None,
        body={"error": {"message": "Generation impossible."}},
    )
    model, completions = _model(
        [
            provider_error,
            '{"kind":"clarify","response":"Quelle categorie choisissez-vous ?"}',
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Prends la plus chere.",
        messages=[{"role": "user", "content": "Prends la plus chere."}],
    )

    decision = model.decide(state, [])

    assert decision.response == "Quelle categorie choisissez-vous ?"
    assert len(completions.calls) == 2
    retry_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert retry_payload["context_mode"] == "compact_retry_after_provider_failure"
    assert completions.calls[1]["max_tokens"] == 320


def test_payload_exposes_structured_working_memory_for_short_followups():
    model, completions = _model(
        ['{"kind":"tool","tool_name":"create_reservation","arguments":{}}']
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Prends finalement la plus chere",
        messages=[
            {"role": "user", "content": "Prepare une reservation"},
            {
                "role": "user",
                "content": "Prends finalement la plus chere",
                "turn_index": 1,
            },
        ],
        turn_index=1,
        working_memory={
            "objective": "Creer une reservation",
            "known_facts": {"adults": 1},
            "constraints": ["selectionner l'offre la plus chere"],
            "selected_references": {},
        },
    )

    model.decide(
        state,
        [
            ToolSpec(
                name="create_reservation",
                description="Cree une reservation.",
                risk=RiskLevel.SENSITIVE_WRITE,
                input_schema={"type": "object"},
            )
        ],
    )

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    assert payload["working_memory"]["objective"] == "Creer une reservation"
    assert payload["working_memory"]["known_facts"] == {"adults": 1}
    assert payload["conversation"][-1]["turn_index"] == 1


def test_occupancy_range_is_grouped_instead_of_sending_every_day():
    model, completions = _model(
        ['{"kind":"final","response":"Occupation verifiee."}']
    )
    days = [
        {
            "stay_date": f"2026-09-{day:02d}",
            "total_rooms": 20,
            "occupied_rooms": 4 if day < 16 else 6,
            "available_rooms": 16 if day < 16 else 14,
            "occupancy_rate_percent": 20.0 if day < 16 else 30.0,
        }
        for day in range(1, 31)
    ]
    result = ToolResult(
        call_id="occupancy-month",
        tool_name="get_occupancy_range",
        status=ToolStatus.SUCCESS,
        message="Occupation calculee.",
        data={
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "average_occupancy_rate_percent": 25.0,
            "days": days,
        },
        verified=True,
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Quel est le taux du mois ?",
        messages=[
            {"role": "user", "content": "Quel est le taux du mois ?"},
            {"role": "tool", "content": result.model_dump_json()},
        ],
    )

    model.decide(state, [])

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    data = payload["conversation"][1]["observation"]["data"]
    assert "days" not in data
    assert data["day_count"] == 30
    assert len(data["occupancy_runs"]) == 2
    assert data["average_occupancy_rate_percent"] == 25.0


def test_observations_from_a_previous_goal_keep_only_a_small_audit_summary():
    model, completions = _model(
        ['{"kind":"final","response":"Nouvel objectif traite."}']
    )
    result = ToolResult(
        call_id="old-availability",
        tool_name="check_availability",
        status=ToolStatus.SUCCESS,
        message="20 chambres disponibles.",
        data={"rooms": [{"room_id": str(index)} for index in range(100)]},
        verified=True,
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Nouvelle demande",
        intent_revision=2,
        messages=[
            {
                "role": "tool",
                "content": result.model_dump_json(),
                "goal_revision": 1,
            },
            {
                "role": "user",
                "content": "Nouvelle demande",
                "turn_index": 2,
                "goal_revision": 2,
            },
        ],
    )

    model.decide(state, [])

    payload = json.loads(completions.calls[0]["messages"][1]["content"])
    observation = payload["conversation"][0]["observation"]
    assert observation["historical_goal"] is True
    assert "data" not in observation
    assert observation["call_id"] == "old-availability"
