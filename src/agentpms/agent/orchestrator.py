"""Boucle agentique bornee, controlee et observable."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from time import perf_counter

from agentpms.contracts import (
    AgentDecision,
    AgentResponse,
    AgentRunStatus,
    DecisionKind,
    RiskLevel,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from agentpms.observability.tracing import NullTraceSink, TraceEvent, TraceSink
from agentpms.safety import ConfirmationManager, DecisionGroundingPolicy, PermissionPolicy
from agentpms.tools import ToolContext, ToolRegistry

from .decision import (
    DecisionModel,
    DecisionModelUnavailableError,
    InvalidDecisionError,
)
from .state import AgentMessage, AgentState, InMemoryStateStore, StateStore


class AgentOrchestrator:
    def __init__(
        self,
        *,
        decision_model: DecisionModel,
        registry: ToolRegistry,
        permissions: PermissionPolicy,
        confirmations: ConfirmationManager,
        decision_grounding: DecisionGroundingPolicy | None = None,
        state_store: StateStore | None = None,
        trace_sink: TraceSink | None = None,
        max_steps: int = 6,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps doit etre positif")
        self.decision_model = decision_model
        self.registry = registry
        self.permissions = permissions
        self.confirmations = confirmations
        self.decision_grounding = decision_grounding
        self.state_store = state_store or InMemoryStateStore()
        self.trace_sink = trace_sink or NullTraceSink()
        self.max_steps = max_steps

    def start(
        self,
        user_message: str,
        *,
        actor_id: str,
        role: str = "reception",
    ) -> AgentResponse:
        if not user_message.strip():
            raise ValueError("La demande utilisateur ne peut pas etre vide")
        state = AgentState(
            actor_id=actor_id,
            role=role,
            user_message=user_message.strip(),
            messages=[AgentMessage(role="user", content=user_message.strip())],
            max_steps=self.max_steps,
        )
        self.state_store.save(state)
        self.trace_sink.record(
            TraceEvent(event_type="request_started", request_id=state.request_id)
        )
        return self._advance(state)

    def confirm(
        self,
        *,
        request_id: str,
        confirmation_id: str,
        approved: bool,
        actor_id: str,
    ) -> AgentResponse:
        state = self.state_store.get(request_id)
        if state is None:
            return AgentResponse(
                request_id=request_id,
                status=AgentRunStatus.ERROR,
                message="Execution agentique introuvable.",
            )
        if state.pending_call is None or state.pending_confirmation_id is None:
            return self._response(
                state, AgentRunStatus.ERROR, "Aucune action n'attend de confirmation."
            )
        if confirmation_id != state.pending_confirmation_id:
            return self._response(
                state, AgentRunStatus.ERROR, "Identifiant de confirmation incorrect."
            )
        call = state.pending_call
        if not approved:
            valid, message = self.confirmations.consume(
                confirmation_id,
                request_id=state.request_id,
                actor_id=actor_id,
                call=call,
            )
            if not valid:
                self._clear_expired_confirmation(state, message)
                return self._response(state, AgentRunStatus.ERROR, message)
            state.pending_call = None
            state.pending_confirmation_id = None
            state.completed = True
            denial_message = "Action annulee : aucune modification n'a ete effectuee."
            state.messages.append(
                AgentMessage(
                    role="assistant",
                    content=denial_message,
                    turn_index=state.turn_index,
                    goal_revision=state.intent_revision,
                )
            )
            self.state_store.save(state)
            self.trace_sink.record(
                TraceEvent(
                    event_type="confirmation_denied",
                    request_id=state.request_id,
                )
            )
            return self._response(
                state,
                AgentRunStatus.DENIED,
                denial_message,
            )

        valid, message = self.confirmations.consume(
            confirmation_id,
            request_id=state.request_id,
            actor_id=actor_id,
            call=call,
        )
        if not valid:
            self._clear_expired_confirmation(state, message)
            return self._response(state, AgentRunStatus.ERROR, message)

        prepared = self.registry.validate(call)
        if isinstance(prepared, ToolResult):
            self._append_result(state, prepared)
            return self._response(state, AgentRunStatus.ERROR, prepared.message)
        tool, _ = prepared
        if not self.permissions.is_allowed(state.role, tool):
            return self._response(
                state, AgentRunStatus.FORBIDDEN, "Action interdite pour ce role."
            )

        state.pending_call = None
        state.pending_confirmation_id = None
        result = self._execute(state, call, tool.risk)
        self._append_result(state, result)
        self.state_store.save(state)
        if result.status != ToolStatus.SUCCESS:
            state.completed = True
            message = result.message
            state.messages.append(AgentMessage(role="assistant", content=message))
            self.state_store.save(state)
            return self._response(state, AgentRunStatus.ERROR, message)
        return self._advance(state)

    def continue_request(
        self,
        *,
        request_id: str,
        user_message: str,
        actor_id: str,
    ) -> AgentResponse:
        """Poursuit une conversation existante, terminee ou a preciser."""
        state = self.state_store.get(request_id)
        if state is None:
            return AgentResponse(
                request_id=request_id,
                status=AgentRunStatus.ERROR,
                message="Execution agentique introuvable.",
            )
        if state.actor_id != actor_id:
            return self._response(
                state,
                AgentRunStatus.FORBIDDEN,
                "Cette conversation appartient a un autre utilisateur.",
            )
        if state.pending_call is not None:
            return self._response(
                state,
                AgentRunStatus.NEEDS_CONFIRMATION,
                "Une action attend deja une confirmation.",
            )
        message = user_message.strip()
        if not message:
            return self._response(
                state,
                AgentRunStatus.ERROR,
                "La precision utilisateur ne peut pas etre vide.",
            )
        state.user_message = message
        state.turn_index += 1
        state.messages.append(
            AgentMessage(
                role="user",
                content=message,
                turn_index=state.turn_index,
                goal_revision=state.intent_revision,
            )
        )
        state.completed = False
        state.step_count = 0
        self.state_store.save(state)
        self.trace_sink.record(
            TraceEvent(event_type="request_continued", request_id=state.request_id)
        )
        return self._advance(state)

    def _advance(self, state: AgentState) -> AgentResponse:
        while state.step_count < self.max_steps:
            try:
                decision = self.decision_model.decide(state, self.registry.specs())
                self._apply_model_context(state, decision)
                if self.decision_grounding is not None:
                    proposed_decision = decision
                    decision = self.decision_grounding.review(state, decision)
                    latest_result = (
                        state.tool_results[-1] if state.tool_results else None
                    )
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="grounding_review",
                            request_id=state.request_id,
                            tool_name=proposed_decision.tool_name,
                            status="changed" if decision != proposed_decision else "accepted",
                            fields={
                                "proposed_kind": proposed_decision.kind.value,
                                "reviewed_kind": decision.kind.value,
                                "reviewed_tool": decision.tool_name,
                                "effective_room_selection_strategy": (
                                    state.room_selection_strategy
                                ),
                                "latest_result_tool": (
                                    latest_result.tool_name
                                    if latest_result is not None
                                    else None
                                ),
                                "latest_room_type_count": (
                                    len(latest_result.data.get("room_types", []))
                                    if latest_result is not None
                                    else 0
                                ),
                            },
                        )
                    )
            except Exception as error:
                if isinstance(error, DecisionModelUnavailableError):
                    state.completed = True
                    self.state_store.save(state)
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="decision_provider_error",
                            request_id=state.request_id,
                            status="unavailable",
                            fields={"error_type": type(error).__name__},
                        )
                    )
                    return self._response(
                        state,
                        AgentRunStatus.ERROR,
                        (
                            "Le service de decision IA est momentanement "
                            "indisponible. Aucune action n'a ete executee. Vous pouvez "
                            "reprendre cette conversation sans ressaisir les "
                            "informations deja fournies."
                        ),
                    )
                if isinstance(error, InvalidDecisionError):
                    state.completed = True
                    self.state_store.save(state)
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="decision_validation_error",
                            request_id=state.request_id,
                            status="invalid",
                            fields={"error_type": type(error).__name__},
                        )
                    )
                    return self._response(
                        state,
                        AgentRunStatus.ERROR,
                        (
                            "Le modele a repondu dans un format invalide meme "
                            "apres correction. Aucune action n'a ete executee; "
                            "la conversation peut etre reprise."
                        ),
                    )
                recovered = (
                    self.decision_grounding.recover(state)
                    if self.decision_grounding is not None
                    else None
                )
                if recovered is not None:
                    decision = recovered
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="decision_recovered",
                            request_id=state.request_id,
                            tool_name=decision.tool_name,
                            status=decision.kind.value,
                            fields={
                                "step": state.step_count + 1,
                                "error_type": type(error).__name__,
                            },
                        )
                    )
                else:
                    state.completed = True
                    self.state_store.save(state)
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="decision_error",
                            request_id=state.request_id,
                            status="error",
                            fields={"error_type": type(error).__name__},
                        )
                    )
                    return self._response(
                        state,
                        AgentRunStatus.ERROR,
                        "Le modele n'a pas produit une decision exploitable.",
                    )

            state.step_count += 1
            if (
                decision.kind == DecisionKind.TOOL
                or decision.plan
                or decision.required_tools
            ):
                self._register_plan(state, decision)
            self.trace_sink.record(
                TraceEvent(
                    event_type="decision",
                    request_id=state.request_id,
                    tool_name=decision.tool_name,
                    status=decision.kind.value,
                    fields={
                        "step": state.step_count,
                        "plan": decision.plan,
                        "room_selection_strategy": (
                            decision.room_selection_strategy
                        ),
                        "room_selection_evidence": (
                            decision.room_selection_evidence
                        ),
                        "effective_room_selection_strategy": (
                            state.room_selection_strategy
                        ),
                        "effective_room_selection_turn": (
                            state.room_selection_turn
                        ),
                    },
                )
            )

            if decision.kind == DecisionKind.FINAL:
                remaining_tools = self._remaining_required_steps(state)
                terminal_tool_error = (
                    state.tool_results[-1]
                    if state.tool_results
                    and state.tool_results[-1].status != ToolStatus.SUCCESS
                    and state.tool_results[-1].tool_name in remaining_tools
                    else None
                )
                if remaining_tools and terminal_tool_error is None:
                    guard_result = ToolResult(
                        call_id=f"workflow-guard-{state.step_count}",
                        tool_name="workflow_guard",
                        status=ToolStatus.ERROR,
                        message=(
                            "Le workflow annonce n'est pas termine. Choisissez "
                            "le prochain tool au lieu de conclure."
                        ),
                        data={"remaining_tools": remaining_tools},
                        error_code="workflow_incomplete",
                        verified=True,
                    )
                    state.messages.append(
                        AgentMessage(
                            role="tool",
                            content=guard_result.model_dump_json(),
                            turn_index=state.turn_index,
                            goal_revision=state.intent_revision,
                        )
                    )
                    self.state_store.save(state)
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="premature_final_blocked",
                            request_id=state.request_id,
                            status="blocked",
                            fields={"remaining_tools": remaining_tools},
                        )
                    )
                    continue
                if terminal_tool_error is not None:
                    state.completed = True
                    state.messages.append(
                        AgentMessage(
                            role="assistant",
                            content=terminal_tool_error.message,
                            turn_index=state.turn_index,
                            goal_revision=state.intent_revision,
                        )
                    )
                    self.state_store.save(state)
                    self.trace_sink.record(
                        TraceEvent(
                            event_type="workflow_failed",
                            request_id=state.request_id,
                            tool_name=terminal_tool_error.tool_name,
                            status="error",
                            fields={
                                "error_code": terminal_tool_error.error_code,
                                "remaining_tools": remaining_tools,
                            },
                        )
                    )
                    return self._response(
                        state,
                        AgentRunStatus.ERROR,
                        terminal_tool_error.message,
                    )
                unsafe = self._unverified_write(state)
                if unsafe is not None:
                    state.completed = True
                    self.state_store.save(state)
                    return self._response(
                        state,
                        AgentRunStatus.ERROR,
                        "Je ne peux pas confirmer l'action : le PMS ne l'a pas verifiee.",
                    )
                state.completed = True
                state.messages.append(
                    AgentMessage(
                        role="assistant",
                        content=decision.response or "",
                        turn_index=state.turn_index,
                        goal_revision=state.intent_revision,
                    )
                )
                self.state_store.save(state)
                return self._response(
                    state, AgentRunStatus.COMPLETED, decision.response or ""
                )

            if decision.kind == DecisionKind.CLARIFY:
                state.completed = True
                clarification = decision.response or "Une precision est necessaire."
                state.messages.append(
                    AgentMessage(
                        role="assistant",
                        content=clarification,
                        turn_index=state.turn_index,
                        goal_revision=state.intent_revision,
                    )
                )
                self.state_store.save(state)
                return self._response(
                    state,
                    AgentRunStatus.NEEDS_CLARIFICATION,
                    clarification,
                )

            response = self._handle_tool_decision(state, decision)
            if response is not None:
                return response

        state.completed = True
        self.state_store.save(state)
        self.trace_sink.record(
            TraceEvent(
                event_type="step_limit_reached",
                request_id=state.request_id,
                status="stopped",
            )
        )
        return self._response(
            state,
            AgentRunStatus.STOPPED,
            "Execution arretee : nombre maximal d'etapes atteint.",
        )

    def _handle_tool_decision(
        self, state: AgentState, decision: AgentDecision
    ) -> AgentResponse | None:
        call = ToolCall(name=decision.tool_name or "", arguments=decision.arguments)
        state.messages.append(
            AgentMessage(
                role="assistant",
                content=(
                    AgentDecision(
                        kind=DecisionKind.TOOL,
                        tool_name=call.name,
                        arguments=call.arguments,
                        plan=decision.plan,
                        required_tools=decision.required_tools,
                        respond_with_tool_result=decision.respond_with_tool_result,
                        room_selection_strategy=decision.room_selection_strategy,
                        room_selection_evidence=decision.room_selection_evidence,
                    ).model_dump_json(exclude_none=True)
                ),
                turn_index=state.turn_index,
                goal_revision=state.intent_revision,
            )
        )
        prepared = self.registry.validate(call)
        if isinstance(prepared, ToolResult):
            self._append_result(state, prepared)
            self.state_store.save(state)
            return None
        tool, arguments = prepared
        call = call.model_copy(
            update={
                "arguments": arguments.model_dump(
                    mode="json",
                    exclude_none=True,
                )
            }
        )
        if not self.permissions.is_allowed(state.role, tool):
            result = ToolResult(
                call_id=call.call_id,
                tool_name=tool.name,
                status=ToolStatus.FORBIDDEN,
                message="Tool interdit pour ce role.",
                error_code="permission_denied",
            )
            self._append_result(state, result)
            state.completed = True
            self.state_store.save(state)
            return self._response(
                state, AgentRunStatus.FORBIDDEN, "Action interdite pour ce role."
            )

        action_key = self._write_action_key(
            state,
            tool.name,
            call.arguments,
        )
        if (
            tool.risk != RiskLevel.READ
            and action_key in state.completed_write_actions
        ):
            message = (
                "Cette action exacte a deja ete executee et verifiee. "
                "Elle ne sera pas executee une seconde fois."
            )
            state.completed = True
            state.messages.append(
                AgentMessage(
                    role="assistant",
                    content=message,
                    turn_index=state.turn_index,
                    goal_revision=state.intent_revision,
                )
            )
            self.state_store.save(state)
            self.trace_sink.record(
                TraceEvent(
                    event_type="duplicate_write_blocked",
                    request_id=state.request_id,
                    tool_name=tool.name,
                    status="blocked",
                    fields={"intent_revision": state.intent_revision},
                )
            )
            return self._response(state, AgentRunStatus.COMPLETED, message)

        if self.permissions.requires_confirmation(tool):
            prompt = self.confirmations.issue(
                request_id=state.request_id,
                actor_id=state.actor_id,
                call=call,
                summary=tool.confirmation_summary(arguments),
            )
            state.pending_call = call
            state.pending_confirmation_id = prompt.confirmation_id
            self.state_store.save(state)
            self.trace_sink.record(
                TraceEvent(
                    event_type="confirmation_requested",
                    request_id=state.request_id,
                    tool_name=tool.name,
                    status="pending",
                    fields={"argument_names": sorted(call.arguments)},
                )
            )
            return AgentResponse(
                request_id=state.request_id,
                status=AgentRunStatus.NEEDS_CONFIRMATION,
                message=prompt.summary,
                steps=state.step_count,
                tool_results=state.tool_results,
                confirmation=prompt,
            )

        result = self._execute(state, call, tool.risk)
        self._append_result(state, result)
        self.state_store.save(state)
        if (
            (tool.return_direct or decision.respond_with_tool_result)
            and tool.risk == RiskLevel.READ
            and result.status == ToolStatus.SUCCESS
            and result.verified
            and not self._plan_has_remaining_steps(state)
        ):
            state.completed = True
            state.messages.append(
                AgentMessage(
                    role="assistant",
                    content=result.message,
                    turn_index=state.turn_index,
                    goal_revision=state.intent_revision,
                )
            )
            self.state_store.save(state)
            self.trace_sink.record(
                TraceEvent(
                    event_type="verified_tool_result_returned",
                    request_id=state.request_id,
                    tool_name=tool.name,
                    status="completed",
                )
            )
            return self._response(
                state,
                AgentRunStatus.COMPLETED,
                result.message,
            )
        return None

    @staticmethod
    def _plan_has_remaining_steps(state: AgentState) -> bool:
        return bool(AgentOrchestrator._remaining_plan_steps(state))

    @staticmethod
    def _remaining_plan_steps(state: AgentState) -> list[str]:
        return AgentOrchestrator._subtract_completed_occurrences(
            state.workflow_plan,
            state.completed_plan_steps,
        )

    @staticmethod
    def _remaining_required_steps(state: AgentState) -> list[str]:
        return AgentOrchestrator._subtract_completed_occurrences(
            state.required_plan_steps,
            state.completed_plan_steps,
        )

    @staticmethod
    def _subtract_completed_occurrences(
        planned: list[str],
        completed: list[str],
    ) -> list[str]:
        """Soustrait chaque execution une seule fois, sans perdre les doublons."""

        completed_counts = Counter(completed)
        remaining: list[str] = []
        for name in planned:
            if completed_counts[name] > 0:
                completed_counts[name] -= 1
            else:
                remaining.append(name)
        return remaining

    @staticmethod
    def _merge_max_occurrences(
        existing: list[str],
        proposed: list[str],
    ) -> list[str]:
        """Conserve l'ordre et le plus grand nombre annonce de chaque etape."""

        merged = list(existing)
        existing_counts = Counter(existing)
        seen = Counter()
        for name in proposed:
            seen[name] += 1
            if seen[name] > existing_counts[name]:
                merged.append(name)
        return merged

    def _execute(
        self, state: AgentState, call: ToolCall, risk: RiskLevel
    ) -> ToolResult:
        started = perf_counter()
        result = self.registry.invoke(
            call,
            ToolContext(
                request_id=state.request_id,
                call_id=call.call_id,
                actor_id=state.actor_id,
                role=state.role,
                idempotency_key=self._write_action_key(
                    state,
                    call.name,
                    call.arguments,
                ),
            ),
        )
        if risk != RiskLevel.READ:
            state.write_call_ids.append(call.call_id)
            if result.status == ToolStatus.SUCCESS and not result.verified:
                result = result.model_copy(
                    update={
                        "status": ToolStatus.ERROR,
                        "message": "Action non confirmee par une relecture du PMS.",
                        "error_code": "unverified_write",
                    }
                )
            if result.status == ToolStatus.SUCCESS and result.verified:
                action_key = self._write_action_key(
                    state,
                    call.name,
                    call.arguments,
                )
                if action_key not in state.completed_write_actions:
                    state.completed_write_actions.append(action_key)
        self.trace_sink.record(
            TraceEvent(
                event_type="tool_finished",
                request_id=state.request_id,
                tool_name=call.name,
                status=result.status.value,
                duration_ms=round((perf_counter() - started) * 1000, 3),
                fields={
                    "argument_names": sorted(call.arguments),
                    "verified": result.verified,
                    "error_code": result.error_code,
                },
            )
        )
        self._record_plan_result(state, result)
        return result

    def _register_plan(self, state: AgentState, decision: AgentDecision) -> None:
        available = {spec.name for spec in self.registry.specs()}
        proposed = [name for name in decision.plan if name in available]
        if decision.tool_name in available and decision.tool_name not in proposed:
            proposed.insert(0, decision.tool_name)
        required = [
            name for name in decision.required_tools if name in available
        ]
        for name in required:
            if name not in proposed:
                proposed.append(name)
        if not proposed and not required:
            return
        event_type = "plan_created"
        if state.plan_revision == state.intent_revision:
            merged = self._merge_max_occurrences(
                state.workflow_plan,
                proposed,
            )
            merged_required = self._merge_max_occurrences(
                state.required_plan_steps,
                required,
            )
            if (
                merged == state.workflow_plan
                and merged_required == state.required_plan_steps
            ):
                return
            proposed = merged
            required = merged_required
            event_type = "plan_adapted"
        state.plan_revision = state.intent_revision
        state.workflow_plan = proposed
        state.required_plan_steps = required
        self.trace_sink.record(
            TraceEvent(
                event_type=event_type,
                request_id=state.request_id,
                tool_name=decision.tool_name,
                status="planned",
                fields={
                    "tools": proposed,
                    "required_tools": required,
                    "intent_revision": state.intent_revision,
                },
            )
        )

    def _apply_model_context(
        self,
        state: AgentState,
        decision: AgentDecision,
    ) -> None:
        goal_changed = (
            decision.new_goal
            and state.last_goal_change_turn != state.turn_index
        )
        if goal_changed:
            state.intent_revision += 1
            state.last_goal_change_turn = state.turn_index
            for message in reversed(state.messages):
                if message.role == "user" and message.turn_index == state.turn_index:
                    message.goal_revision = state.intent_revision
                    break
            state.plan_revision = -1
            state.workflow_plan = []
            state.required_plan_steps = []
            state.completed_plan_steps = []
            state.room_selection_strategy = None
            state.room_selection_evidence = None
            state.room_selection_turn = None
            self.trace_sink.record(
                TraceEvent(
                    event_type="goal_changed",
                    request_id=state.request_id,
                    status="active",
                    fields={
                        "turn_index": state.turn_index,
                        "goal_revision": state.intent_revision,
                    },
                )
            )
        if state.step_count == 0 and decision.room_selection_strategy is not None:
            evidence_is_grounded = self._selection_evidence_is_grounded(
                state,
                decision,
            )
            self.trace_sink.record(
                TraceEvent(
                    event_type="room_selection_interpreted",
                    request_id=state.request_id,
                    status="accepted" if evidence_is_grounded else "rejected",
                    fields={
                        "strategy": decision.room_selection_strategy,
                        "evidence": decision.room_selection_evidence,
                        "turn_index": state.turn_index,
                    },
                )
            )
            if evidence_is_grounded:
                state.room_selection_strategy = decision.room_selection_strategy
                state.room_selection_evidence = decision.room_selection_evidence
                state.room_selection_turn = state.turn_index
        if decision.memory is not None:
            state.working_memory = decision.memory.model_copy(deep=True)

    @staticmethod
    def _selection_evidence_is_grounded(
        state: AgentState,
        decision: AgentDecision,
    ) -> bool:
        strategy = decision.room_selection_strategy
        evidence = " ".join((decision.room_selection_evidence or "").casefold().split())
        if strategy is None or strategy == "only_available_type" or not evidence:
            return False
        user_text = " ".join(state.user_message.casefold().split())
        return evidence in user_text

    def _record_plan_result(self, state: AgentState, result: ToolResult) -> None:
        if result.status == ToolStatus.SUCCESS and result.verified:
            state.completed_plan_steps.append(result.tool_name)
            return
        self.trace_sink.record(
            TraceEvent(
                event_type="plan_adapted",
                request_id=state.request_id,
                tool_name=result.tool_name,
                status="blocked",
                fields={
                    "result_status": result.status.value,
                    "error_code": result.error_code,
                    "completed_tools": state.completed_plan_steps,
                },
            )
        )

    @staticmethod
    def _append_result(state: AgentState, result: ToolResult) -> None:
        state.tool_results.append(result)
        state.messages.append(
            AgentMessage(
                role="tool",
                content=result.model_dump_json(),
                turn_index=state.turn_index,
                goal_revision=state.intent_revision,
            )
        )

    @staticmethod
    def _unverified_write(state: AgentState) -> ToolResult | None:
        write_ids = set(state.write_call_ids)
        latest_write = next(
            (
                result
                for result in reversed(state.tool_results)
                if result.call_id in write_ids
            ),
            None,
        )
        if latest_write is None:
            return None
        if latest_write.status == ToolStatus.SUCCESS and latest_write.verified:
            return None
        return latest_write

    @staticmethod
    def _write_action_key(
        state: AgentState,
        tool_name: str,
        arguments: dict[str, object],
    ) -> str:
        payload = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        return (
            f"{state.request_id}:{state.intent_revision}:"
            f"{tool_name}:{fingerprint}"
        )

    def _clear_expired_confirmation(
        self, state: AgentState, validation_message: str
    ) -> None:
        if validation_message != "Confirmation expiree.":
            return
        state.pending_call = None
        state.pending_confirmation_id = None
        state.completed = True
        self.state_store.save(state)
        self.trace_sink.record(
            TraceEvent(
                event_type="confirmation_expired",
                request_id=state.request_id,
                status="expired",
            )
        )

    @staticmethod
    def _response(
        state: AgentState, status: AgentRunStatus, message: str
    ) -> AgentResponse:
        return AgentResponse(
            request_id=state.request_id,
            status=status,
            message=message,
            steps=state.step_count,
            tool_results=state.tool_results,
        )
