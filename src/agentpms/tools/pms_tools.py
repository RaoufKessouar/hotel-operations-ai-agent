"""Tools PMS metier. Aucun endpoint arbitraire n'est expose au LLM."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentpms.contracts import RiskLevel, ToolResult, ToolStatus
from agentpms.pms import PMSAdapter, PMSResult

from .base import BaseTool, ToolContext


def _tool_result(name: str, call_id: str, result: PMSResult) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name=name,
        status=ToolStatus(result.status.value),
        message=result.message,
        data=result.data,
        external_operation_id=result.external_operation_id,
        error_code=result.error_code,
        retryable=result.retryable,
        verified=result.verified,
    )


def _fr_number(value: object) -> str:
    return str(value).replace(".", ",")


def _format_occupancy(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    data = result.data
    occupied_label = (
        "chambre occupée"
        if data["occupied_rooms"] == 1
        else "chambres occupées"
    )
    available_label = (
        "chambre disponible"
        if data["available_rooms"] == 1
        else "chambres disponibles"
    )
    return result.model_copy(
        update={
            "message": (
                f"Pour la nuit du {data['stay_date']}, "
                f"{data['occupied_rooms']} {occupied_label} sur "
                f"{data['total_rooms']}, soit "
                f"{_fr_number(data['occupancy_rate_percent'])} %. "
                f"Il reste {data['available_rooms']} {available_label}."
            )
        }
    )


def _format_occupancy_range(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    data = result.data
    days = [day for day in data.get("days", []) if isinstance(day, dict)]
    grouped: list[dict[str, object]] = []
    for day in days:
        signature = (
            day.get("total_rooms"),
            day.get("occupied_rooms"),
            day.get("occupancy_rate_percent"),
        )
        if grouped and grouped[-1]["signature"] == signature:
            grouped[-1]["end_date"] = day.get("stay_date")
            continue
        grouped.append(
            {
                "start_date": day.get("stay_date"),
                "end_date": day.get("stay_date"),
                "total_rooms": day.get("total_rooms"),
                "occupied_rooms": day.get("occupied_rooms"),
                "occupancy_rate_percent": day.get("occupancy_rate_percent"),
                "signature": signature,
            }
        )
    details = []
    for group in grouped:
        period = str(group["start_date"])
        if group["end_date"] != group["start_date"]:
            period += f" au {group['end_date']}"
        details.append(
            f"{period} : {group['occupied_rooms']}/{group['total_rooms']} "
            "chambre(s) occupee(s) "
            f"({_fr_number(group['occupancy_rate_percent'])} %)"
        )
    message = (
        f"Du {data['start_date']} au {data['end_date']}, le taux d'occupation "
        "moyen est de "
        f"{_fr_number(data['average_occupancy_rate_percent'])} %."
    )
    if details:
        message += " " + " ; ".join(details) + "."
    return result.model_copy(update={"message": message})


def _format_stay_list(
    result: ToolResult,
    *,
    date_key: str,
    singular: str,
    plural: str,
) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    reservations = [
        item for item in result.data.get("reservations", []) if isinstance(item, dict)
    ]
    count = len(reservations)
    label = singular if count == 1 else plural
    message = f"{count} {label} le {result.data.get(date_key)}."
    if reservations:
        details = [
            (
                f"#{item.get('reservation_id')} — {item.get('guest_name')} — "
                f"{item.get('room_type') or 'type non renseigné'} — "
                f"statut {item.get('status') or 'non renseigné'}"
            )
            for item in reservations
        ]
        message += "\n" + "\n".join(details)
    return result.model_copy(update={"message": message})


def _format_availability(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    data = result.data
    rooms = [item for item in data.get("rooms", []) if isinstance(item, dict)]
    room_types = [
        item for item in data.get("room_types", []) if isinstance(item, dict)
    ]
    currency = data.get("currency") or "EUR"
    message = (
        f"Du {data.get('arrival_date')} au {data.get('departure_date')}, "
        f"{len(rooms)} chambre(s) sont disponibles pour "
        f"{data.get('adults')} adulte(s) et {data.get('children')} enfant(s)."
    )
    if room_types:
        details = []
        for category in room_types:
            available_count = int(category.get("available_count") or 0)
            if available_count <= 0:
                details.append(
                    f"{category.get('name')} : indisponible pour cette occupation"
                )
                continue
            total = category.get("total_price_with_tax")
            price = (
                f", total {total} {currency} TTC"
                if total not in (None, "")
                else ", tarif non communiqué par le PMS"
            )
            details.append(
                f"{category.get('name')} : "
                f"{available_count} disponible(s){price}"
            )
        message += "\n" + "\n".join(details)
    return result.model_copy(update={"message": message})


def _format_invoice(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    invoice = result.data.get("invoice") or {}
    currency = invoice.get("currency") or "EUR"
    message = (
        f"Facture #{invoice.get('number')} — réservation "
        f"{invoice.get('reservation_id')} — {invoice.get('guest_name')}. "
        f"Total HT : {invoice.get('total_excluding_tax')} {currency}; "
        f"total TTC : {invoice.get('total_including_tax')} {currency}."
    )
    lines = [item for item in invoice.get("lines", []) if isinstance(item, dict)]
    if lines:
        message += "\n" + "\n".join(
            f"{item.get('description')} : {item.get('quantity')} × "
            f"{item.get('unit_price_including_tax')} {currency} TTC = "
            f"{item.get('total_including_tax')} {currency} TTC"
            for item in lines
        )
    return result.model_copy(update={"message": message})


def _format_customer_requests(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    requests = [
        item for item in result.data.get("requests", []) if isinstance(item, dict)
    ]
    message = f"{len(requests)} demande(s) client trouvée(s)."
    if requests:
        message += "\n" + "\n".join(
            f"#{item.get('request_id')} — {item.get('customer_name')} — "
            f"{item.get('subject')} — statut {item.get('status')}"
            for item in requests
        )
    return result.model_copy(update={"message": message})


def _format_customer_request(result: ToolResult) -> ToolResult:
    if result.status != ToolStatus.SUCCESS or not result.verified:
        return result
    request = result.data.get("request") or {}
    message = (
        f"Demande #{request.get('request_id')} — {request.get('customer_name')} — "
        f"{request.get('subject')} — statut {request.get('status')}."
    )
    messages = [
        item for item in request.get("messages", []) if isinstance(item, dict)
    ]
    if messages:
        message += "\n" + "\n".join(
            f"{item.get('sender')} ({item.get('created_at')}): "
            f"{item.get('message')}"
            for item in messages
        )
    return result.model_copy(update={"message": message})


class SearchReservationsInput(BaseModel):
    guest_name: str | None = Field(default=None, min_length=2)
    reservation_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _has_criterion(self) -> "SearchReservationsInput":
        if not self.guest_name and not self.reservation_id:
            raise ValueError("guest_name ou reservation_id est requis")
        return self


class SearchReservationsTool(BaseTool):
    name = "search_reservations"
    description = "Recherche une reservation par nom de client ou identifiant."
    risk = RiskLevel.READ
    input_model = SearchReservationsInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: SearchReservationsInput, context: ToolContext
    ) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.search_reservations(
                guest_name=arguments.guest_name,
                reservation_id=arguments.reservation_id,
            ),
        )


class QueryStaysInput(BaseModel):
    stay_date: date | None = None
    arrival_date: date | None = None
    departure_date: date | None = None
    guest_name: str | None = Field(default=None, min_length=2)
    room_type: str | None = Field(default=None, min_length=2)
    status: Literal[
        "all",
        "confirmed",
        "checked_in",
        "checked_out",
        "cancelled",
        "refunded",
    ] = "all"

    @model_validator(mode="after")
    def _has_filter(self) -> "QueryStaysInput":
        if not any(
            value is not None
            for value in (
                self.stay_date,
                self.arrival_date,
                self.departure_date,
                self.guest_name,
                self.room_type,
            )
        ) and self.status == "all":
            raise ValueError("au moins un filtre de sejour est requis")
        return self


class QueryStaysTool(BaseTool):
    name = "query_stays"
    description = (
        "Interroge les sejours avec des filtres metier generiques : presence a "
        "une date (stay_date), arrivee, depart, client, categorie ou statut. "
        "Pour 'in stay', utilise stay_date; la convention est arrivee <= date "
        "< depart. Lecture seule, sans inventer de client."
    )
    risk = RiskLevel.READ
    input_model = QueryStaysInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self,
        arguments: QueryStaysInput,
        context: ToolContext,
    ) -> ToolResult:
        result = _tool_result(
            self.name,
            context.call_id,
            self.adapter.query_stays(**arguments.model_dump()),
        )
        if result.status != ToolStatus.SUCCESS or not result.verified:
            return result
        reservations = [
            item
            for item in result.data.get("reservations", [])
            if isinstance(item, dict)
        ]
        scope = []
        if arguments.stay_date:
            scope.append(f"presents le {arguments.stay_date.isoformat()}")
        if arguments.arrival_date:
            scope.append(f"arrivee le {arguments.arrival_date.isoformat()}")
        if arguments.departure_date:
            scope.append(f"depart le {arguments.departure_date.isoformat()}")
        if arguments.guest_name:
            scope.append(f"client {arguments.guest_name}")
        if arguments.room_type:
            scope.append(f"categorie {arguments.room_type}")
        lines = [
            (
                f"#{item.get('reservation_id')} — {item.get('guest_name')} — "
                f"{item.get('room_type') or 'chambre non renseignee'} "
                f"{item.get('room_id') or ''} — "
                f"du {item.get('arrival_date')} au {item.get('departure_date')} — "
                f"statut {item.get('status')}"
            ).replace("  ", " ")
            for item in reservations
        ]
        message = (
            f"{len(reservations)} sejour(s) trouve(s)"
            + (" pour " + ", ".join(scope) if scope else "")
            + "."
        )
        if lines:
            message += "\n" + "\n".join(lines)
        return result.model_copy(update={"message": message})


class GetReservationInput(BaseModel):
    reservation_id: str = Field(min_length=1)


class GetReservationTool(BaseTool):
    name = "get_reservation"
    description = "Charge les details verifies d'une reservation unique."
    risk = RiskLevel.READ
    input_model = GetReservationInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: GetReservationInput, context: ToolContext
    ) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.get_reservation(arguments.reservation_id),
        )


class GetReservationsInput(BaseModel):
    reservation_ids: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_reservations(self) -> "GetReservationsInput":
        values = [value.strip() for value in self.reservation_ids]
        if any(not value for value in values):
            raise ValueError("les identifiants de reservation sont requis")
        if len(values) != len(set(values)):
            raise ValueError("chaque reservation doit apparaitre une seule fois")
        self.reservation_ids = values
        return self


class GetReservationsTool(BaseTool):
    name = "get_reservations"
    description = (
        "Charge et verifie plusieurs reservations a partir de leur liste exacte "
        "d'identifiants. Utiliser avant une operation portant sur plusieurs dossiers."
    )
    risk = RiskLevel.READ
    input_model = GetReservationsInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self,
        arguments: GetReservationsInput,
        context: ToolContext,
    ) -> ToolResult:
        reservations: list[dict[str, object]] = []
        operations: list[dict[str, object]] = []
        for reservation_id in arguments.reservation_ids:
            result = self.adapter.get_reservation(reservation_id)
            operations.append(
                {
                    "reservation_id": reservation_id,
                    "status": result.status.value,
                    "message": result.message,
                    "verified": result.verified,
                }
            )
            reservation = result.data.get("reservation")
            if (
                result.status.value == ToolStatus.SUCCESS.value
                and result.verified
                and isinstance(reservation, dict)
                and str(reservation.get("reservation_id") or "") == reservation_id
            ):
                reservations.append(reservation)
                continue
            return ToolResult(
                call_id=context.call_id,
                tool_name=self.name,
                status=ToolStatus.ERROR,
                message=(
                    f"Verification multiple incomplete : {len(reservations)}/"
                    f"{len(arguments.reservation_ids)} reservation(s) verifiee(s). "
                    f"Echec pour #{reservation_id} : {result.message}"
                ),
                data={"reservations": reservations, "operations": operations},
                error_code=result.error_code or "reservation_batch_verification_failed",
                retryable=result.retryable,
                verified=False,
            )
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message=(
                f"{len(reservations)} reservation(s) chargee(s) et verifiee(s) "
                "dans QloApps : "
                + ", ".join(
                    f"#{reservation.get('reservation_id')}"
                    for reservation in reservations
                )
                + "."
            ),
            data={"reservations": reservations, "operations": operations},
            verified=True,
        )


class ListArrivalsInput(BaseModel):
    arrival_date: date


class ListArrivalsTool(BaseTool):
    name = "list_arrivals"
    description = "Liste les arrivees prevues a une date donnee."
    risk = RiskLevel.READ
    return_direct = True
    input_model = ListArrivalsInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(self, arguments: ListArrivalsInput, context: ToolContext) -> ToolResult:
        return _format_stay_list(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.list_arrivals(arrival_date=arguments.arrival_date),
            ),
            date_key="arrival_date",
            singular="arrivée prévue",
            plural="arrivées prévues",
        )


class ListDeparturesInput(BaseModel):
    departure_date: date


class ListDeparturesTool(BaseTool):
    name = "list_departures"
    description = "Liste les departs prevus a une date donnee."
    risk = RiskLevel.READ
    return_direct = True
    input_model = ListDeparturesInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: ListDeparturesInput, context: ToolContext
    ) -> ToolResult:
        return _format_stay_list(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.list_departures(departure_date=arguments.departure_date),
            ),
            date_key="departure_date",
            singular="départ prévu",
            plural="départs prévus",
        )


class GetOccupancyInput(BaseModel):
    stay_date: date


class GetOccupancyTool(BaseTool):
    name = "get_occupancy"
    description = "Calcule le taux d'occupation des chambres pour une nuit donnee."
    risk = RiskLevel.READ
    return_direct = True
    input_model = GetOccupancyInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(self, arguments: GetOccupancyInput, context: ToolContext) -> ToolResult:
        return _format_occupancy(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.get_occupancy(stay_date=arguments.stay_date),
            )
        )


class GetOccupancyRangeInput(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _valid_dates(self) -> "GetOccupancyRangeInput":
        if self.end_date < self.start_date:
            raise ValueError("end_date doit suivre start_date")
        if (self.end_date - self.start_date).days > 365:
            raise ValueError("la periode ne peut pas depasser 366 nuits")
        return self


class GetOccupancyRangeTool(BaseTool):
    name = "get_occupancy_range"
    description = "Calcule l'occupation quotidienne et moyenne sur une periode."
    risk = RiskLevel.READ
    return_direct = True
    input_model = GetOccupancyRangeInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: GetOccupancyRangeInput, context: ToolContext
    ) -> ToolResult:
        return _format_occupancy_range(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.get_occupancy_range(
                    start_date=arguments.start_date,
                    end_date=arguments.end_date,
                ),
            )
        )


class CheckAvailabilityInput(BaseModel):
    arrival_date: date
    departure_date: date
    room_type: str | None = None
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)

    @model_validator(mode="after")
    def _valid_dates(self) -> "CheckAvailabilityInput":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date doit suivre arrival_date")
        return self


class CheckAvailabilityTool(BaseTool):
    name = "check_availability"
    description = "Verifie les chambres disponibles pour une periode donnee."
    risk = RiskLevel.READ
    input_model = CheckAvailabilityInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: CheckAvailabilityInput, context: ToolContext
    ) -> ToolResult:
        return _format_availability(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.check_availability(
                    arrival_date=arguments.arrival_date,
                    departure_date=arguments.departure_date,
                    room_type=arguments.room_type,
                    adults=arguments.adults,
                    children=arguments.children,
                ),
            )
        )


class QuoteStayInput(BaseModel):
    arrival_date: date
    departure_date: date
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    include_breakfast: bool = False

    @model_validator(mode="after")
    def _valid_dates(self) -> "QuoteStayInput":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date doit suivre arrival_date")
        return self


class QuoteStayTool(BaseTool):
    name = "quote_stay"
    description = (
        "Etablit un devis comparatif verifie par type de chambre, par nuit et "
        "pour tout le sejour, avec petit-dejeuner si demande. Indique "
        "explicitement si la taxe de sejour n'est pas configuree."
    )
    risk = RiskLevel.READ
    return_direct = True
    input_model = QuoteStayInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(self, arguments: QuoteStayInput, context: ToolContext) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.quote_stay(**arguments.model_dump()),
        )


class RoomRequirement(BaseModel):
    reference: str = Field(min_length=1, max_length=100)
    adults: int = Field(default=1, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)


class PlanRoomAllocationInput(BaseModel):
    arrival_date: date
    departure_date: date
    requirements: list[RoomRequirement] = Field(min_length=1, max_length=50)
    selection_strategy: Literal["cheapest", "most_expensive"]
    include_breakfast: bool = False

    @model_validator(mode="after")
    def _valid_allocation(self) -> "PlanRoomAllocationInput":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date doit suivre arrival_date")
        references = [item.reference for item in self.requirements]
        if len(references) != len(set(references)):
            raise ValueError("chaque chambre demandee doit avoir une reference unique")
        return self


class PlanRoomAllocationTool(BaseTool):
    name = "plan_room_allocation"
    description = (
        "Calcule une allocation verifiee pour une ou plusieurs chambres, y "
        "compris des occupations differentes, selon le prix le moins cher ou "
        "le plus cher. Utiliser avant toute creation de plusieurs reservations. "
        "Les montants et les chambres proviennent exclusivement du PMS."
    )
    risk = RiskLevel.READ
    return_direct = True
    input_model = PlanRoomAllocationInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self,
        arguments: PlanRoomAllocationInput,
        context: ToolContext,
    ) -> ToolResult:
        lookup: dict[tuple[int, int], tuple[ToolResult, ToolResult]] = {}
        for requirement in arguments.requirements:
            occupancy = (requirement.adults, requirement.children)
            if occupancy in lookup:
                continue
            availability = _tool_result(
                self.name,
                context.call_id,
                self.adapter.check_availability(
                    arrival_date=arguments.arrival_date,
                    departure_date=arguments.departure_date,
                    adults=requirement.adults,
                    children=requirement.children,
                ),
            )
            if availability.status != ToolStatus.SUCCESS or not availability.verified:
                return availability
            quote = _tool_result(
                self.name,
                context.call_id,
                self.adapter.quote_stay(
                    arrival_date=arguments.arrival_date,
                    departure_date=arguments.departure_date,
                    adults=requirement.adults,
                    children=requirement.children,
                    include_breakfast=arguments.include_breakfast,
                ),
            )
            if quote.status != ToolStatus.SUCCESS or not quote.verified:
                return quote
            lookup[occupancy] = (availability, quote)

        selected_room_ids: set[str] = set()
        selected: dict[str, dict[str, object]] = {}
        indexed_requirements = list(enumerate(arguments.requirements))
        indexed_requirements.sort(
            key=lambda item: (-(item[1].adults + item[1].children), item[0])
        )
        currency = "EUR"
        tourist_tax: dict[str, object] = {
            "status": "unknown",
            "included_in_totals": False,
        }
        for _, requirement in indexed_requirements:
            availability, quote = lookup[(requirement.adults, requirement.children)]
            currency = str(quote.data.get("currency") or currency)
            raw_tax = quote.data.get("tourist_tax")
            if isinstance(raw_tax, dict):
                tourist_tax = dict(raw_tax)
            quoted_types = {
                str(item.get("room_type_id") or ""): item
                for item in quote.data.get("room_quotes", [])
                if isinstance(item, Mapping)
            }
            candidates: list[tuple[Decimal, str, dict[str, object], Mapping]] = []
            for raw_room in availability.data.get("rooms", []):
                if not isinstance(raw_room, Mapping) or raw_room.get("available") is not True:
                    continue
                max_adults = int(raw_room.get("max_adults") or 0)
                max_children = int(raw_room.get("max_children") or 0)
                if max_adults > 0 and max_adults < requirement.adults:
                    continue
                if max_children > 0 and max_children < requirement.children:
                    continue
                room_id = str(raw_room.get("room_id") or "")
                room_type_id = str(raw_room.get("room_type_id") or "")
                room_quote = quoted_types.get(room_type_id)
                if not room_id or room_id in selected_room_ids or room_quote is None:
                    continue
                try:
                    total = Decimal(
                        str(room_quote.get("total_stay_excluding_tourist_tax"))
                    )
                except (ArithmeticError, TypeError, ValueError):
                    continue
                room_number = str(raw_room.get("room_number") or room_id)
                candidates.append((total, room_number, dict(raw_room), room_quote))
            candidates.sort(
                key=lambda item: (item[0], item[1]),
                reverse=arguments.selection_strategy == "most_expensive",
            )
            if not candidates:
                return ToolResult(
                    call_id=context.call_id,
                    tool_name=self.name,
                    status=ToolStatus.CONFLICT,
                    message=(
                        "Le PMS ne dispose pas d'une combinaison de chambres "
                        "suffisante pour satisfaire toutes les occupations demandees."
                    ),
                    data={
                        "arrival_date": arguments.arrival_date.isoformat(),
                        "departure_date": arguments.departure_date.isoformat(),
                        "allocated_count": len(selected),
                        "requested_count": len(arguments.requirements),
                    },
                    error_code="insufficient_room_allocation",
                    verified=True,
                )
            total, room_number, room, room_quote = candidates[0]
            room_id = str(room.get("room_id") or "")
            selected_room_ids.add(room_id)
            selected[requirement.reference] = {
                "reference": requirement.reference,
                "adults": requirement.adults,
                "children": requirement.children,
                "room_id": room_id,
                "room_number": room_number,
                "room_type_id": str(room.get("room_type_id") or ""),
                "room_type": str(room.get("room_type") or ""),
                "room_stay_with_tax": str(
                    room_quote.get("room_stay_with_tax") or "0.00"
                ),
                "breakfast_stay_with_tax": str(
                    room_quote.get("breakfast_stay_with_tax") or "0.00"
                ),
                "total_stay_excluding_tourist_tax": f"{total:.2f}",
            }

        allocations = [selected[item.reference] for item in arguments.requirements]
        grand_total = sum(
            (
                Decimal(str(item["total_stay_excluding_tourist_tax"]))
                for item in allocations
            ),
            Decimal("0"),
        )
        strategy_label = (
            "les moins cheres"
            if arguments.selection_strategy == "cheapest"
            else "les plus cheres"
        )
        lines = [
            (
                f"- {item['reference']} : {item['room_type']} "
                f"{item['room_number']} — "
                f"{item['total_stay_excluding_tourist_tax']} {currency} TTC"
            )
            for item in allocations
        ]
        message = (
            f"Allocation QloApps verifiee du {arguments.arrival_date.isoformat()} "
            f"au {arguments.departure_date.isoformat()} : {len(allocations)} "
            f"chambre(s) {strategy_label}.\n"
            + "\n".join(lines)
            + f"\nTotal des {len(allocations)} chambres : "
            f"{grand_total:.2f} {currency} TTC."
        )
        if tourist_tax.get("included_in_totals") is not True:
            message += " Taxe de sejour exclue des totaux."
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message=message,
            data={
                "arrival_date": arguments.arrival_date.isoformat(),
                "departure_date": arguments.departure_date.isoformat(),
                "selection_strategy": arguments.selection_strategy,
                "include_breakfast": arguments.include_breakfast,
                "currency": currency,
                "requested_count": len(arguments.requirements),
                "allocations": allocations,
                "total_stay_excluding_tourist_tax": f"{grand_total:.2f}",
                "tourist_tax": tourist_tax,
            },
            verified=True,
        )


class CreateReservationInput(BaseModel):
    guest_first_name: str = Field(min_length=1, max_length=100)
    guest_last_name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, min_length=5, max_length=254)
    phone: str | None = Field(default=None, min_length=6, max_length=40)
    arrival_date: date
    departure_date: date
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    room_type_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_booking(self) -> "CreateReservationInput":
        if self.departure_date <= self.arrival_date:
            raise ValueError("departure_date doit suivre arrival_date")
        if self.email is not None and (
            "@" not in self.email or "." not in self.email.rsplit("@", 1)[-1]
        ):
            raise ValueError("email invalide")
        if self.phone is not None and sum(
            character.isdigit() for character in self.phone
        ) < 6:
            raise ValueError("telephone invalide")
        return self


class CreateReservationTool(BaseTool):
    name = "create_reservation"
    description = (
        "Cree uniquement l'hebergement dans le PMS puis le relit. Ce tool "
        "n'ajoute aucun extra; un petit-dejeuner demande exige ensuite add_extra."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = CreateReservationInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: CreateReservationInput) -> str:
        return self.adapter.preview_create_reservation(**arguments.model_dump())

    def execute(
        self, arguments: CreateReservationInput, context: ToolContext
    ) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.create_reservation(
                **arguments.model_dump(),
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )


class ReservationDraft(CreateReservationInput):
    reference: str = Field(min_length=1, max_length=100)


class CreateReservationsInput(BaseModel):
    reservations: list[ReservationDraft] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_targets(self) -> "CreateReservationsInput":
        references = [item.reference for item in self.reservations]
        room_ids = [item.room_id for item in self.reservations]
        if len(references) != len(set(references)):
            raise ValueError("chaque reservation doit avoir une reference unique")
        if len(room_ids) != len(set(room_ids)):
            raise ValueError("une chambre ne peut pas etre reservee deux fois")
        return self


class CreateReservationsTool(BaseTool):
    name = "create_reservations"
    description = (
        "Cree et verifie plusieurs reservations distinctes sous une seule "
        "confirmation. Utiliser pour toute demande portant sur plusieurs "
        "chambres, clients ou dossiers; chaque element doit provenir d'une "
        "allocation PMS verifiee."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = CreateReservationsInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: CreateReservationsInput) -> str:
        previews = []
        for item in arguments.reservations:
            values = item.model_dump(exclude={"reference"})
            previews.append(
                f"[{item.reference}] "
                + self.adapter.preview_create_reservation(**values)
            )
        return (
            f"Creer {len(arguments.reservations)} reservations distinctes dans "
            "QloApps sous une seule validation :\n"
            + "\n".join(previews)
        )

    def execute(
        self,
        arguments: CreateReservationsInput,
        context: ToolContext,
    ) -> ToolResult:
        created: list[dict[str, object]] = []
        operations: list[dict[str, object]] = []
        base_key = context.idempotency_key or f"{context.request_id}:{context.call_id}"
        for index, item in enumerate(arguments.reservations, start=1):
            values = item.model_dump(exclude={"reference"})
            result = self.adapter.create_reservation(
                **values,
                idempotency_key=f"{base_key}:{index}:{item.reference}:{item.room_id}",
            )
            operation = {
                "reference": item.reference,
                "room_id": item.room_id,
                "status": result.status.value,
                "message": result.message,
                "verified": result.verified,
                "external_operation_id": result.external_operation_id,
            }
            operations.append(operation)
            reservation = result.data.get("reservation")
            if (
                result.status.value == ToolStatus.SUCCESS.value
                and result.verified
                and isinstance(reservation, dict)
            ):
                created.append({"reference": item.reference, **reservation})
                continue
            return ToolResult(
                call_id=context.call_id,
                tool_name=self.name,
                status=ToolStatus.ERROR,
                message=(
                    f"Creation multiple partielle : {len(created)}/"
                    f"{len(arguments.reservations)} reservation(s) creee(s) et "
                    f"verifiee(s). Echec sur {item.reference} : {result.message}"
                ),
                data={
                    "requested_count": len(arguments.reservations),
                    "created_count": len(created),
                    "reservations": created,
                    "operations": operations,
                },
                external_operation_id=base_key,
                error_code=result.error_code or "partial_batch_creation",
                retryable=result.retryable,
                verified=False,
            )
        lines = [
            (
                f"- {item['reference']} : reservation "
                f"#{item.get('reservation_id')} — {item.get('guest_name')} — "
                f"{item.get('room_type')} {item.get('room_id')}"
            )
            for item in created
        ]
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message=(
                f"{len(created)} reservations creees et confirmees par "
                "relecture QloApps.\n" + "\n".join(lines)
            ),
            data={
                "requested_count": len(arguments.reservations),
                "created_count": len(created),
                "reservations": created,
                "operations": operations,
            },
            external_operation_id=base_key,
            verified=True,
        )


class UpdateReservationInput(BaseModel):
    reservation_id: str = Field(min_length=1)
    arrival_date: date | None = None
    departure_date: date | None = None
    adults: int | None = Field(default=None, ge=1, le=20)
    children: int | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="after")
    def _has_change(self) -> "UpdateReservationInput":
        if all(
            value is None
            for value in (
                self.arrival_date,
                self.departure_date,
                self.adults,
                self.children,
            )
        ):
            raise ValueError("au moins une modification est requise")
        if (
            self.arrival_date is not None
            and self.departure_date is not None
            and self.departure_date <= self.arrival_date
        ):
            raise ValueError("departure_date doit suivre arrival_date")
        return self


class UpdateReservationTool(BaseTool):
    name = "update_reservation"
    description = (
        "Modifie les dates ou le nombre d'occupants d'une reservation existante, "
        "controle les conflits puis relit le PMS. Utiliser notamment pour prolonger."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = UpdateReservationInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: UpdateReservationInput) -> str:
        return self.adapter.preview_update_reservation(**arguments.model_dump())

    def execute(
        self, arguments: UpdateReservationInput, context: ToolContext
    ) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.update_reservation(
                **arguments.model_dump(),
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )


class StayStatusInput(BaseModel):
    reservation_id: str = Field(min_length=1)


class CheckInReservationTool(BaseTool):
    name = "check_in_reservation"
    description = "Enregistre le check-in d'une reservation et relit le sejour."
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = StayStatusInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: StayStatusInput) -> str:
        return self.adapter.preview_check_in(
            reservation_id=arguments.reservation_id
        )

    def execute(self, arguments: StayStatusInput, context: ToolContext) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.check_in_reservation(
                reservation_id=arguments.reservation_id,
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )


class CheckOutReservationTool(BaseTool):
    name = "check_out_reservation"
    description = "Enregistre le check-out d'une reservation et relit le sejour."
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = StayStatusInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: StayStatusInput) -> str:
        return self.adapter.preview_check_out(
            reservation_id=arguments.reservation_id
        )

    def execute(self, arguments: StayStatusInput, context: ToolContext) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.check_out_reservation(
                reservation_id=arguments.reservation_id,
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )


class GetInvoiceInput(BaseModel):
    reservation_id: str = Field(min_length=1)


class GetInvoiceTool(BaseTool):
    name = "get_invoice"
    description = (
        "Consulte la facture emise dans le PMS pour une reservation et retourne "
        "ses montants et ses lignes verifies."
    )
    risk = RiskLevel.READ
    return_direct = True
    input_model = GetInvoiceInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(self, arguments: GetInvoiceInput, context: ToolContext) -> ToolResult:
        return _format_invoice(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.get_invoice(reservation_id=arguments.reservation_id),
            )
        )


class IssueInvoiceTool(BaseTool):
    name = "issue_invoice"
    description = (
        "Emet une facture numerotee pour une reservation dans le PMS puis la "
        "relit. Ne pas utiliser pour un simple devis."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = GetInvoiceInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: GetInvoiceInput) -> str:
        return self.adapter.preview_issue_invoice(
            reservation_id=arguments.reservation_id
        )

    def execute(self, arguments: GetInvoiceInput, context: ToolContext) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.issue_invoice(
                reservation_id=arguments.reservation_id,
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )


class IssueInvoicesInput(BaseModel):
    reservation_ids: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_reservations(self) -> "IssueInvoicesInput":
        values = [value.strip() for value in self.reservation_ids]
        if any(not value for value in values):
            raise ValueError("les identifiants de reservation sont requis")
        if len(values) != len(set(values)):
            raise ValueError("une reservation ne peut pas etre facturee deux fois")
        self.reservation_ids = values
        return self


class IssueInvoicesTool(BaseTool):
    name = "issue_invoices"
    description = (
        "Emet et verifie plusieurs factures correspondant a une liste exacte "
        "de reservations, sous une seule confirmation. Utiliser pour toute "
        "demande de facturation plurielle."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = IssueInvoicesInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: IssueInvoicesInput) -> str:
        return (
            f"Emettre {len(arguments.reservation_ids)} facture(s) QloApps pour "
            "les reservations suivantes : "
            + ", ".join(f"#{value}" for value in arguments.reservation_ids)
            + ". Chaque facture recevra un numero. Confirmer ?"
        )

    def execute(
        self,
        arguments: IssueInvoicesInput,
        context: ToolContext,
    ) -> ToolResult:
        invoices: list[dict[str, object]] = []
        operations: list[dict[str, object]] = []
        base_key = context.idempotency_key or f"{context.request_id}:{context.call_id}"
        for index, reservation_id in enumerate(arguments.reservation_ids, start=1):
            result = self.adapter.issue_invoice(
                reservation_id=reservation_id,
                idempotency_key=f"{base_key}:{index}:{reservation_id}",
            )
            operations.append(
                {
                    "reservation_id": reservation_id,
                    "status": result.status.value,
                    "message": result.message,
                    "verified": result.verified,
                    "external_operation_id": result.external_operation_id,
                }
            )
            invoice = result.data.get("invoice")
            if (
                result.status.value == ToolStatus.SUCCESS.value
                and result.verified
                and isinstance(invoice, dict)
            ):
                invoices.append(invoice)
                continue
            return ToolResult(
                call_id=context.call_id,
                tool_name=self.name,
                status=ToolStatus.ERROR,
                message=(
                    f"Facturation multiple partielle : {len(invoices)}/"
                    f"{len(arguments.reservation_ids)} facture(s) emise(s) et "
                    f"verifiee(s). Echec pour #{reservation_id} : {result.message}"
                ),
                data={"invoices": invoices, "operations": operations},
                external_operation_id=base_key,
                error_code=result.error_code or "partial_batch_invoicing",
                retryable=result.retryable,
                verified=False,
            )
        lines = []
        for invoice in invoices:
            currency = invoice.get("currency") or "EUR"
            lines.append(
                f"- Facture #{invoice.get('number')} — reservation "
                f"#{invoice.get('reservation_id')} — "
                f"{invoice.get('total_including_tax')} {currency} TTC"
            )
        return ToolResult(
            call_id=context.call_id,
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            message=(
                f"{len(invoices)} facture(s) emise(s) et verifiee(s) dans "
                "QloApps.\n" + "\n".join(lines)
            ),
            data={"invoices": invoices, "operations": operations},
            external_operation_id=base_key,
            verified=True,
        )


class ListCustomerRequestsInput(BaseModel):
    status: Literal["open", "pending", "closed", "all"] = "open"


class ListCustomerRequestsTool(BaseTool):
    name = "list_customer_requests"
    description = "Liste les demandes clients QloApps par statut."
    risk = RiskLevel.READ
    return_direct = True
    input_model = ListCustomerRequestsInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: ListCustomerRequestsInput, context: ToolContext
    ) -> ToolResult:
        return _format_customer_requests(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.list_customer_requests(status=arguments.status),
            )
        )


class GetCustomerRequestInput(BaseModel):
    request_id: str = Field(min_length=1)


class GetCustomerRequestTool(BaseTool):
    name = "get_customer_request"
    description = "Charge une demande client et l'historique de ses messages."
    risk = RiskLevel.READ
    input_model = GetCustomerRequestInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, arguments: GetCustomerRequestInput, context: ToolContext
    ) -> ToolResult:
        return _format_customer_request(
            _tool_result(
                self.name,
                context.call_id,
                self.adapter.get_customer_request(request_id=arguments.request_id),
            )
        )


class ReplyCustomerRequestInput(BaseModel):
    request_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4_000)


class ReplyCustomerRequestTool(BaseTool):
    name = "reply_customer_request"
    description = (
        "Envoie une reponse visible par le client dans une demande QloApps, "
        "puis relit exactement le message."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = ReplyCustomerRequestInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: ReplyCustomerRequestInput) -> str:
        return self.adapter.preview_reply_customer_request(
            request_id=arguments.request_id,
            message=arguments.message,
        )

    def execute(
        self, arguments: ReplyCustomerRequestInput, context: ToolContext
    ) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.reply_customer_request(
                request_id=arguments.request_id,
                message=arguments.message,
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )

class AddExtraInput(BaseModel):
    reservation_id: str = Field(min_length=1)
    extra_code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=100)
    unit_price: Decimal | None = Field(default=None, ge=0, decimal_places=2)


class AddExtraTool(BaseTool):
    name = "add_extra"
    description = (
        "Ajoute un extra facture a une reservation existante puis relit le "
        "dossier. Apres une creation, utiliser l'identifiant retourne par le PMS."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    input_model = AddExtraInput

    def __init__(self, adapter: PMSAdapter) -> None:
        self.adapter = adapter

    def confirmation_summary(self, arguments: AddExtraInput) -> str:
        return self.adapter.preview_add_extra(
            reservation_id=arguments.reservation_id,
            extra_code=arguments.extra_code,
            label=arguments.label,
            quantity=arguments.quantity,
            unit_price=arguments.unit_price,
        )

    def execute(self, arguments: AddExtraInput, context: ToolContext) -> ToolResult:
        return _tool_result(
            self.name,
            context.call_id,
            self.adapter.add_extra(
                reservation_id=arguments.reservation_id,
                extra_code=arguments.extra_code,
                label=arguments.label,
                quantity=arguments.quantity,
                unit_price=arguments.unit_price,
                idempotency_key=(
                    context.idempotency_key
                    or f"{context.request_id}:{context.call_id}"
                ),
            ),
        )
