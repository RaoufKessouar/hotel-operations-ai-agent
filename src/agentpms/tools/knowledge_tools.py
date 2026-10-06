"""Tools qui exposent le RAG existant a l'agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agentpms.contracts import RiskLevel, ToolResult, ToolStatus
from agentpms.knowledge import KnowledgeClient

from .base import BaseTool, ToolContext


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class SearchKnowledgeInput(BaseModel):
    question: str = Field(min_length=2)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=12)


class SearchKnowledgeTool(BaseTool):
    name = "search_knowledge"
    description = (
        "Interroge le RAG verifie pour une politique hotel ou une procedure PMS. "
        "Ce tool informe mais n'effectue aucune action dans le PMS."
    )
    risk = RiskLevel.READ
    input_model = SearchKnowledgeInput

    def __init__(self, client: KnowledgeClient) -> None:
        self.client = client

    def execute(
        self, arguments: SearchKnowledgeInput, context: ToolContext
    ) -> ToolResult:
        result = self.client.ask(
            arguments.question,
            history=[item.model_dump() for item in arguments.history],
        )
        status = ToolStatus.NOT_FOUND if result.abstained else ToolStatus.SUCCESS
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=status,
            message=result.text,
            data={
                "route": result.route,
                "abstained": result.abstained,
                "citations_valid": result.citations_valid,
                "verification_decision": result.verification_decision,
            },
            citations=result.citations,
            verified=result.abstained or result.citations_valid,
        )

