from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from agentpms.agent import (
    AgentOrchestrator,
    DecisionModelUnavailableError,
    QueuedDecisionModel,
)
from agentpms.agent.state import AgentState
from agentpms.contracts import (
    AgentDecision,
    AgentRunStatus,
    AgentWorkingMemory,
    DecisionKind,
    RiskLevel,
    ToolResult,
    ToolStatus,
)
from agentpms.observability import InMemoryTraceSink
from agentpms.safety import (
    ConfirmationManager,
    DecisionGroundingPolicy,
    PermissionPolicy,
)
from agentpms.tools import BaseTool, ToolContext, ToolRegistry
from agentpms.tools.pms_tools import (
    AddExtraTool,
    CheckAvailabilityTool,
    CreateReservationTool,
    SearchReservationsTool,
)


def _orchestrator(pms, decisions, *, allowed=None, max_steps=6):
    trace = InMemoryTraceSink()
    registry = ToolRegistry(
        [SearchReservationsTool(pms), AddExtraTool(pms)]
    )
    orchestrator = AgentOrchestrator(
        decision_model=QueuedDecisionModel(decisions),
        registry=registry,
        permissions=PermissionPolicy(
            {"reception": set(allowed or {"search_reservations", "add_extra"})}
        ),
        confirmations=ConfirmationManager(ttl_seconds=300),
        trace_sink=trace,
        max_steps=max_steps,
    )
    return orchestrator, trace


def _add_extra_decision():
    return AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="add_extra",
        arguments={
            "reservation_id": "R-4587",
            "extra_code": "BREAKFAST",
            "label": "Petit-dejeuner",
            "quantity": 1,
            "unit_price": "15.00",
        },
    )


class _DirectInput(BaseModel):
    pass


class _DirectReadTool(BaseTool):
    name = "direct_read"
    description = "Retourne un resultat verifie sans reformulation."
    risk = RiskLevel.READ
    return_direct = True
    input_model = _DirectInput

    def execute(self, arguments: _DirectInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message="Resultat exact du devis.",
            verified=True,
        )


class _ModelDirectedReadTool(_DirectReadTool):
    name = "model_directed_read"
    return_direct = False


class _FailingReadTool(_DirectReadTool):
    name = "failing_read"

    def execute(self, arguments: _DirectInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.ERROR,
            message="Le PMS est momentanement indisponible.",
            error_code="pms_unavailable",
            retryable=True,
            verified=False,
        )


class _FailingWriteTool(BaseTool):
    name = "failing_write"
    description = "Ecriture de test qui echoue."
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = _DirectInput

    def execute(self, arguments: _DirectInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.ERROR,
            message="Le PMS a refuse la modification.",
            error_code="pms_write_failed",
            verified=False,
        )


class _RecoveringWriteTool(_FailingWriteTool):
    name = "recovering_write"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, arguments: _DirectInput, context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return super().execute(arguments, context)
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message="Modification corrigee et verifiee.",
            verified=True,
        )


class _UnavailableDecisionModel:
    def decide(self, state, tools):
        raise DecisionModelUnavailableError("GPU indisponible")


def test_provider_failure_is_reported_without_claiming_a_bad_decision(pms):
    orchestrator, trace = _orchestrator(pms, [])
    orchestrator.decision_model = _UnavailableDecisionModel()

    response = orchestrator.start(
        "Prends la chambre la plus chere.",
        actor_id="reception",
    )

    assert response.status == AgentRunStatus.ERROR
    assert "service de decision IA est momentanement indisponible" in response.message
    assert "Aucune action n'a ete executee" in response.message
    assert trace.events[-1].event_type == "decision_provider_error"


class _AvailabilityThenInvalidDecision:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, state, tools):
        self.calls += 1
        if self.calls == 1:
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="check_availability",
                arguments={
                    "arrival_date": "2026-09-18",
                    "departure_date": "2026-09-20",
                    "adults": 2,
                    "children": 0,
                },
            )
        raise ValueError("invalid model output")


def test_model_failure_after_a_read_is_not_replaced_by_semantic_rules(pms):
    registry = ToolRegistry(
        [CheckAvailabilityTool(pms), CreateReservationTool(pms)]
    )
    orchestrator = AgentOrchestrator(
        decision_model=_AvailabilityThenInvalidDecision(),
        registry=registry,
        permissions=PermissionPolicy(
            {"reception": {"check_availability", "create_reservation"}}
        ),
        confirmations=ConfirmationManager(ttl_seconds=300),
        decision_grounding=DecisionGroundingPolicy(),
    )

    response = orchestrator.start(
        (
            "Fais une reservation pour Jean Pierrlafittre du 18/09 au 20/09 "
            "pour deux personnes. Prends le moins cher, sans email ni telephone."
        ),
        actor_id="u1",
    )

    assert response.status == AgentRunStatus.ERROR
    assert response.confirmation is None
    assert response.tool_results[-1].tool_name == "check_availability"
    assert "modele" in response.message


def test_sensitive_write_waits_for_confirmation_then_executes(pms):
    orchestrator, trace = _orchestrator(
        pms,
        [
            _add_extra_decision(),
            AgentDecision(
                kind=DecisionKind.FINAL,
                response="Le petit-dejeuner a ete ajoute et verifie.",
            ),
        ],
    )

    pending = orchestrator.start(
        "Ajoute un petit-dejeuner a la reservation R-4587.",
        actor_id="reception-1",
    )

    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []

    completed = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.tool_results[-1].status == ToolStatus.SUCCESS
    assert completed.tool_results[-1].verified is True
    assert len(pms.get_reservation("R-4587").data["reservation"]["extras"]) == 1
    assert {event.event_type for event in trace.events} >= {
        "confirmation_requested",
        "plan_created",
        "tool_finished",
    }


def test_failed_confirmed_write_stops_without_requesting_confirmation_again():
    orchestrator = AgentOrchestrator(
        decision_model=QueuedDecisionModel(
            [
                AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="failing_write",
                    arguments={},
                ),
                AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="failing_write",
                    arguments={},
                ),
            ]
        ),
        registry=ToolRegistry([_FailingWriteTool()]),
        permissions=PermissionPolicy({"reception": {"failing_write"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )
    pending = orchestrator.start("Modifie la donnee.", actor_id="u1")

    response = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="u1",
    )

    assert response.status == AgentRunStatus.ERROR
    assert response.confirmation is None
    assert response.message == "Le PMS a refuse la modification."
    assert len(response.tool_results) == 1


def test_a_corrected_verified_write_supersedes_an_older_failed_attempt():
    tool = _RecoveringWriteTool()
    decision = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name=tool.name,
        arguments={},
    )
    orchestrator = AgentOrchestrator(
        decision_model=QueuedDecisionModel(
            [
                decision,
                decision,
                AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="La correction est terminee.",
                ),
            ]
        ),
        registry=ToolRegistry([tool]),
        permissions=PermissionPolicy({"reception": {tool.name}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )
    first = orchestrator.start("Modifie la donnee.", actor_id="u1")
    failed = orchestrator.confirm(
        request_id=first.request_id,
        confirmation_id=first.confirmation.confirmation_id,
        approved=True,
        actor_id="u1",
    )
    assert failed.status == AgentRunStatus.ERROR

    second = orchestrator.continue_request(
        request_id=first.request_id,
        user_message="Corrige puis reessaie.",
        actor_id="u1",
    )
    completed = orchestrator.confirm(
        request_id=second.request_id,
        confirmation_id=second.confirmation.confirmation_id,
        approved=True,
        actor_id="u1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.message == "La correction est terminee."
    assert tool.calls == 2


def test_orchestrator_tracks_and_adapts_a_public_tool_plan(pms):
    first = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="search_reservations",
        arguments={"guest_name": "Jean Dupont"},
        plan=["search_reservations"],
    )
    second = _add_extra_decision().model_copy(
        update={"plan": ["add_extra"]}
    )
    orchestrator, trace = _orchestrator(pms, [first, second])

    pending = orchestrator.start(
        "Trouve Jean Dupont puis ajoute un petit-dejeuner.",
        actor_id="reception-1",
    )
    state = orchestrator.state_store.get(pending.request_id)

    assert pending.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert state.workflow_plan == ["search_reservations", "add_extra"]
    assert state.completed_plan_steps == ["search_reservations"]
    assert [
        event.event_type
        for event in trace.events
        if event.event_type in {"plan_created", "plan_adapted"}
    ] == ["plan_created", "plan_adapted"]


def test_repeated_write_decision_is_blocked_after_one_verified_execution(pms):
    orchestrator, trace = _orchestrator(
        pms,
        [_add_extra_decision(), _add_extra_decision()],
    )
    pending = orchestrator.start(
        "Ajoute un petit-dejeuner a la reservation R-4587.",
        actor_id="reception-1",
    )

    completed = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.confirmation is None
    assert "seconde fois" in completed.message
    assert len(pms.get_reservation("R-4587").data["reservation"]["extras"]) == 1
    assert "duplicate_write_blocked" in {
        event.event_type for event in trace.events
    }


def test_distinct_writes_using_the_same_tool_receive_distinct_confirmations(pms):
    first = _add_extra_decision()
    second = _add_extra_decision().model_copy(
        update={
            "arguments": {
                **_add_extra_decision().arguments,
                "quantity": 2,
            }
        }
    )
    orchestrator, _ = _orchestrator(
        pms,
        [
            first,
            second,
            AgentDecision(kind=DecisionKind.FINAL, response="Deux ajouts verifies."),
        ],
    )

    pending_first = orchestrator.start(
        "Ajoute deux prestations distinctes.",
        actor_id="reception-1",
    )
    pending_second = orchestrator.confirm(
        request_id=pending_first.request_id,
        confirmation_id=pending_first.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    completed = orchestrator.confirm(
        request_id=pending_second.request_id,
        confirmation_id=pending_second.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )

    assert pending_second.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert completed.status == AgentRunStatus.COMPLETED
    extras = pms.get_reservation("R-4587").data["reservation"]["extras"]
    assert [item["quantity"] for item in extras] == [1, 2]


def test_plan_tracks_repeated_occurrences_of_the_same_tool():
    decisions = [
        AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="model_directed_read",
            arguments={},
            plan=["model_directed_read", "model_directed_read"],
            required_tools=["model_directed_read", "model_directed_read"],
        ),
        AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="model_directed_read",
            arguments={},
            plan=["model_directed_read"],
            required_tools=["model_directed_read"],
        ),
        AgentDecision(kind=DecisionKind.FINAL, response="Deux lectures terminees."),
    ]
    orchestrator = AgentOrchestrator(
        decision_model=QueuedDecisionModel(decisions),
        registry=ToolRegistry([_ModelDirectedReadTool()]),
        permissions=PermissionPolicy({"reception": {"model_directed_read"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )

    response = orchestrator.start("Execute deux lectures.", actor_id="u1")
    state = orchestrator.state_store.get(response.request_id)

    assert response.status == AgentRunStatus.COMPLETED
    assert state.workflow_plan == ["model_directed_read", "model_directed_read"]
    assert state.completed_plan_steps == [
        "model_directed_read",
        "model_directed_read",
    ]


def test_expired_confirmation_releases_the_conversation(pms):
    orchestrator, _ = _orchestrator(
        pms,
        [
            _add_extra_decision(),
            AgentDecision(kind=DecisionKind.FINAL, response="Nouvelle demande recue."),
        ],
    )
    pending = orchestrator.start(
        "Ajoute un petit-dejeuner a la reservation R-4587.",
        actor_id="reception-1",
    )
    record = orchestrator.confirmations._records[
        pending.confirmation.confirmation_id
    ]
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    expired = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="reception-1",
    )
    resumed = orchestrator.continue_request(
        request_id=pending.request_id,
        user_message="Passons a une nouvelle demande.",
        actor_id="reception-1",
    )

    assert expired.status == AgentRunStatus.ERROR
    assert expired.message == "Confirmation expiree."
    assert resumed.status == AgentRunStatus.COMPLETED
    assert resumed.message == "Nouvelle demande recue."


def test_denied_confirmation_performs_no_write(pms):
    orchestrator, _ = _orchestrator(pms, [_add_extra_decision()])
    pending = orchestrator.start("Ajoute le petit-dejeuner.", actor_id="u1")

    denied = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=False,
        actor_id="u1",
    )

    assert denied.status == AgentRunStatus.DENIED
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []


def test_other_actor_cannot_reuse_confirmation(pms):
    orchestrator, _ = _orchestrator(pms, [_add_extra_decision()])
    pending = orchestrator.start("Ajoute le petit-dejeuner.", actor_id="u1")

    rejected = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=True,
        actor_id="u2",
    )

    assert rejected.status == AgentRunStatus.ERROR
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []


def test_other_actor_cannot_cancel_confirmation(pms):
    orchestrator, _ = _orchestrator(pms, [_add_extra_decision()])
    pending = orchestrator.start("Ajoute le petit-dejeuner.", actor_id="u1")

    rejected = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=False,
        actor_id="u2",
    )

    assert rejected.status == AgentRunStatus.ERROR

    accepted = orchestrator.confirm(
        request_id=pending.request_id,
        confirmation_id=pending.confirmation.confirmation_id,
        approved=False,
        actor_id="u1",
    )
    assert accepted.status == AgentRunStatus.DENIED


def test_forbidden_tool_is_not_executed(pms):
    orchestrator, _ = _orchestrator(
        pms, [_add_extra_decision()], allowed={"search_reservations"}
    )

    response = orchestrator.start("Ajoute le petit-dejeuner.", actor_id="u1")

    assert response.status == AgentRunStatus.FORBIDDEN
    assert pms.get_reservation("R-4587").data["reservation"]["extras"] == []


def test_ambiguous_search_can_trigger_clarification(pms):
    orchestrator, _ = _orchestrator(
        pms,
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="search_reservations",
                arguments={"guest_name": "Dupont"},
            ),
            AgentDecision(
                kind=DecisionKind.CLARIFY,
                response="Deux reservations correspondent. Laquelle faut-il utiliser ?",
            ),
        ],
    )

    response = orchestrator.start("Trouve la reservation Dupont.", actor_id="u1")

    assert response.status == AgentRunStatus.NEEDS_CLARIFICATION
    assert response.tool_results[0].status == ToolStatus.AMBIGUOUS


def test_agent_stops_after_configured_number_of_steps(pms):
    decisions = [
        AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="unknown_tool",
            arguments={},
        )
        for _ in range(2)
    ]
    orchestrator, _ = _orchestrator(pms, decisions, max_steps=2)

    response = orchestrator.start("Boucle.", actor_id="u1")

    assert response.status == AgentRunStatus.STOPPED
    assert response.steps == 2


def test_verified_return_direct_tool_does_not_ask_the_llm_to_rephrase():
    decision_model = QueuedDecisionModel(
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="direct_read",
                arguments={},
            )
        ]
    )
    orchestrator = AgentOrchestrator(
        decision_model=decision_model,
        registry=ToolRegistry([_DirectReadTool()]),
        permissions=PermissionPolicy({"reception": {"direct_read"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )

    response = orchestrator.start("Donne le devis.", actor_id="u1")

    assert response.status == AgentRunStatus.COMPLETED
    assert response.message == "Resultat exact du devis."
    assert response.steps == 1


def test_model_can_return_a_verified_read_result_without_a_second_llm_call():
    decision_model = QueuedDecisionModel(
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="model_directed_read",
                arguments={},
                respond_with_tool_result=True,
            )
        ]
    )
    orchestrator = AgentOrchestrator(
        decision_model=decision_model,
        registry=ToolRegistry([_ModelDirectedReadTool()]),
        permissions=PermissionPolicy({"reception": {"model_directed_read"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )

    response = orchestrator.start("Donne le resultat PMS exact.", actor_id="u1")

    assert response.status == AgentRunStatus.COMPLETED
    assert response.message == "Resultat exact du devis."
    assert response.steps == 1


def test_premature_final_cannot_stop_a_model_planned_multi_tool_workflow():
    trace = InMemoryTraceSink()
    decision_model = QueuedDecisionModel(
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="model_directed_read",
                arguments={},
                plan=["model_directed_read", "direct_read"],
                required_tools=["direct_read"],
                respond_with_tool_result=True,
            ),
            AgentDecision(
                kind=DecisionKind.FINAL,
                response="Conclusion prematuree.",
            ),
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="direct_read",
                arguments={},
                plan=["direct_read"],
            ),
        ]
    )
    orchestrator = AgentOrchestrator(
        decision_model=decision_model,
        registry=ToolRegistry([_ModelDirectedReadTool(), _DirectReadTool()]),
        permissions=PermissionPolicy(
            {"reception": {"model_directed_read", "direct_read"}}
        ),
        confirmations=ConfirmationManager(ttl_seconds=300),
        trace_sink=trace,
    )

    response = orchestrator.start("Execute plusieurs etapes.", actor_id="u1")

    assert response.status == AgentRunStatus.COMPLETED
    assert response.message == "Resultat exact du devis."
    assert response.steps == 3
    assert [result.tool_name for result in response.tool_results] == [
        "model_directed_read",
        "direct_read",
    ]
    assert any(event.event_type == "premature_final_blocked" for event in trace.events)


def test_final_after_required_tool_failure_stops_without_retry_loop():
    trace = InMemoryTraceSink()
    decision_model = QueuedDecisionModel(
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="failing_read",
                arguments={},
                plan=["failing_read"],
                required_tools=["failing_read"],
            ),
            AgentDecision(
                kind=DecisionKind.FINAL,
                response="Je vais ignorer la panne.",
            ),
        ]
    )
    orchestrator = AgentOrchestrator(
        decision_model=decision_model,
        registry=ToolRegistry([_FailingReadTool()]),
        permissions=PermissionPolicy({"reception": {"failing_read"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
        trace_sink=trace,
    )

    response = orchestrator.start("Consulte le PMS.", actor_id="u1")

    assert response.status == AgentRunStatus.ERROR
    assert response.message == "Le PMS est momentanement indisponible."
    assert response.steps == 2
    assert len(response.tool_results) == 1
    assert trace.events[-1].event_type == "workflow_failed"


def test_model_cannot_use_direct_result_flag_to_bypass_write_confirmation():
    orchestrator = AgentOrchestrator(
        decision_model=QueuedDecisionModel(
            [
                AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="failing_write",
                    arguments={},
                    respond_with_tool_result=True,
                )
            ]
        ),
        registry=ToolRegistry([_FailingWriteTool()]),
        permissions=PermissionPolicy({"reception": {"failing_write"}}),
        confirmations=ConfirmationManager(ttl_seconds=300),
    )

    response = orchestrator.start("Modifie la donnee.", actor_id="u1")

    assert response.status == AgentRunStatus.NEEDS_CONFIRMATION
    assert response.confirmation is not None
    assert response.tool_results == []


def test_short_followup_preserves_the_same_goal_and_working_memory(pms):
    memory = AgentWorkingMemory(
        objective="Creer une reservation",
        known_facts={"guest_name": "Maria Delafuente", "adults": 1},
        constraints=["choisir l'offre la plus chere"],
    )
    orchestrator, _ = _orchestrator(
        pms,
        [
            AgentDecision(
                kind=DecisionKind.CLARIFY,
                response="Quelle categorie choisissez-vous ?",
                memory=memory,
                new_goal=True,
            ),
            AgentDecision(
                kind=DecisionKind.FINAL,
                response="Le choix le plus cher est conserve.",
                memory=memory,
            ),
        ],
    )

    first = orchestrator.start("Prepare la reservation.", actor_id="u1")
    continued = orchestrator.continue_request(
        request_id=first.request_id,
        user_message="Prends la plus chere.",
        actor_id="u1",
    )
    state = orchestrator.state_store.get(first.request_id)

    assert continued.status == AgentRunStatus.COMPLETED
    assert state.intent_revision == 1
    assert state.turn_index == 1
    assert state.working_memory == memory


def test_new_goal_is_registered_only_once_during_the_same_turn(pms):
    orchestrator, trace = _orchestrator(
        pms,
        [
            AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="search_reservations",
                arguments={"guest_name": "Jean Dupont"},
                new_goal=True,
            ),
            AgentDecision(
                kind=DecisionKind.FINAL,
                response="Reservation trouvee.",
                new_goal=True,
            ),
        ],
    )

    response = orchestrator.start("Trouve Jean Dupont.", actor_id="u1")
    state = orchestrator.state_store.get(response.request_id)

    assert response.status == AgentRunStatus.COMPLETED
    assert state.intent_revision == 1
    assert sum(event.event_type == "goal_changed" for event in trace.events) == 1


def test_room_preference_requires_verbatim_evidence_from_the_current_user_turn(pms):
    orchestrator, _ = _orchestrator(pms, [])
    state = AgentState(
        actor_id="u1",
        role="reception",
        user_message="Prends la plus chère.",
        messages=[{"role": "user", "content": "Prends la plus chère."}],
    )
    grounded = AgentDecision(
        kind=DecisionKind.CLARIFY,
        response="Je traite votre choix.",
        room_selection_strategy="most_expensive",
        room_selection_evidence="la plus chère",
    )

    orchestrator._apply_model_context(state, grounded)

    assert state.room_selection_strategy == "most_expensive"
    assert state.room_selection_evidence == "la plus chère"
    assert state.room_selection_turn == 0


def test_room_preference_cannot_appear_after_a_tool_observation(pms):
    orchestrator, _ = _orchestrator(pms, [])
    state = AgentState(
        actor_id="u1",
        role="reception",
        user_message="Crée une réservation.",
        messages=[{"role": "user", "content": "Crée une réservation."}],
        step_count=1,
    )
    fabricated = AgentDecision(
        kind=DecisionKind.TOOL,
        tool_name="create_reservation",
        arguments={},
        room_selection_strategy="cheapest",
        room_selection_evidence="une réservation",
    )

    orchestrator._apply_model_context(state, fabricated)

    assert state.room_selection_strategy is None
    assert state.room_selection_evidence is None
