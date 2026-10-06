from datetime import date
import json
from types import SimpleNamespace

from openai import APIError

from agentpms.agent.state import AgentState
from agentpms.contracts import RiskLevel
from agentpms.llm import DeepSeekDecisionModel
from agentpms.llm.deepseek import CONTROL_ARGUMENT, RESPOND_FUNCTION
from agentpms.tools.base import ToolSpec
from agentpms.tools.pms_tools import CreateReservationsInput


class FakeCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return SimpleNamespace(choices=[SimpleNamespace(message=output)])


def _control(**updates):
    value = {
        "plan": [],
        "required_tools": [],
        "memory_updated": False,
        "memory": {
            "objective": "",
            "summary": "",
            "known_facts": [],
            "constraints": [],
            "unavailable_fields": [],
            "selected_references": [],
        },
        "new_goal": False,
        "respond_with_tool_result": False,
        "room_selection_strategy": "",
        "room_selection_evidence": "",
    }
    value.update(updates)
    return value


def _message(function_name, arguments):
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(
                    name=function_name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                )
            )
        ],
    )


def _model(outputs):
    model = DeepSeekDecisionModel(
        base_url="https://api.deepseek.com/beta",
        api_key="test-key",
        model="deepseek-v4-flash",
        today_provider=lambda: date(2026, 9, 5),
    )
    completions = FakeCompletions(outputs)
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return model, completions


def _occupancy_tool():
    return ToolSpec(
        name="get_occupancy",
        description="Consulte le taux d'occupation d'une nuit.",
        risk=RiskLevel.READ,
        input_schema={
            "title": "GetOccupancyInput",
            "type": "object",
            "properties": {
                "stay_date": {
                    "title": "Stay Date",
                    "type": "string",
                    "format": "date",
                }
            },
            "required": ["stay_date"],
        },
    )


def test_deepseek_uses_a_native_function_call_for_the_next_tool():
    control = _control(
        plan=["get_occupancy"],
        required_tools=["get_occupancy"],
        respond_with_tool_result=True,
    )
    model, completions = _model(
        [
            _message(
                "get_occupancy",
                {"stay_date": "2026-09-06", CONTROL_ARGUMENT: control},
            )
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Quel est le taux pour demain ?",
        messages=[
            {"role": "user", "content": "Quel est le taux pour demain ?"}
        ],
    )

    decision = model.decide(state, [_occupancy_tool()])

    assert decision.tool_name == "get_occupancy"
    assert decision.arguments == {"stay_date": "2026-09-06"}
    assert decision.respond_with_tool_result is True
    call = completions.calls[0]
    # DeepSeek V4 refuse tool_choice en mode thinking. Le prompt impose un
    # appel unique et la validation locale rejette toute autre sortie.
    assert "tool_choice" not in call
    assert "parallel_tool_calls" not in call
    assert "response_format" not in call
    function = call["tools"][0]["function"]
    assert function["name"] == "get_occupancy"
    assert function["strict"] is True
    assert CONTROL_ARGUMENT in function["parameters"]["required"]
    stay_date = function["parameters"]["properties"]["stay_date"]
    assert "format" not in stay_date
    assert "AAAA-MM-JJ" in stay_date["description"]


def test_deepseek_response_function_preserves_structured_memory():
    control = _control(
        memory_updated=True,
        memory={
            "objective": "Creer une reservation",
            "summary": "Le client veut la chambre la plus chere.",
            "known_facts": [
                {"name": "adults", "value_json": "1"},
                {"name": "breakfast", "value_json": "true"},
            ],
            "constraints": ["choisir la plus chere"],
            "unavailable_fields": ["email"],
            "selected_references": [
                {"name": "room_strategy", "value": "most_expensive"}
            ],
        },
    )
    model, _ = _model(
        [
            _message(
                RESPOND_FUNCTION,
                {
                    "response_type": "clarify",
                    "response": "Quelle date de départ souhaitez-vous ?",
                    CONTROL_ARGUMENT: control,
                },
            )
        ]
    )
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Fais la réservation",
        messages=[{"role": "user", "content": "Fais la réservation"}],
    )

    decision = model.decide(state, [_occupancy_tool()])

    assert decision.kind.value == "clarify"
    assert decision.memory.objective == "Creer une reservation"
    assert decision.memory.known_facts == {"adults": 1, "breakfast": True}
    assert decision.memory.unavailable_fields == ["email"]


def test_deepseek_retries_one_invalid_native_decision():
    invalid = SimpleNamespace(content="texte", tool_calls=None)
    valid = _message(
        RESPOND_FUNCTION,
        {
            "response_type": "final",
            "response": "Demande terminée.",
            CONTROL_ARGUMENT: _control(),
        },
    )
    model, completions = _model([invalid, valid])
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Termine",
        messages=[{"role": "user", "content": "Termine"}],
    )

    decision = model.decide(state, [])

    assert decision.kind.value == "final"
    assert len(completions.calls) == 2
    retry_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert retry_payload["context_mode"] == "compact_retry"
    assert "previous_decision_error" in retry_payload


def test_deepseek_retries_a_provider_error_with_compact_context():
    provider_error = APIError(
        "Service indisponible",
        request=None,
        body={"error": {"message": "Service indisponible"}},
    )
    valid = _message(
        RESPOND_FUNCTION,
        {
            "response_type": "final",
            "response": "Service rétabli.",
            CONTROL_ARGUMENT: _control(),
        },
    )
    model, completions = _model([provider_error, valid])
    state = AgentState(
        actor_id="reception",
        role="reception",
        user_message="Bonjour",
        messages=[{"role": "user", "content": "Bonjour"}],
    )

    decision = model.decide(state, [])

    assert decision.response == "Service rétabli."
    assert len(completions.calls) == 2
    retry_payload = json.loads(completions.calls[1]["messages"][1]["content"])
    assert retry_payload["context_mode"] == "compact_retry"


def test_deepseek_inlines_nested_pydantic_schemas_for_strict_tools():
    tool = ToolSpec(
        name="create_reservations",
        description="Cree plusieurs reservations.",
        risk=RiskLevel.SENSITIVE_WRITE,
        input_schema=CreateReservationsInput.model_json_schema(),
    )

    parameters = DeepSeekDecisionModel._function_tools([tool])[0]["function"][
        "parameters"
    ]
    encoded = json.dumps(parameters)

    assert "$defs" not in encoded
    assert "$ref" not in encoded
    item_schema = parameters["properties"]["reservations"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == set(item_schema["properties"])
