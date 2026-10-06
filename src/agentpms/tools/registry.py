"""Registre ferme : le LLM ne peut appeler que des tools enregistres."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from agentpms.contracts import ToolCall, ToolResult, ToolStatus

from .base import BaseTool, ToolContext, ToolSpec


class ToolRegistry:
    def __init__(self, tools: list[BaseTool] | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool deja enregistre : {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in sorted(self._tools)]

    def validate(
        self, call: ToolCall
    ) -> tuple[BaseTool, BaseModel] | ToolResult:
        tool = self.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                status=ToolStatus.ERROR,
                message="Tool inconnu ou non autorise.",
                error_code="unknown_tool",
            )
        try:
            arguments = tool.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                status=ToolStatus.ERROR,
                message="Arguments du tool invalides.",
                data={"validation_errors": exc.errors(include_input=False)},
                error_code="invalid_arguments",
            )
        return tool, arguments

    def invoke(self, call: ToolCall, context: ToolContext) -> ToolResult:
        prepared = self.validate(call)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, arguments = prepared
        try:
            return tool.execute(arguments, context)
        except Exception:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                status=ToolStatus.ERROR,
                message="Le tool a rencontre une erreur interne.",
                error_code="tool_exception",
                retryable=False,
            )

