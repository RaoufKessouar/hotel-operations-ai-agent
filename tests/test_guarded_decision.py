from agentpms.agent import GuardedDecisionModel, QueuedDecisionModel
from agentpms.agent.state import AgentMessage, AgentState
from agentpms.contracts import AgentDecision, DecisionKind, ToolResult, ToolStatus


class _ForbiddenSemanticRouter:
    def decide(self, state, tools):
        raise AssertionError("le routeur semantique ne doit jamais etre consulte")


def _state_after_tool(message: str) -> AgentState:
    result = ToolResult(
        call_id="availability-1",
        tool_name="check_availability",
        status=ToolStatus.SUCCESS,
        message="Disponibilites verifiees.",
        data={"room_types": []},
        verified=True,
    )
    return AgentState(
        actor_id="reception",
        role="reception",
        user_message=message,
        messages=[
            AgentMessage(role="user", content="Prepare une reservation"),
            AgentMessage(role="tool", content=result.model_dump_json()),
            AgentMessage(role="user", content=message, turn_index=1),
        ],
        tool_results=[result],
        turn_index=1,
    )


def test_primary_model_keeps_control_after_a_tool_observation():
    expected = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="create_reservation",
        arguments={"guest_first_name": "Maria"},
    )
    model = GuardedDecisionModel(
        QueuedDecisionModel([expected]),
        _ForbiddenSemanticRouter(),
    )

    decision = model.decide(_state_after_tool("Prends la plus chere"), [])

    assert decision == expected


def test_primary_model_interprets_a_new_goal_without_keyword_override():
    expected = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="get_occupancy_range",
        arguments={"start_date": "2026-09-01", "end_date": "2026-09-30"},
        new_goal=True,
    )
    model = GuardedDecisionModel(
        QueuedDecisionModel([expected]),
        _ForbiddenSemanticRouter(),
    )

    decision = model.decide(
        _state_after_tool("Et maintenant donne-moi l'occupation du mois"),
        [],
    )

    assert decision == expected


def test_primary_clarification_is_not_rewritten_by_local_rules():
    expected = AgentDecision(
        kind=DecisionKind.CLARIFY,
        response="Quelle reservation est concernee ?",
    )
    model = GuardedDecisionModel(
        QueuedDecisionModel([expected]),
        _ForbiddenSemanticRouter(),
    )

    assert model.decide(_state_after_tool("Prolonge-la"), []) == expected
