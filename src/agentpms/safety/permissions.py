"""Autorisations independantes du LLM."""

from __future__ import annotations

from agentpms.contracts import RiskLevel
from agentpms.tools.base import BaseTool


class PermissionPolicy:
    def __init__(
        self,
        allowed_tools: dict[str, set[str]],
        *,
        confirmation_risks: set[RiskLevel] | None = None,
    ) -> None:
        self.allowed_tools = {
            role: set(names) for role, names in allowed_tools.items()
        }
        self.confirmation_risks = confirmation_risks or {
            RiskLevel.SENSITIVE_WRITE
        }

    def is_allowed(self, role: str, tool: BaseTool) -> bool:
        allowed = self.allowed_tools.get(role, set())
        return "*" in allowed or tool.name in allowed

    def requires_confirmation(self, tool: BaseTool) -> bool:
        return tool.risk in self.confirmation_risks

