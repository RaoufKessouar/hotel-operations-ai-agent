"""Contrats partages entre l'agent, les tools et les adaptateurs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class RiskLevel(StrEnum):
    READ = "read"
    ROUTINE_WRITE = "routine_write"
    SENSITIVE_WRITE = "sensitive_write"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"
    FORBIDDEN = "forbidden"
    ERROR = "error"


class DecisionKind(StrEnum):
    TOOL = "tool"
    CLARIFY = "clarify"
    FINAL = "final"


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    NEEDS_CONFIRMATION = "needs_confirmation"
    DENIED = "denied"
    FORBIDDEN = "forbidden"
    STOPPED = "stopped"
    ERROR = "error"


class Citation(BaseModel):
    index: int = Field(ge=1)
    title: str
    detail: str = ""
    url: str | None = None
    score: float | None = None
    timestamp_start: float | None = None
    images: list[str] = Field(default_factory=list)


class KnowledgeResult(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    abstained: bool = False
    route: str = "both"
    citations_valid: bool = False
    verification_decision: str = "not_run"


class ToolCall(BaseModel):
    call_id: str = Field(default_factory=lambda: uuid4().hex)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    status: ToolStatus
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    external_operation_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    verified: bool = False


class AgentWorkingMemory(BaseModel):
    """Memoire de travail explicite maintenue par le modele entre les tours.

    Elle ne donne aucune autorisation d'execution. Les valeurs utilisees par un
    tool d'ecriture restent validees par le registre, les politiques de securite
    et l'adaptateur PMS.
    """

    objective: str = ""
    summary: str = ""
    known_facts: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    unavailable_fields: list[str] = Field(default_factory=list, max_length=20)
    selected_references: dict[str, str] = Field(default_factory=dict)


class AgentDecision(BaseModel):
    kind: DecisionKind
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    response: str | None = None
    plan: list[str] = Field(default_factory=list, max_length=64)
    required_tools: list[str] = Field(default_factory=list, max_length=64)
    memory: AgentWorkingMemory | None = None
    new_goal: bool = False
    respond_with_tool_result: bool = False
    room_selection_strategy: Literal[
        "explicit_type",
        "cheapest",
        "most_expensive",
        "budget_match",
        "only_available_type",
    ] | None = None
    room_selection_evidence: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _coherent_decision(self) -> "AgentDecision":
        if self.kind == DecisionKind.TOOL and not self.tool_name:
            raise ValueError("Une decision tool requiert tool_name")
        if self.kind != DecisionKind.TOOL and not self.response:
            raise ValueError("Une decision finale ou de clarification requiert response")
        return self


class ConfirmationPrompt(BaseModel):
    confirmation_id: str
    request_id: str
    tool_name: str
    summary: str
    expires_at: datetime


class AgentResponse(BaseModel):
    request_id: str
    status: AgentRunStatus
    message: str
    steps: int = 0
    tool_results: list[ToolResult] = Field(default_factory=list)
    confirmation: ConfirmationPrompt | None = None
