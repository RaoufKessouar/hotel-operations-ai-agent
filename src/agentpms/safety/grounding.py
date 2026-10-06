"""Validation structurelle des decisions avant tout appel au PMS.

Cette couche ne tente jamais de comprendre la phrase de l'utilisateur. Le LLM
est responsable de l'intention, des references conversationnelles et des
valeurs extraites. La politique verifie uniquement la provenance PMS des
entites et impose les prerequis techniques des ecritures.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from agentpms.agent.state import AgentState
from agentpms.contracts import AgentDecision, DecisionKind, ToolResult, ToolStatus


def _verified_results(state: AgentState, tool_name: str) -> list[ToolResult]:
    return [
        result
        for result in reversed(state.tool_results)
        if result.tool_name == tool_name
        and result.status == ToolStatus.SUCCESS
        and result.verified
    ]


def _matching_result(
    state: AgentState,
    tool_name: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> ToolResult | None:
    return next(
        (
            result
            for result in _verified_results(state, tool_name)
            if predicate(result.data)
        ),
        None,
    )


def _same(value: object, expected: object) -> bool:
    return str(value or "") == str(expected or "")


def _reservation_was_loaded(state: AgentState, reservation_id: str) -> bool:
    return (
        _matching_result(
            state,
            "get_reservation",
            lambda data: _same(
                (data.get("reservation") or {}).get("reservation_id"),
                reservation_id,
            ),
        )
        is not None
    )


def _customer_request_was_loaded(state: AgentState, request_id: str) -> bool:
    return (
        _matching_result(
            state,
            "get_customer_request",
            lambda data: _same(
                (data.get("request") or {}).get("request_id"),
                request_id,
            ),
        )
        is not None
    )


def _known_reservation_ids(state: AgentState) -> set[str]:
    identifiers: set[str] = set()
    for result in state.tool_results:
        if result.status != ToolStatus.SUCCESS or not result.verified:
            continue
        reservation = result.data.get("reservation")
        if isinstance(reservation, dict) and reservation.get("reservation_id"):
            identifiers.add(str(reservation["reservation_id"]))
        for item in result.data.get("reservations", []):
            if isinstance(item, dict) and item.get("reservation_id"):
                identifiers.add(str(item["reservation_id"]))
    return identifiers


_MONEY_PATTERN = re.compile(
    r"(?<!\d)(\d[\d\s\u00a0\u202f.,]*\d|\d)\s*(?:EUR|€)",
    flags=re.IGNORECASE,
)


def _money_decimal(value: object) -> Decimal | None:
    raw = str(value).strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _verified_money_values(state: AgentState) -> set[Decimal]:
    values: set[Decimal] = set()

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if any(marker in key for marker in ("price", "total", "amount")):
            parsed = _money_decimal(value)
            if parsed is not None:
                values.add(parsed)

    for result in state.tool_results:
        if result.status == ToolStatus.SUCCESS and result.verified:
            visit(result.data)
    return values


class DecisionGroundingPolicy:
    """Impose des preuves PMS sans router le langage naturel."""

    def __init__(self, default_room_selection_strategy: str = "ask") -> None:
        if default_room_selection_strategy not in {
            "cheapest",
            "most_expensive",
            "ask",
        }:
            raise ValueError("strategie de chambre par defaut invalide")
        self.default_room_selection_strategy = default_room_selection_strategy

    def recover(self, state: AgentState) -> AgentDecision | None:
        """Aucune decision semantique n'est fabriquee si le modele echoue."""
        return None

    def review(self, state: AgentState, decision: AgentDecision) -> AgentDecision:
        if decision.kind == DecisionKind.CLARIFY:
            return self._ground_monetary_response(state, decision)
        if decision.kind == DecisionKind.FINAL:
            return self._ground_monetary_response(state, decision)
        if decision.kind != DecisionKind.TOOL:
            return decision
        handlers = {
            "create_reservation": self._review_create_reservation,
            "create_reservations": self._review_create_reservations,
            "update_reservation": self._review_reservation_write,
            "check_in_reservation": self._review_reservation_write,
            "check_out_reservation": self._review_reservation_write,
            "issue_invoice": self._review_reservation_write,
            "issue_invoices": self._review_issue_invoices,
            "add_extra": self._review_add_extra,
            "reply_customer_request": self._review_customer_reply,
        }
        handler = handlers.get(decision.tool_name or "")
        return handler(state, decision) if handler is not None else decision

    @staticmethod
    def _ground_monetary_response(
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        response = decision.response or ""
        mentioned = [
            value
            for match in _MONEY_PATTERN.finditer(response)
            if (value := _money_decimal(match.group(1))) is not None
        ]
        if not mentioned:
            return decision
        allowed = _verified_money_values(state)
        if allowed and all(value in allowed for value in mentioned):
            return decision
        latest = next(
            (
                result
                for result in reversed(state.tool_results)
                if result.status == ToolStatus.SUCCESS and result.verified
            ),
            None,
        )
        if latest is None:
            return decision.model_copy(
                update={
                    "response": (
                        "Je ne peux pas annoncer ce montant sans resultat "
                        "financier verifie du PMS."
                    )
                }
            )
        suffix = (
            "\nPrecisez votre choix pour continuer."
            if decision.kind == DecisionKind.CLARIFY
            else ""
        )
        return decision.model_copy(update={"response": latest.message + suffix})

    def _review_create_reservation(
        self,
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        arguments = dict(decision.arguments)
        known = state.working_memory.known_facts
        unavailable = set(state.working_memory.unavailable_fields)
        for field in ("email", "phone"):
            proposed = arguments.get(field)
            expected = known.get(field)
            if field in unavailable or expected in (None, ""):
                arguments[field] = None
            elif str(proposed or "").casefold() != str(expected).casefold():
                arguments[field] = None
        decision = decision.model_copy(update={"arguments": arguments})
        required = (
            "arrival_date",
            "departure_date",
            "adults",
            "children",
            "room_type_id",
            "room_id",
        )
        if any(arguments.get(name) in (None, "") for name in required):
            # Le registre retournera invalid_arguments au modele avec le schema
            # exact a corriger. Cette politique n'invente aucune valeur.
            return decision

        def same_stay(data: dict[str, Any]) -> bool:
            if not _same(data.get("arrival_date"), arguments["arrival_date"]):
                return False
            if not _same(data.get("departure_date"), arguments["departure_date"]):
                return False
            if data.get("adults") is not None and int(data["adults"]) != int(
                arguments["adults"]
            ):
                return False
            if data.get("children") is not None and int(data["children"]) != int(
                arguments["children"]
            ):
                return False
            return True

        availability = _matching_result(state, "check_availability", same_stay)
        if availability is not None:
            selected = self._review_room_selection(
                state,
                decision,
                availability,
            )
            if selected.kind != DecisionKind.TOOL:
                return selected
            selected_arguments = selected.arguments
            if any(
                isinstance(room, dict)
                and room.get("available") is True
                and _same(
                    room.get("room_type_id"),
                    selected_arguments["room_type_id"],
                )
                and _same(room.get("room_id"), selected_arguments["room_id"])
                for room in availability.data.get("rooms", [])
            ):
                return selected

        # Une creation ne peut jamais s'appuyer sur une chambre imaginee ou sur
        # une ancienne recherche portant sur d'autres dates/voyageurs.
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="check_availability",
            arguments={
                "arrival_date": arguments["arrival_date"],
                "departure_date": arguments["departure_date"],
                "room_type": None,
                "adults": arguments["adults"],
                "children": arguments["children"],
            },
            plan=list(
                dict.fromkeys(
                    ["check_availability", *decision.plan, "create_reservation"]
                )
            ),
            required_tools=decision.required_tools,
            memory=decision.memory,
            new_goal=decision.new_goal,
            room_selection_strategy=decision.room_selection_strategy,
            room_selection_evidence=decision.room_selection_evidence,
        )

    def _review_create_reservations(
        self,
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        raw_reservations = decision.arguments.get("reservations")
        if not isinstance(raw_reservations, list) or not raw_reservations:
            return decision
        reservations = [
            dict(item) for item in raw_reservations if isinstance(item, dict)
        ]
        if len(reservations) != len(raw_reservations):
            return decision
        dates = {
            (
                str(item.get("arrival_date") or ""),
                str(item.get("departure_date") or ""),
            )
            for item in reservations
        }
        if len(dates) != 1:
            return AgentDecision(
                kind=DecisionKind.CLARIFY,
                response=(
                    "La creation multiple doit porter sur une meme periode. "
                    "Separez les sejours ayant des dates differentes."
                ),
                plan=decision.plan,
                required_tools=decision.required_tools,
                memory=decision.memory,
                new_goal=decision.new_goal,
            )
        arrival_date, departure_date = next(iter(dates))
        allocation = _matching_result(
            state,
            "plan_room_allocation",
            lambda data: (
                _same(data.get("arrival_date"), arrival_date)
                and _same(data.get("departure_date"), departure_date)
                and int(data.get("requested_count") or 0) == len(reservations)
            ),
        )
        if allocation is None:
            strategy = (
                state.room_selection_strategy
                or self.default_room_selection_strategy
            )
            if strategy not in {"cheapest", "most_expensive"}:
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response=(
                        "Plusieurs categories peuvent convenir a cette operation "
                        "multiple. Quelle categorie souhaitez-vous retenir pour "
                        "les chambres du groupe ?"
                    ),
                    plan=decision.plan,
                    required_tools=decision.required_tools,
                    memory=decision.memory,
                    new_goal=decision.new_goal,
                )
            requirements = [
                {
                    "reference": str(item.get("reference") or f"room-{index}"),
                    "adults": int(item.get("adults") or 1),
                    "children": int(item.get("children") or 0),
                }
                for index, item in enumerate(reservations, start=1)
            ]
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="plan_room_allocation",
                arguments={
                    "arrival_date": arrival_date,
                    "departure_date": departure_date,
                    "requirements": requirements,
                    "selection_strategy": strategy,
                    "include_breakfast": bool(
                        state.working_memory.known_facts.get("include_breakfast")
                        or state.working_memory.known_facts.get("breakfast")
                    ),
                },
                plan=list(
                    dict.fromkeys(
                        ["plan_room_allocation", *decision.plan, "create_reservations"]
                    )
                ),
                required_tools=decision.required_tools,
                memory=decision.memory,
                new_goal=decision.new_goal,
                room_selection_strategy=decision.room_selection_strategy,
                room_selection_evidence=decision.room_selection_evidence,
            )

        by_reference = {
            str(item.get("reference") or ""): item
            for item in allocation.data.get("allocations", [])
            if isinstance(item, dict)
        }
        grounded: list[dict[str, Any]] = []
        unavailable = set(state.working_memory.unavailable_fields)
        for item in reservations:
            reference = str(item.get("reference") or "")
            selected = by_reference.get(reference)
            if selected is None:
                return decision
            if (
                int(item.get("adults") or 0) != int(selected.get("adults") or 0)
                or int(item.get("children") or 0)
                != int(selected.get("children") or 0)
            ):
                return decision
            item["room_id"] = str(selected.get("room_id") or "")
            item["room_type_id"] = str(selected.get("room_type_id") or "")
            for field in ("email", "phone"):
                if field in unavailable:
                    item[field] = None
            grounded.append(item)
        if len({item["room_id"] for item in grounded}) != len(grounded):
            return decision
        return decision.model_copy(
            update={"arguments": {"reservations": grounded}}
        )

    def _review_room_selection(
        self,
        state: AgentState,
        decision: AgentDecision,
        availability: ToolResult,
    ) -> AgentDecision:
        room_types = [
            category
            for category in availability.data.get("room_types", [])
            if isinstance(category, dict)
            and int(category.get("available_count") or 0) > 0
        ]
        if len(room_types) <= 1:
            return decision

        strategy = (
            state.room_selection_strategy
            or self.default_room_selection_strategy
        )
        if strategy in {None, "only_available_type", "ask"}:
            currency = availability.data.get("currency") or "EUR"
            choices = "; ".join(
                f"{category.get('name')} : "
                f"{category.get('total_price_with_tax')} {currency} TTC"
                for category in room_types
            )
            return AgentDecision(
                kind=DecisionKind.CLARIFY,
                response=(
                    "Voici les categories disponibles pour ce sejour : "
                    f"{choices}. Quelle categorie souhaitez-vous retenir pour "
                    "le client ?"
                ),
                plan=decision.plan,
                required_tools=decision.required_tools,
                memory=decision.memory,
                new_goal=decision.new_goal,
                room_selection_strategy=decision.room_selection_strategy,
                room_selection_evidence=decision.room_selection_evidence,
            )

        if strategy not in {"cheapest", "most_expensive"}:
            return decision

        priced: list[tuple[Decimal, dict[str, Any]]] = []
        for category in room_types:
            try:
                priced.append(
                    (Decimal(str(category.get("total_price_with_tax"))), category)
                )
            except (InvalidOperation, TypeError):
                continue
        if not priced:
            return decision
        target = (
            min(priced, key=lambda item: item[0])[1]
            if strategy == "cheapest"
            else max(priced, key=lambda item: item[0])[1]
        )
        rooms = [
            room
            for room in availability.data.get("rooms", [])
            if isinstance(room, dict)
            and room.get("available") is True
            and _same(room.get("room_type_id"), target.get("room_type_id"))
        ]
        if not rooms:
            return decision
        arguments = dict(decision.arguments)
        arguments["room_type_id"] = str(target.get("room_type_id") or "")
        arguments["room_id"] = str(rooms[0].get("room_id") or "")
        return decision.model_copy(update={"arguments": arguments})

    @staticmethod
    def _review_reservation_write(
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        reservation_id = str(decision.arguments.get("reservation_id") or "")
        if not reservation_id or _reservation_was_loaded(state, reservation_id):
            return decision
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="get_reservation",
            arguments={"reservation_id": reservation_id},
            plan=list(
                dict.fromkeys(
                    ["get_reservation", *decision.plan, decision.tool_name or ""]
                )
            ),
            required_tools=decision.required_tools,
            memory=decision.memory,
            new_goal=decision.new_goal,
        )

    @staticmethod
    def _review_issue_invoices(
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        raw = decision.arguments.get("reservation_ids")
        if not isinstance(raw, list) or not raw:
            return decision
        reservation_ids = [str(value) for value in raw]
        missing = [
            value
            for value in reservation_ids
            if value not in _known_reservation_ids(state)
        ]
        if not missing:
            return decision
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="get_reservations",
            arguments={"reservation_ids": missing},
            plan=list(
                dict.fromkeys(
                    ["get_reservations", *decision.plan, "issue_invoices"]
                )
            ),
            required_tools=list(
                dict.fromkeys(
                    [
                        "get_reservations",
                        *decision.required_tools,
                        "issue_invoices",
                    ]
                )
            ),
            memory=decision.memory,
            new_goal=decision.new_goal,
        )

    @classmethod
    def _review_add_extra(
        cls,
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        prepared = cls._review_reservation_write(state, decision)
        if prepared.tool_name != "add_extra":
            return prepared
        arguments = dict(prepared.arguments)
        # Le catalogue PMS, et non le modele, fixe le code, le libelle et le
        # prix du seul extra actuellement expose par ce tool.
        arguments["extra_code"] = "BREAKFAST"
        arguments["label"] = "Petit-dejeuner"
        arguments.pop("unit_price", None)
        reservation_result = _matching_result(
            state,
            "get_reservation",
            lambda data: _same(
                (data.get("reservation") or {}).get("reservation_id"),
                arguments.get("reservation_id"),
            ),
        )
        reservation = (
            reservation_result.data.get("reservation", {})
            if reservation_result is not None
            else {}
        )
        if isinstance(reservation, dict):
            quote = _matching_result(
                state,
                "quote_stay",
                lambda data: (
                    _same(data.get("arrival_date"), reservation.get("arrival_date"))
                    and _same(
                        data.get("departure_date"),
                        reservation.get("departure_date"),
                    )
                    and (
                        data.get("adults") is None
                        or int(data["adults"])
                        == int(reservation.get("adults") or 0)
                    )
                    and isinstance(data.get("breakfast"), dict)
                    and data["breakfast"].get("included") is True
                ),
            )
            breakfast = (
                quote.data.get("breakfast", {}) if quote is not None else {}
            )
            if isinstance(breakfast, dict):
                quoted_quantity = int(
                    breakfast.get("quantity_per_charge") or 0
                )
                if quoted_quantity > 0:
                    # Le devis PMS est la source de verite. Le modele ne peut
                    # pas confondre le nombre de nuits avec la quantite par
                    # facturation du service.
                    arguments["quantity"] = quoted_quantity
        return prepared.model_copy(update={"arguments": arguments})

    @staticmethod
    def _review_customer_reply(
        state: AgentState,
        decision: AgentDecision,
    ) -> AgentDecision:
        request_id = str(decision.arguments.get("request_id") or "")
        if not request_id or _customer_request_was_loaded(state, request_id):
            return decision
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="get_customer_request",
            arguments={"request_id": request_id},
            plan=list(
                dict.fromkeys(
                    [
                        "get_customer_request",
                        *decision.plan,
                        "reply_customer_request",
                    ]
                )
            ),
            required_tools=decision.required_tools,
            memory=decision.memory,
            new_goal=decision.new_goal,
        )
