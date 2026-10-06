"""Etat explicite et serialisable d'une execution agentique."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from agentpms.contracts import AgentWorkingMemory, ToolCall, ToolResult


class AgentMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    turn_index: int = Field(default=0, ge=0)
    goal_revision: int = Field(default=0, ge=0)


class AgentState(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    actor_id: str
    role: str
    user_message: str
    messages: list[AgentMessage] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    write_call_ids: list[str] = Field(default_factory=list)
    completed_write_actions: list[str] = Field(default_factory=list)
    intent_revision: int = Field(default=0, ge=0)
    turn_index: int = Field(default=0, ge=0)
    last_goal_change_turn: int = -1
    working_memory: AgentWorkingMemory = Field(default_factory=AgentWorkingMemory)
    room_selection_strategy: str | None = None
    room_selection_evidence: str | None = None
    room_selection_turn: int | None = Field(default=None, ge=0)
    plan_revision: int = -1
    workflow_plan: list[str] = Field(default_factory=list)
    required_plan_steps: list[str] = Field(default_factory=list)
    completed_plan_steps: list[str] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = Field(default=6, ge=1)
    pending_call: ToolCall | None = None
    pending_confirmation_id: str | None = None
    completed: bool = False


class StateStore(Protocol):
    def save(self, state: AgentState) -> None: ...
    def get(self, request_id: str) -> AgentState | None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._states: dict[str, AgentState] = {}

    def save(self, state: AgentState) -> None:
        self._states[state.request_id] = state.model_copy(deep=True)

    def get(self, request_id: str) -> AgentState | None:
        state = self._states.get(request_id)
        return state.model_copy(deep=True) if state is not None else None
