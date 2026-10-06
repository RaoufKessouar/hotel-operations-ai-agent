"""Contrat d'un tool autorise par l'orchestrateur."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from agentpms.contracts import RiskLevel, ToolResult


class ToolContext(BaseModel):
    request_id: str
    call_id: str
    actor_id: str
    role: str
    idempotency_key: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    risk: RiskLevel
    input_schema: dict[str, Any]


class BaseTool(ABC):
    name: str
    description: str
    risk: RiskLevel = RiskLevel.READ
    return_direct: bool = False
    input_model: type[BaseModel]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            risk=self.risk,
            input_schema=self.input_model.model_json_schema(),
        )

    def confirmation_summary(self, arguments: BaseModel) -> str:
        return f"Executer l'action {self.name}."

    @abstractmethod
    def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        raise NotImplementedError
