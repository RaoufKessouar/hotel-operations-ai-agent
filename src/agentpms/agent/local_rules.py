"""Moteur local borne pour rendre les scenarios de demonstration executables."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from datetime import date, timedelta
import json
import re
import unicodedata

from agentpms.contracts import AgentDecision, DecisionKind, ToolResult, ToolStatus
from agentpms.tools.base import ToolSpec

from .state import AgentState


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).casefold()
    typo_aliases = {
        "demian": "demain",
        "resatvation": "reservation",
        "resavation": "reservation",
    }
    for typo, replacement in typo_aliases.items():
        normalized = re.sub(rf"\b{typo}\b", replacement, normalized)
    return normalized


def _latest_user_message(state: AgentState) -> str:
    return next(
        (message.content for message in reversed(state.messages) if message.role == "user"),
        state.user_message,
    )


def _booking_user_messages(state: AgentState) -> list[str]:
    messages = [message.content for message in state.messages if message.role == "user"]
    start = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if _wants_booking_creation(messages[index])
        ),
        0,
    )
    return messages[start:]


def _booking_user_context(state: AgentState) -> str:
    return " ".join(_booking_user_messages(state))


def _looks_like_booking_details(message: str) -> bool:
    normalized = _plain(message)
    return any(
        (
            _email(message),
            _phone(message),
            _guest_counts(message),
            _booking_guest_name(message, allow_standalone=True),
            re.search(r"\b(?:chambre|room|type)\b", normalized),
            re.search(r"\bmoins cher(?:e)?\b", normalized),
            _contact_absence_is_explicit(message),
        )
    )


def _booking_flow_active(state: AgentState) -> bool:
    start = next(
        (
            index
            for index in range(len(state.messages) - 1, -1, -1)
            if state.messages[index].role == "user"
            and _wants_booking_creation(state.messages[index].content)
        ),
        None,
    )
    if start is None:
        return False
    latest = _latest_user_message(state)
    if _wants_booking_creation(latest):
        return True
    for message in state.messages[start + 1 :]:
        if message.role != "tool":
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if (
            payload.get("tool_name") == "create_reservation"
            and payload.get("status") == ToolStatus.SUCCESS.value
        ):
            return False
    return _looks_like_booking_details(latest)


def _reservation_id(message: str) -> str | None:
    normalized = _plain(message)
    match = re.search(
        r"(?:reservation|dossier|numero|n[°o]|#)\s*(?:n[°o]\s*)?#?\s*([a-z]*-?\d+)",
        normalized,
    )
    return match.group(1).upper() if match else None


def _guest_name(message: str) -> str | None:
    value = _plain(message).strip().rstrip(".?!")
    patterns = (
        r"(?:reservation|resa)(?:s)?\s+(?:de|pour)\s+(.+)$",
        r"(?:recherche|cherche|trouve)\s+(?:la\s+|les\s+)?(?:reservation|resa)(?:s)?\s+(?:de|pour)?\s*(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            candidate = re.sub(
                r"^(?:m\.?|mme\.?|madame|monsieur|monisuer)\s+",
                "",
                match.group(1).strip(" ,"),
            )
            if not any(char.isdigit() for char in candidate) and len(candidate) >= 2:
                return candidate
    return None


def _dates(message: str) -> list[date]:
    values: list[date] = []
    for raw in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message):
        try:
            values.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return values


def _short_dates(message: str, today: date, *, roll_forward: bool) -> list[date]:
    values: list[date] = []
    for day_value, month_value, year_value in re.findall(
        r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b",
        _plain(message),
    ):
        year = int(year_value) if year_value else today.year
        try:
            candidate = date(year, int(month_value), int(day_value))
        except ValueError:
            continue
        if roll_forward and not year_value and candidate < today:
            candidate = date(year + 1, candidate.month, candidate.day)
        values.append(candidate)
    return values


def _requested_day(message: str, today: date) -> date | None:
    normalized = _plain(message)
    if re.search(r"\bdemain\b", normalized):
        return today + timedelta(days=1)
    if re.search(r"\b(?:ce soir|aujourd'hui|aujourdhui)\b", normalized):
        return today
    explicit_dates = _dates(message)
    if explicit_dates:
        return explicit_dates[0]
    short_dates = _short_dates(message, today, roll_forward=False)
    return short_dates[0] if short_dates else None


def _booking_date_range(message: str, today: date) -> tuple[date, date] | None:
    normalized = _plain(message)
    iso_dates = _dates(message)
    short_dates = _short_dates(message, today, roll_forward=True)

    relative_arrival = bool(
        re.search(r"\b(?:ce soir|aujourd'hui|aujourdhui)\b", normalized)
    )
    if relative_arrival:
        arrivals = [today]
        departures = iso_dates + short_dates
    else:
        combined = iso_dates + short_dates
        arrivals = combined[:1]
        departures = combined[1:2]
    if not arrivals or not departures or departures[0] <= arrivals[0]:
        return None
    return arrivals[0], departures[0]


def _booking_guest_name(
    message: str, *, allow_standalone: bool = False
) -> tuple[str, str] | None:
    normalized = _plain(message)
    end_of_name = (
        r"(?=\s+(?:du|au\s+\d|de\s+(?:ce|demain|\d)|a partir|"
        r"pour\s+(?:le\s+)?\d|pour\s+\d+\s+(?:personnes?|adultes?)|"
        r"avec|sans|email|tel(?:ephone)?)\b|[,.;]|$)"
    )
    patterns = (
        r"\bau nom de\s+(.+?)" + end_of_name,
        r"(?:cree|creer|creez|fais|fait|faire|faites|effectue|effectuer|effectuez)"
        r"\s+(?:une?\s+)?(?:reservation|resa)\s+(?:pour)\s+(.+?)"
        + end_of_name,
    )
    candidate = None
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            candidate = match.group(1).strip()
            break
    if candidate is None and allow_standalone:
        candidate = normalized.strip().rstrip(".?!").split(",", 1)[0].strip()
        forbidden = {
            "adresse",
            "adulte",
            "adultes",
            "chambre",
            "cher",
            "chere",
            "email",
            "enfant",
            "enfants",
            "la",
            "moins",
            "personne",
            "personnes",
            "room",
            "tel",
            "telephone",
            "type",
        }
        standalone_parts = candidate.split()
        if (
            not 2 <= len(standalone_parts) <= 4
            or any(part in forbidden for part in standalone_parts)
            or any(not re.fullmatch(r"[a-z][a-z'-]*", part) for part in standalone_parts)
        ):
            candidate = None
    if candidate is None:
        return None
    candidate = re.sub(
        r"^(?:m\.?|mme\.?|madame|monsieur|monisuer)\s+", "", candidate
    )
    parts = [part for part in candidate.split() if part]
    if len(parts) < 2:
        return None
    return parts[0].title(), " ".join(parts[1:]).title()


def _booking_guest_from_state(state: AgentState) -> tuple[str, str] | None:
    messages = _booking_user_messages(state)
    for index in range(len(messages) - 1, -1, -1):
        guest = _booking_guest_name(
            messages[index],
            allow_standalone=index > 0,
        )
        if guest is not None:
            return guest
    return None


def _email(message: str) -> str | None:
    match = re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", message)
    return match.group(0) if match else None


def _phone(message: str) -> str | None:
    match = re.search(
        r"(?:tel(?:ephone)?|portable|phone)\s*[:=]?\s*(\+?\d[\d .-]{4,}\d)",
        _plain(message),
    )
    return match.group(1).strip() if match else None


def _contact_absence_is_explicit(message: str) -> bool:
    normalized = _plain(message)
    return any(
        phrase in normalized
        for phrase in (
            "infos de contact on en dispose pas",
            "infos de contacte on en dispose pas",
            "coordonnees on en dispose pas",
            "pas d infos de contact",
            "pas d information de contact",
            "pas les coordonnees",
            "pas de coordonnees",
            "sans coordonnees",
            "sans contact",
            "ni email ni telephone",
            "aucun email ni telephone",
            "fait sans",
            "faire sans",
            "je ne les ai pas",
            "y en a pas besoin",
        )
    )


_NUMBER_WORDS = {
    "zero": 0,
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
}


def _count_value(raw: str) -> int:
    return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]


def _guest_counts(message: str) -> tuple[int, int] | None:
    normalized = _plain(message)
    number = r"(\d+|zero|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix)"
    adults_match = re.search(
        rf"\b{number}\s+(?:personnes?|adultes?)\b",
        normalized,
    )
    if not adults_match:
        return None
    children_match = re.search(rf"\b{number}\s+enfants?\b", normalized)
    children = _count_value(children_match.group(1)) if children_match else 0
    adults = _count_value(adults_match.group(1))
    return adults, children


def _wants_booking_creation(message: str) -> bool:
    normalized = _plain(message)
    if (
        _stay_status_action(message) is not None
        or _wants_booking_update(message)
        or _invoice_action(message) is not None
    ):
        return False
    action = re.search(
        r"\b(?:cree|creer|creez|fais|fait|faire|faites|effectue|effectuer|effectuez)\b",
        normalized,
    )
    booking = re.search(r"\b(?:reservation|resa)\b", normalized)
    return action is not None and booking is not None


def _wants_booking_update(message: str) -> bool:
    normalized = _plain(message)
    return any(
        fragment in normalized
        for fragment in ("prolong", "modifi", "decal", "change la date", "change les dates")
    )


def _stay_status_action(message: str) -> str | None:
    normalized = _plain(message)
    if re.search(r"\bcheck[ -]?out\b", normalized):
        return "check_out_reservation"
    if re.search(r"\bcheck[ -]?in\b", normalized):
        return "check_in_reservation"
    return None


def _invoice_action(message: str) -> str | None:
    normalized = _plain(message)
    if not re.search(r"\b(?:facture|invoice)\b", normalized):
        return None
    if re.search(
        r"\b(?:emet|emettre|emets|edite|editer|genere|generer|cree|creer|fais|faire)\b",
        normalized,
    ):
        return "issue_invoice"
    return "get_invoice"


def _customer_request_id(message: str) -> str | None:
    normalized = _plain(message)
    match = re.search(
        r"(?:demande|message|ticket)\s*(?:client)?\s*(?:n[°o]\s*)?#?\s*(\d+)",
        normalized,
    )
    return match.group(1) if match else None


def _customer_request_action(message: str) -> str | None:
    normalized = _plain(message)
    if not re.search(r"\b(?:demandes?|messages?|tickets?)\s+clients?\b", normalized):
        return None
    if re.search(r"\b(?:reponds?|repondre|envoie|envoyer)\b", normalized):
        return "reply_customer_request"
    if _customer_request_id(message):
        return "get_customer_request"
    return "list_customer_requests"


def _customer_reply_text(message: str) -> str | None:
    colon = message.split(":", 1)
    if len(colon) == 2 and colon[1].strip():
        return colon[1].strip()
    match = re.search(r"\b(?:en disant|que)\s+(.+)$", message, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _wants_breakfast(message: str) -> bool:
    normalized = _plain(message)
    return bool(
        re.search(r"\bpetits?[- ]dejeuners?\b", normalized)
        or re.search(r"\bpetits?\s+dej\b", normalized)
        or "breakfast" in normalized
    )


_FRENCH_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}


def _remaining_month_range(message: str, today: date) -> tuple[date, date] | None:
    normalized = _plain(message)
    if "mois" not in normalized:
        return None
    remaining_only = bool(re.search(r"reste\s+du\s+mois", normalized))
    month_match = next(
        (
            (name, number)
            for name, number in _FRENCH_MONTHS.items()
            if name in normalized
        ),
        None,
    )
    month = month_match[1] if month_match else today.month
    year_match = re.search(r"\b(20\d{2})\b", normalized)
    year = int(year_match.group(1)) if year_match else today.year
    if not year_match and month < today.month:
        year += 1
    first = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    start = (
        today + timedelta(days=1)
        if remaining_only and first <= today <= end
        else first
    )
    return (start, end) if start <= end else None


def _occupancy_range_summary(result: ToolResult) -> str:
    days = result.data.get("days", [])
    if not days:
        return result.message
    groups: list[dict[str, object]] = []
    for day in days:
        signature = (
            day.get("occupied_rooms"),
            day.get("total_rooms"),
            day.get("occupancy_rate_percent"),
        )
        if groups and groups[-1]["signature"] == signature:
            groups[-1]["end"] = day.get("stay_date")
            continue
        groups.append(
            {
                "start": day.get("stay_date"),
                "end": day.get("stay_date"),
                "signature": signature,
            }
        )

    lines = []
    for group in groups:
        occupied, total, rate = group["signature"]
        period = (
            f"Le {group['start']}"
            if group["start"] == group["end"]
            else f"Du {group['start']} au {group['end']}"
        )
        rate_text = str(rate).replace(".", ",")
        lines.append(
            f"- {period} : {occupied}/{total} chambres occupées ({rate_text} %)."
        )
    average = str(result.data.get("average_occupancy_rate_percent")).replace(".", ",")
    return (
        f"Du {result.data.get('start_date')} au {result.data.get('end_date')}, "
        f"le taux d’occupation moyen est de {average} %.\n"
        + "\n".join(lines)
    )


def _selected_room_type(
    message: str, categories: list[dict[str, object]]
) -> dict[str, object] | None:
    normalized = _plain(message)
    for category in sorted(
        categories,
        key=lambda item: len(str(item.get("name") or "")),
        reverse=True,
    ):
        name = _plain(str(category.get("name") or ""))
        identifier = str(category.get("room_type_id") or "")
        if (name and name in normalized) or re.search(
            rf"\btype\s*#?{re.escape(identifier)}\b",
            normalized,
        ):
            return category
    if any(
        phrase in normalized
        for phrase in ("moins chere", "moins cher", "premier prix", "economique")
    ):
        priced = [
            category
            for category in categories
            if category.get("total_price_with_tax") not in (None, "")
        ]
        if priced:
            return min(
                priced,
                key=lambda item: float(item.get("total_price_with_tax") or 0),
            )
    return None


def _availability_options(result: ToolResult) -> str:
    currency = result.data.get("currency") or "EUR"
    lines = []
    for category in result.data.get("room_types", []):
        total = category.get("total_price_with_tax")
        price = (
            f", total {float(total):.2f} {currency} TTC"
            if total not in (None, "")
            else ""
        )
        lines.append(
            f"- {category.get('name')} (type {category.get('room_type_id')}) : "
            f"{category.get('available_count')} disponible(s){price}"
        )
    return "\n".join(lines)


def _reservation_summary(result: ToolResult) -> str:
    reservation = result.data.get("reservation", {})
    if not reservation:
        return result.message
    room = reservation.get("room_type") or "chambre non renseignee"
    raw_status = str(reservation.get("status") or "inconnu")
    status = {
        "confirmed": "confirmée",
        "checked_in": "check-in effectué",
        "checked_out": "check-out effectué",
        "cancelled": "annulée",
        "refunded": "remboursée",
        "new": "nouvelle",
    }.get(raw_status, raw_status)
    extras = reservation.get("extras") or []
    extras_text = ", ".join(item.get("label", "Service") for item in extras)
    suffix = f" Services : {extras_text}." if extras_text else " Aucun service ajoute."
    return (
        f"Reservation #{reservation.get('reservation_id')} — "
        f"{reservation.get('guest_name')}, du {reservation.get('arrival_date')} "
        f"au {reservation.get('departure_date')}, {room}. "
        f"Statut : {status}." + suffix
    )


def _invoice_summary(result: ToolResult) -> str:
    invoice = result.data.get("invoice", {})
    if not invoice:
        return result.message
    number = invoice.get("number") or invoice.get("invoice_id")
    lines = invoice.get("lines") or []
    details = ""
    if lines:
        details = "\n" + "\n".join(
            f"- {line.get('description')} : {line.get('total_including_tax')} "
            f"{invoice.get('currency') or 'EUR'} TTC"
            for line in lines
        )
    return (
        f"Facture #{number} de la réservation "
        f"{invoice.get('reservation_id')} — {invoice.get('guest_name')}. "
        f"Total : {invoice.get('total_including_tax')} "
        f"{invoice.get('currency') or 'EUR'} TTC; date d’émission : "
        f"{invoice.get('issued_at')}.{details}"
    )


def _customer_request_summary(result: ToolResult) -> str:
    request = result.data.get("request", {})
    if not request:
        return result.message
    messages = request.get("messages") or []
    history = "\n".join(
        f"- {'Hôtel' if item.get('sender') == 'hotel' else 'Client'} : "
        f"{item.get('message')}"
        for item in messages
    )
    return (
        f"Demande client #{request.get('request_id')} — "
        f"{request.get('customer_name')} — {request.get('subject')} "
        f"(statut : {request.get('status')})."
        + (f"\n{history}" if history else "")
    )


class LocalRulesDecisionModel:
    """Fallback explicable; le modele compatible OpenAI reste configurable."""

    def __init__(self, today_provider: Callable[[], date] = date.today) -> None:
        self._today_provider = today_provider

    def decide(self, state: AgentState, tools: list[ToolSpec]) -> AgentDecision:
        available = {tool.name for tool in tools}
        if state.messages and state.messages[-1].role == "tool" and state.tool_results:
            return self._after_tool(state, state.tool_results[-1], available)
        return self._from_user(state, available)

    def _from_user(self, state: AgentState, available: set[str]) -> AgentDecision:
        message = _latest_user_message(state)
        normalized = _plain(message)
        today = self._today_provider()
        raw_conversation = " ".join(
            item.content for item in state.messages if item.role == "user"
        )
        booking_context = _booking_user_context(state)
        conversation = _plain(raw_conversation)
        wants_extra = any(
            word in conversation for word in ("ajoute", "ajouter")
        ) and _wants_breakfast(conversation)
        identifier = _reservation_id(message)

        previous = state.tool_results[-1] if state.tool_results else None
        if previous and previous.status == ToolStatus.AMBIGUOUS and identifier:
            return self._reservation_action(identifier, wants_extra, available)

        customer_action = _customer_request_action(message)
        if customer_action is not None:
            if customer_action not in available:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="La gestion des demandes clients n'est pas activee.",
                )
            request_identifier = _customer_request_id(message)
            if customer_action == "list_customer_requests":
                status = "all" if "toutes" in normalized else "open"
                if "ferme" in normalized or "cloture" in normalized:
                    status = "closed"
                elif "attente" in normalized:
                    status = "pending"
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name=customer_action,
                    arguments={"status": status},
                )
            if not request_identifier:
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response="Indiquez le numero de la demande client.",
                )
            if customer_action == "reply_customer_request":
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="get_customer_request",
                    arguments={"request_id": request_identifier},
                )
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name=customer_action,
                arguments={"request_id": request_identifier},
            )

        invoice_action = _invoice_action(message)
        if invoice_action is not None:
            if invoice_action not in available:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="La facturation n'est pas activee dans ce PMS.",
                )
            if not identifier:
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response="Indiquez le numero de la reservation a facturer.",
                )
            if invoice_action == "issue_invoice" and "get_reservation" in available:
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="get_reservation",
                    arguments={"reservation_id": identifier},
                )
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="get_invoice",
                arguments={"reservation_id": identifier},
            )

        wants_quote = any(
            word in normalized
            for word in ("prix", "tarif", "devis", "total", "taxe")
        )
        if wants_quote and "quote_stay" in available:
            booking_dates = _booking_date_range(booking_context, today)
            counts = _guest_counts(booking_context)
            if booking_dates is not None and counts is not None:
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="quote_stay",
                    arguments={
                        "arrival_date": booking_dates[0].isoformat(),
                        "departure_date": booking_dates[1].isoformat(),
                        "adults": counts[0],
                        "children": counts[1],
                        "include_breakfast": _wants_breakfast(conversation),
                    },
                )

        creation_context = _booking_flow_active(state)
        if creation_context:
            if "create_reservation" not in available:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="La creation de reservation n'est pas activee dans QloApps.",
                )
            guest = _booking_guest_from_state(state)
            booking_dates = _booking_date_range(booking_context, today)
            counts = _guest_counts(booking_context)
            missing = []
            if guest is None:
                missing.append("le prenom et le nom du client")
            if booking_dates is None:
                missing.append("les dates d'arrivee et de depart")
            if counts is None:
                missing.append("le nombre de personnes")
            if missing:
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response="Pour preparer la reservation, indiquez " + ", ".join(missing) + ".",
                )
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="check_availability",
                arguments={
                    "arrival_date": booking_dates[0].isoformat(),
                    "departure_date": booking_dates[1].isoformat(),
                    "adults": counts[0],
                    "children": counts[1],
                },
            )

        if wants_extra:
            if identifier:
                return self._reservation_action(identifier, True, available)
            guest = _guest_name(message)
            if guest and "search_reservations" in available:
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="search_reservations",
                    arguments={"guest_name": guest},
                )
            return AgentDecision(
                kind=DecisionKind.CLARIFY,
                response="Pour quelle reservation faut-il ajouter le petit-dejeuner ?",
            )

        stay_action = _stay_status_action(message)
        if stay_action is not None:
            if stay_action not in available:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="Cette operation de reception n'est pas activee.",
                )
            if identifier and "get_reservation" in available:
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="get_reservation",
                    arguments={"reservation_id": identifier},
                )
            return AgentDecision(
                kind=DecisionKind.CLARIFY,
                response="Indiquez le numero de la reservation concernee.",
            )

        if _wants_booking_update(message):
            if "update_reservation" not in available:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="La modification de reservation n'est pas activee.",
                )
            if identifier and "get_reservation" in available:
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="get_reservation",
                    arguments={"reservation_id": identifier},
                )
            return AgentDecision(
                kind=DecisionKind.CLARIFY,
                response="Indiquez le numero de la reservation a modifier.",
            )

        if "disponib" in normalized and "check_availability" in available:
            dates = _dates(message)
            if len(dates) < 2:
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response=(
                        "Indiquez les dates d'arrivee et de depart au format "
                        "AAAA-MM-JJ."
                    ),
                )
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="check_availability",
                arguments={
                    "arrival_date": dates[0].isoformat(),
                    "departure_date": dates[1].isoformat(),
                },
            )

        previous_tool = previous.tool_name if previous else None
        day_reference = _requested_day(message, today)
        requested_day = day_reference or today
        has_day_reference = day_reference is not None
        arrivals_context = previous_tool == "list_arrivals"
        if "list_arrivals" in available and (
            (
                "arrivee" in normalized
                and (
                    has_day_reference
                    or any(
                        word in normalized
                        for word in ("jour", "soir", "demain", "aujourd'hui", "aujourdhui")
                    )
                )
            )
            or (arrivals_context and has_day_reference)
        ):
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="list_arrivals",
                arguments={"arrival_date": requested_day.isoformat()},
            )

        departures_context = previous_tool == "list_departures"
        if "list_departures" in available and (
            (
                "depart" in normalized
                and (
                    has_day_reference
                    or any(
                        word in normalized
                        for word in ("jour", "soir", "demain", "aujourd'hui", "aujourdhui")
                    )
                )
            )
            or (departures_context and has_day_reference)
        ):
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="list_departures",
                arguments={"departure_date": requested_day.isoformat()},
            )

        occupancy_context = previous_tool in {"get_occupancy", "get_occupancy_range"}
        remaining_month = _remaining_month_range(message, today)
        if (
            "get_occupancy_range" in available
            and remaining_month is not None
            and ("occup" in normalized or occupancy_context)
        ):
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="get_occupancy_range",
                arguments={
                    "start_date": remaining_month[0].isoformat(),
                    "end_date": remaining_month[1].isoformat(),
                },
            )

        if "get_occupancy" in available and (
            "occup" in normalized or (occupancy_context and has_day_reference)
        ):
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="get_occupancy",
                arguments={"stay_date": requested_day.isoformat()},
            )

        if identifier and "get_reservation" in available:
            return self._reservation_action(identifier, False, available)

        guest = _guest_name(message)
        if guest and "search_reservations" in available:
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="search_reservations",
                arguments={"guest_name": guest},
            )

        if "search_knowledge" in available and any(
            word in normalized for word in ("comment", "procedure", "politique", "annulation")
        ):
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="search_knowledge",
                arguments={"question": message},
            )

        return AgentDecision(
            kind=DecisionKind.CLARIFY,
            response=(
                "Je peux consulter les arrivees et l'occupation sur une date ou une "
                "periode, rechercher ou creer une reservation, verifier une disponibilite, "
                "consulter une procedure "
                "ou ajouter un petit-dejeuner. Que souhaitez-vous faire ?"
            ),
        )

    @staticmethod
    def _reservation_action(
        identifier: str, wants_extra: bool, available: set[str]
    ) -> AgentDecision:
        if wants_extra and "add_extra" in available:
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="add_extra",
                arguments={
                    "reservation_id": identifier,
                    "extra_code": "BREAKFAST",
                    "label": "Petit-dejeuner",
                    "quantity": 1,
                },
            )
        return AgentDecision(
            kind=DecisionKind.TOOL,
            tool_name="get_reservation",
            arguments={"reservation_id": identifier},
        )

    def _after_tool(
        self,
        state: AgentState,
        result: ToolResult,
        available: set[str],
    ) -> AgentDecision:
        raw_conversation = " ".join(
            item.content for item in state.messages if item.role == "user"
        )
        booking_context = _booking_user_context(state)
        conversation = _plain(raw_conversation)
        wants_extra = any(
            word in conversation for word in ("ajoute", "ajouter")
        ) and _wants_breakfast(conversation)
        wants_creation = _booking_flow_active(state)
        wants_breakfast = _wants_breakfast(booking_context)
        if result.tool_name == "search_reservations":
            if result.status == ToolStatus.SUCCESS and wants_extra:
                reservation = result.data.get("reservation", {})
                identifier = str(reservation.get("reservation_id") or "")
                if identifier:
                    return self._reservation_action(identifier, True, available)
            if result.status == ToolStatus.AMBIGUOUS:
                choices = result.data.get("reservations", [])
                lines = [
                    f"#{item.get('reservation_id')} — {item.get('guest_name')} "
                    f"({item.get('arrival_date')} au {item.get('departure_date')})"
                    for item in choices
                ]
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response="Plusieurs reservations correspondent :\n" + "\n".join(lines)
                    + "\nIndiquez le numero souhaite.",
                )
            if result.status == ToolStatus.SUCCESS:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=_reservation_summary(result),
                )

        if result.tool_name == "get_reservation" and result.status == ToolStatus.SUCCESS:
            latest = _latest_user_message(state)
            invoice_action = _invoice_action(latest)
            if invoice_action == "issue_invoice" and invoice_action in available:
                reservation = result.data.get("reservation", {})
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="issue_invoice",
                    arguments={
                        "reservation_id": reservation.get("reservation_id")
                    },
                )
            stay_action = _stay_status_action(latest)
            if stay_action is not None and stay_action in available:
                reservation = result.data.get("reservation", {})
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name=stay_action,
                    arguments={
                        "reservation_id": reservation.get("reservation_id")
                    },
                )
            if _wants_booking_update(latest) and "update_reservation" in available:
                reservation = result.data.get("reservation", {})
                current_departure = date.fromisoformat(
                    str(reservation.get("departure_date"))
                )
                target = _requested_day(latest, self._today_provider())
                if target is None and re.search(
                    r"\b(?:d[' ]?une|une)\s+nuit\b", _plain(latest)
                ):
                    target = current_departure + timedelta(days=1)
                if target is None:
                    return AgentDecision(
                        kind=DecisionKind.CLARIFY,
                        response="Indiquez la nouvelle date de depart.",
                    )
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="update_reservation",
                    arguments={
                        "reservation_id": reservation.get("reservation_id"),
                        "departure_date": target.isoformat(),
                    },
                )
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=_reservation_summary(result),
            )
        if result.tool_name == "list_customer_requests" and result.status == ToolStatus.SUCCESS:
            requests = result.data.get("requests", [])
            if not requests:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response="Aucune demande client ne correspond au filtre.",
                )
            lines = [
                f"#{item.get('request_id')} — {item.get('customer_name')} — "
                f"{item.get('subject')} ({item.get('status')})"
                for item in requests
            ]
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=f"{len(requests)} demande(s) client :\n" + "\n".join(lines),
            )
        if result.tool_name == "get_customer_request" and result.status == ToolStatus.SUCCESS:
            latest = _latest_user_message(state)
            if _customer_request_action(latest) == "reply_customer_request":
                reply = _customer_reply_text(latest)
                if not reply:
                    return AgentDecision(
                        kind=DecisionKind.CLARIFY,
                        response=(
                            "Indiquez la reponse a envoyer apres deux-points, par "
                            "exemple : Réponds à la demande client 1 : votre message."
                        ),
                    )
                request = result.data.get("request", {})
                return AgentDecision(
                    kind=DecisionKind.TOOL,
                    tool_name="reply_customer_request",
                    arguments={
                        "request_id": request.get("request_id"),
                        "message": reply,
                    },
                )
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=_customer_request_summary(result),
            )
        if result.tool_name == "quote_stay" and result.status == ToolStatus.SUCCESS:
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=result.message,
            )
        if (
            result.tool_name == "check_availability"
            and result.status == ToolStatus.SUCCESS
            and wants_creation
        ):
            rooms = result.data.get("rooms", [])
            categories = result.data.get("room_types", [])
            if not rooms:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=(
                        "Aucune chambre n'est disponible sur la periode demandee. "
                        "Aucune reservation n'a ete creee."
                    ),
                )
            selected_category = _selected_room_type(booking_context, categories)
            email = _email(booking_context)
            phone = _phone(booking_context)
            missing = []
            if selected_category is None:
                missing.append("le type de chambre")
            if missing:
                options = _availability_options(result)
                return AgentDecision(
                    kind=DecisionKind.CLARIFY,
                    response=(
                        f"J'ai verifie la disponibilite : {len(rooms)} chambre(s) "
                        "sont disponibles.\n"
                        + options
                        + "\nPour continuer, indiquez "
                        + ", ".join(missing)
                        + ". Vous pouvez aussi demander 'la moins chere'."
                    ),
                )
            room_type_id = str(selected_category.get("room_type_id") or "")
            selected_room = next(
                (
                    room
                    for room in rooms
                    if str(room.get("room_type_id") or "") == room_type_id
                ),
                None,
            )
            guest = _booking_guest_from_state(state)
            booking_dates = _booking_date_range(
                booking_context,
                self._today_provider(),
            )
            counts = _guest_counts(booking_context)
            if selected_room is None or guest is None or booking_dates is None or counts is None:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=(
                        "La reservation ne peut pas etre preparee avec les donnees "
                        "disponibles. Aucune ecriture n'a ete effectuee."
                    ),
                )
            return AgentDecision(
                kind=DecisionKind.TOOL,
                tool_name="create_reservation",
                arguments={
                    "guest_first_name": guest[0],
                    "guest_last_name": guest[1],
                    "email": email,
                    "phone": phone,
                    "arrival_date": booking_dates[0].isoformat(),
                    "departure_date": booking_dates[1].isoformat(),
                    "adults": counts[0],
                    "children": counts[1],
                    "room_type_id": room_type_id,
                    "room_id": str(selected_room.get("room_id") or ""),
                },
            )
        if result.tool_name == "check_availability" and result.status == ToolStatus.SUCCESS:
            rooms = result.data.get("rooms", [])
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=(
                    f"{len(rooms)} chambre(s) disponible(s) entre "
                    f"{result.data.get('arrival_date')} et {result.data.get('departure_date')}."
                ),
            )
        if result.tool_name == "create_reservation" and result.status == ToolStatus.SUCCESS:
            reservation = result.data.get("reservation", {})
            identifier = str(reservation.get("reservation_id") or "")
            if wants_breakfast and identifier and "add_extra" in available:
                return self._reservation_action(identifier, True, available)
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=result.message + " " + _reservation_summary(result),
            )
        if result.tool_name == "list_arrivals" and result.status == ToolStatus.SUCCESS:
            arrivals = result.data.get("reservations", [])
            arrival_date = result.data.get("arrival_date")
            if not arrivals:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=f"Aucune arrivée prévue le {arrival_date}.",
                )
            arrival_label = "arrivée prévue" if len(arrivals) == 1 else "arrivées prévues"
            lines = [
                f"#{item.get('reservation_id')} — {item.get('guest_name')} — "
                f"{item.get('room_type') or 'chambre non renseignée'}"
                for item in arrivals
            ]
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=(
                    f"{len(arrivals)} {arrival_label} le {arrival_date} :\n"
                    + "\n".join(lines)
                ),
            )
        if result.tool_name == "list_departures" and result.status == ToolStatus.SUCCESS:
            departures = result.data.get("reservations", [])
            departure_date = result.data.get("departure_date")
            if not departures:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=f"Aucun départ prévu le {departure_date}.",
                )
            label = "départ prévu" if len(departures) == 1 else "départs prévus"
            lines = [
                f"#{item.get('reservation_id')} — {item.get('guest_name')} — "
                f"{item.get('room_type') or 'chambre non renseignée'}"
                for item in departures
            ]
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=(
                    f"{len(departures)} {label} le {departure_date} :\n"
                    + "\n".join(lines)
                ),
            )
        if result.tool_name == "get_occupancy" and result.status == ToolStatus.SUCCESS:
            occupied = result.data.get("occupied_rooms")
            total = result.data.get("total_rooms")
            available = result.data.get("available_rooms")
            rate = result.data.get("occupancy_rate_percent")
            stay_date = result.data.get("stay_date")
            occupied_label = "chambre occupée" if occupied == 1 else "chambres occupées"
            available_label = (
                "chambre non occupée" if available == 1 else "chambres non occupées"
            )
            rate_text = str(rate).replace(".", ",")
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=(
                    f"Pour la nuit du {stay_date}, {occupied} {occupied_label} sur {total}, "
                    f"soit un taux de {rate_text} %. "
                    f"Il reste {available} {available_label}."
                ),
            )
        if (
            result.tool_name == "get_occupancy_range"
            and result.status == ToolStatus.SUCCESS
        ):
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=_occupancy_range_summary(result),
            )
        if result.tool_name == "add_extra" and result.status == ToolStatus.SUCCESS:
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=result.message,
            )
        if result.tool_name == "update_reservation" and result.status == ToolStatus.SUCCESS:
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=result.message + " " + _reservation_summary(result),
            )
        if result.tool_name in {
            "check_in_reservation",
            "check_out_reservation",
        } and result.status == ToolStatus.SUCCESS:
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response=result.message + " " + _reservation_summary(result),
            )
        if result.tool_name in {"get_invoice", "issue_invoice"}:
            if result.status == ToolStatus.SUCCESS:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=result.message + " " + _invoice_summary(result),
                )
            return AgentDecision(kind=DecisionKind.FINAL, response=result.message)
        if result.tool_name == "reply_customer_request":
            if result.status == ToolStatus.SUCCESS:
                return AgentDecision(
                    kind=DecisionKind.FINAL,
                    response=result.message + " " + _customer_request_summary(result),
                )
            return AgentDecision(kind=DecisionKind.FINAL, response=result.message)
        if result.tool_name == "search_knowledge":
            return AgentDecision(kind=DecisionKind.FINAL, response=result.message)
        if result.tool_name == "search_reservations" and result.status == ToolStatus.NOT_FOUND:
            return AgentDecision(
                kind=DecisionKind.FINAL,
                response="Aucune réservation ne correspond à ce nom dans QloApps.",
            )
        return AgentDecision(
            kind=DecisionKind.FINAL,
            response=f"L'operation n'a pas abouti : {result.message}",
        )
