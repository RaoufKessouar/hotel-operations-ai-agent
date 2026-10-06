"""Moteur de decision DeepSeek utilisant les appels de fonctions natifs."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import date
import json
from typing import Any

from openai import APIError, OpenAI

from agentpms.agent.decision import (
    DecisionModelUnavailableError,
    InvalidDecisionError,
)
from agentpms.agent.state import AgentState
from agentpms.contracts import AgentDecision, AgentWorkingMemory, DecisionKind
from agentpms.tools.base import ToolSpec

from .openai_compatible import OpenAICompatibleDecisionModel, SYSTEM


CONTROL_ARGUMENT = "_agent_control"
RESPOND_FUNCTION = "respond_to_user"
SELECTION_STRATEGIES = (
    "",
    "explicit_type",
    "cheapest",
    "most_expensive",
    "budget_match",
    "only_available_type",
)

_SEMANTIC_RULES = SYSTEM.partition("Responsabilites semantiques :")[2]
DEEPSEEK_SYSTEM = """Tu es le cerveau agentique d'un assistant de reception hoteliere.

Tu recois l'historique chronologique, la memoire de travail et les observations
verifiees des outils. A chaque etape, appelle exactement UNE fonction fournie :
- appelle directement un outil PMS pour effectuer la prochaine etape ;
- appelle respond_to_user avec response_type=clarify s'il manque une information
  reellement indispensable ;
- appelle respond_to_user avec response_type=final quand la demande est terminee.

Chaque fonction contient un argument technique _agent_control. Renseigne-le a
chaque appel. Il sert uniquement a conserver le plan public, les obligations du
workflow, la memoire conversationnelle et la provenance d'un choix de chambre.
Il sera retire avant l'execution de l'outil PMS. N'appelle jamais plusieurs
fonctions dans la meme reponse et ne produis aucun texte hors appel de fonction.

Responsabilites semantiques :
""" + _SEMANTIC_RULES


def _inline_local_refs(schema: dict[str, object]) -> dict[str, object]:
    """Remplace les $ref Pydantic locaux pour le validateur strict DeepSeek."""

    definitions = schema.get("$defs")
    known = definitions if isinstance(definitions, dict) else {}

    def resolve(value: object) -> object:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            target = known.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"Reference de schema inconnue: {reference}")
            merged = deepcopy(target)
            merged.update({key: item for key, item in value.items() if key != "$ref"})
            return resolve(merged)
        return {
            key: resolve(item)
            for key, item in value.items()
            if key != "$defs"
        }

    resolved = resolve(schema)
    if not isinstance(resolved, dict):
        raise ValueError("Schema racine invalide")
    return resolved


def _clean_strict_schema(value: object) -> object:
    """Adapte un schema Pydantic au sous-ensemble strict de DeepSeek."""

    if isinstance(value, list):
        return [_clean_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, object] = {}
    for key, item in value.items():
        if key in {"title", "default", "examples", "minLength", "maxLength"}:
            continue
        if key == "format" and item == "date":
            cleaned["description"] = "Date ISO au format AAAA-MM-JJ."
            continue
        cleaned[key] = _clean_strict_schema(item)
    if cleaned.get("type") == "object":
        properties = cleaned.get("properties")
        if isinstance(properties, dict):
            cleaned["required"] = list(properties)
        cleaned["additionalProperties"] = False
    return cleaned


def _pair_schema(*, value_name: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            value_name: {"type": "string"},
        },
        "required": ["name", value_name],
        "additionalProperties": False,
    }


def _control_schema(tool_names: list[str]) -> dict[str, object]:
    tool_enum = tool_names or ["none"]
    return {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "items": {"type": "string", "enum": tool_enum},
            },
            "required_tools": {
                "type": "array",
                "items": {"type": "string", "enum": tool_enum},
            },
            "memory_updated": {"type": "boolean"},
            "memory": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "summary": {"type": "string"},
                    "known_facts": {
                        "type": "array",
                        "items": _pair_schema(value_name="value_json"),
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "unavailable_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "selected_references": {
                        "type": "array",
                        "items": _pair_schema(value_name="value"),
                    },
                },
                "required": [
                    "objective",
                    "summary",
                    "known_facts",
                    "constraints",
                    "unavailable_fields",
                    "selected_references",
                ],
                "additionalProperties": False,
            },
            "new_goal": {"type": "boolean"},
            "respond_with_tool_result": {"type": "boolean"},
            "room_selection_strategy": {
                "type": "string",
                "enum": list(SELECTION_STRATEGIES),
            },
            "room_selection_evidence": {"type": "string"},
        },
        "required": [
            "plan",
            "required_tools",
            "memory_updated",
            "memory",
            "new_goal",
            "respond_with_tool_result",
            "room_selection_strategy",
            "room_selection_evidence",
        ],
        "additionalProperties": False,
    }


class DeepSeekDecisionModel(OpenAICompatibleDecisionModel):
    """Selectionne nativement une fonction, puis rend une AgentDecision sure."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 120.0,
        reasoning_effort: str = "low",
        today_provider: Callable[[], date] = date.today,
    ) -> None:
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.today_provider = today_provider

    @staticmethod
    def _function_tools(tools: list[ToolSpec]) -> list[dict[str, object]]:
        names = [tool.name for tool in tools]
        control = _control_schema(names)
        functions: list[dict[str, object]] = []
        for tool in tools:
            parameters = deepcopy(
                _clean_strict_schema(_inline_local_refs(tool.input_schema))
            )
            if not isinstance(parameters, dict):
                raise ValueError(f"Schema invalide pour {tool.name}")
            properties = parameters.setdefault("properties", {})
            if not isinstance(properties, dict):
                raise ValueError(f"Proprietes invalides pour {tool.name}")
            properties[CONTROL_ARGUMENT] = control
            parameters["required"] = list(properties)
            parameters["additionalProperties"] = False
            functions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": (
                            tool.description
                            + " L'execution et la verification restent locales."
                        ),
                        "strict": True,
                        "parameters": parameters,
                    },
                }
            )
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": RESPOND_FUNCTION,
                    "description": (
                        "Demande une precision indispensable a l'utilisateur ou "
                        "retourne la reponse finale une fois le workflow termine."
                    ),
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "response_type": {
                                "type": "string",
                                "enum": ["clarify", "final"],
                            },
                            "response": {"type": "string"},
                            CONTROL_ARGUMENT: control,
                        },
                        "required": [
                            "response_type",
                            "response",
                            CONTROL_ARGUMENT,
                        ],
                        "additionalProperties": False,
                    },
                },
            }
        )
        return functions

    @staticmethod
    def _memory(control: dict[str, Any]) -> AgentWorkingMemory | None:
        if control.get("memory_updated") is not True:
            return None
        raw = control.get("memory")
        if not isinstance(raw, dict):
            raise ValueError("Memoire DeepSeek absente")
        known_facts: dict[str, Any] = {}
        for item in raw.get("known_facts", []):
            if not isinstance(item, dict):
                raise ValueError("Fait memoire invalide")
            name = str(item.get("name") or "").strip()
            if not name:
                raise ValueError("Nom de fait memoire vide")
            encoded = str(item.get("value_json") or "null")
            try:
                known_facts[name] = json.loads(encoded)
            except json.JSONDecodeError:
                known_facts[name] = encoded
        selected_references = {
            str(item["name"]): str(item["value"])
            for item in raw.get("selected_references", [])
            if isinstance(item, dict) and item.get("name")
        }
        return AgentWorkingMemory(
            objective=str(raw.get("objective") or ""),
            summary=str(raw.get("summary") or ""),
            known_facts=known_facts,
            constraints=[str(item) for item in raw.get("constraints", [])],
            unavailable_fields=[
                str(item) for item in raw.get("unavailable_fields", [])
            ],
            selected_references=selected_references,
        )

    @classmethod
    def _decision_from_message(
        cls,
        message: object,
        available_tools: set[str],
    ) -> AgentDecision:
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls or len(tool_calls) != 1:
            raise ValueError("DeepSeek doit retourner exactement un appel de fonction")
        function = tool_calls[0].function
        try:
            payload = json.loads(function.arguments)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("Arguments de fonction DeepSeek invalides") from error
        if not isinstance(payload, dict):
            raise ValueError("Arguments DeepSeek non structures")
        control = payload.pop(CONTROL_ARGUMENT, None)
        if not isinstance(control, dict):
            raise ValueError("Bloc de controle DeepSeek absent")
        strategy = str(control.get("room_selection_strategy") or "") or None
        evidence = str(control.get("room_selection_evidence") or "") or None
        common = {
            "plan": control.get("plan") or [],
            "required_tools": control.get("required_tools") or [],
            "memory": cls._memory(control),
            "new_goal": bool(control.get("new_goal")),
            "respond_with_tool_result": bool(
                control.get("respond_with_tool_result")
            ),
            "room_selection_strategy": strategy,
            "room_selection_evidence": evidence,
        }
        if function.name == RESPOND_FUNCTION:
            response_type = payload.get("response_type")
            if response_type not in {"clarify", "final"}:
                raise ValueError("Type de reponse DeepSeek invalide")
            return AgentDecision(
                kind=DecisionKind(response_type),
                response=str(payload.get("response") or ""),
                **common,
            )
        if function.name not in available_tools:
            raise ValueError("DeepSeek a choisi un outil indisponible")
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name=function.name,
            arguments=payload,
            **common,
        )

    def _native_completion(
        self,
        payload: dict[str, object],
        tools: list[ToolSpec],
    ) -> object:
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DEEPSEEK_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            tools=self._function_tools(tools),
            reasoning_effort=self.reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}},
        )
        return completion.choices[0].message

    def _payload(
        self,
        state: AgentState,
        tools: list[ToolSpec],
        *,
        compact: bool = False,
        validation_error: str | None = None,
    ) -> dict[str, object]:
        current_date = self.today_provider().isoformat()
        conversation = (
            self._emergency_conversation(state)
            if compact
            else self._conversation(state)
        )
        payload: dict[str, object] = {
            "latest_user_message": state.user_message,
            "current_date": current_date,
            "timezone": "Europe/Paris",
            "response_language": "fr",
            "conversation": conversation,
            "working_memory": state.working_memory.model_dump(mode="json"),
            "remaining_steps": max(0, state.max_steps - state.step_count),
            "workflow": {
                "goal_revision": state.intent_revision,
                "plan": state.workflow_plan,
                "required_tools": state.required_plan_steps,
                "completed_tools": state.completed_plan_steps,
                "completed_write_actions": state.completed_write_actions,
                "room_selection": {
                    "strategy": state.room_selection_strategy,
                    "evidence": state.room_selection_evidence,
                    "source_turn": state.room_selection_turn,
                },
            },
        }
        if compact:
            payload["context_mode"] = "compact_retry"
        if validation_error:
            payload["previous_decision_error"] = validation_error[:1_000]
            payload["retry_instruction"] = (
                "Corrige la decision et appelle exactement une fonction autorisee."
            )
        return payload

    def decide(self, state: AgentState, tools: list[ToolSpec]) -> AgentDecision:
        available_tools = {tool.name for tool in tools}
        payload = self._payload(state, tools)
        try:
            message = self._native_completion(payload, tools)
        except APIError:
            try:
                message = self._native_completion(
                    self._payload(state, tools, compact=True), tools
                )
            except APIError as retry_error:
                raise DecisionModelUnavailableError(
                    "DeepSeek est indisponible apres une seconde tentative."
                ) from retry_error
        try:
            return self._decision_from_message(message, available_tools)
        except ValueError as error:
            try:
                repaired = self._native_completion(
                    self._payload(
                        state,
                        tools,
                        compact=True,
                        validation_error=str(error),
                    ),
                    tools,
                )
                return self._decision_from_message(repaired, available_tools)
            except APIError as retry_error:
                raise DecisionModelUnavailableError(
                    "DeepSeek a echoue pendant la correction de sa decision."
                ) from retry_error
            except ValueError as repair_error:
                raise InvalidDecisionError(
                    "DeepSeek n'a pas produit un appel de fonction exploitable."
                ) from repair_error
