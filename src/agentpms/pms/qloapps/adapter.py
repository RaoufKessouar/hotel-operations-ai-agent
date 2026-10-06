"""Adaptateur metier QloApps, sans endpoint expose au modele de langage."""

from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from typing import Any

from agentpms.pms.base import PMSAdapter
from agentpms.pms.models import ExtraLine, PMSResult, PMSStatus, Reservation

from .client import QloAppsAPIError, QloAppsClient


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _date(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def _localized_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    candidates = [item for item in _as_list(value) if isinstance(item, Mapping)]
    preferred = next(
        (item for item in candidates if str(item.get("id")) == "1"),
        candidates[0] if candidates else {},
    )
    return str(preferred.get("value") or "")


def _status(value: Any) -> str:
    return {
        1: "new",
        2: "completed",
        3: "cancelled",
        4: "refunded",
    }.get(int(value or 0), "confirmed")


def _result_from_error(error: QloAppsAPIError) -> PMSResult:
    status = PMSStatus.ERROR
    if error.status_code == 404:
        status = PMSStatus.NOT_FOUND
    elif error.status_code in {401, 403}:
        status = PMSStatus.FORBIDDEN
    elif error.status_code == 409:
        status = PMSStatus.CONFLICT
    return PMSResult(
        status=status,
        message=str(error),
        error_code=error.error_code,
        retryable=error.retryable,
    )


class QloAppsAdapter(PMSAdapter):
    def __init__(
        self,
        client: QloAppsClient,
        *,
        write_client: QloAppsClient | None = None,
        hotel_id: int = 1,
        booking_scan_limit: int = 200,
        employee_id: int = 1,
    ) -> None:
        if hotel_id <= 0:
            raise ValueError("hotel_id doit etre strictement positif")
        if booking_scan_limit <= 0:
            raise ValueError("booking_scan_limit doit etre strictement positif")
        if employee_id <= 0:
            raise ValueError("employee_id doit etre strictement positif")
        self.client = client
        self.write_client = write_client
        self.hotel_id = hotel_id
        self.booking_scan_limit = booking_scan_limit
        self.employee_id = employee_id

    @property
    def name(self) -> str:
        return "qloapps"

    def capabilities(self) -> set[str]:
        capabilities = {
            "search_reservations",
            "get_reservation",
            "query_stays",
            "list_arrivals",
            "list_departures",
            "get_occupancy",
            "get_occupancy_range",
            "check_availability",
            "quote_stay",
            "get_invoice",
            "list_customer_requests",
            "get_customer_request",
        }
        if self.write_client is not None:
            capabilities.update(
                {
                    "create_reservation",
                    "update_reservation",
                    "check_in_reservation",
                    "check_out_reservation",
                    "issue_invoice",
                    "reply_customer_request",
                    "add_extra",
                }
            )
        return capabilities

    @staticmethod
    def _raw_rooms(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        associations = raw.get("associations")
        if not isinstance(associations, Mapping):
            return []
        rooms: list[Mapping[str, Any]] = []
        for room_type in _as_list(associations.get("room_types")):
            if isinstance(room_type, Mapping):
                rooms.extend(
                    item
                    for item in _as_list(room_type.get("rooms"))
                    if isinstance(item, Mapping)
                )
        return rooms

    def _reservation(self, raw: Mapping[str, Any]) -> Reservation:
        associations = raw.get("associations")
        if not isinstance(associations, Mapping):
            associations = {}
        customer = associations.get("customer_detail")
        if not isinstance(customer, Mapping):
            customer = {}
        guest_name = " ".join(
            part
            for part in (
                str(customer.get("firstname") or "").strip(),
                str(customer.get("lastname") or "").strip(),
            )
            if part
        ) or "Client non renseigne"

        room_types = [
            item
            for item in _as_list(associations.get("room_types"))
            if isinstance(item, Mapping)
        ]
        if not room_types:
            raise QloAppsAPIError(
                "La reservation QloApps ne contient aucun sejour.",
                error_code="qloapps_booking_without_stay",
            )
        arrivals = [_date(item["checkin_date"]) for item in room_types]
        departures = [_date(item["checkout_date"]) for item in room_types]
        first_room_type = room_types[0]
        first_rooms = [
            item
            for item in _as_list(first_room_type.get("rooms"))
            if isinstance(item, Mapping)
        ]

        extras: list[ExtraLine] = []
        for room_type in room_types:
            nights = (
                _date(room_type["checkout_date"])
                - _date(room_type["checkin_date"])
            ).days
            for room in _as_list(room_type.get("rooms")):
                if not isinstance(room, Mapping):
                    continue
                self._append_extras(
                    extras,
                    room.get("services"),
                    "SERVICE",
                    "id_service",
                    stay_nights=nights,
                )
                self._append_extras(
                    extras,
                    room.get("facilities"),
                    "FACILITY",
                    "id_facility",
                )

        status = _status(raw.get("booking_status"))
        stay_statuses: list[str] = []
        for room in self._raw_rooms(raw):
            stay_id = room.get("id_hotel_booking")
            if stay_id in (None, ""):
                continue
            try:
                stay = self.client.get_room_booking(stay_id)
            except QloAppsAPIError:
                continue
            if int(stay.get("is_cancelled") or 0):
                stay_statuses.append("cancelled")
            elif int(stay.get("is_refunded") or 0):
                stay_statuses.append("refunded")
            else:
                stay_statuses.append(
                    {
                        1: "confirmed",
                        2: "checked_in",
                        3: "checked_out",
                    }.get(int(stay.get("id_status") or 0), "confirmed")
                )
        if stay_statuses:
            if all(item == "checked_out" for item in stay_statuses):
                status = "checked_out"
            elif any(item == "checked_in" for item in stay_statuses):
                status = "checked_in"
            elif all(item == "cancelled" for item in stay_statuses):
                status = "cancelled"
            elif all(item == "refunded" for item in stay_statuses):
                status = "refunded"
            else:
                status = "confirmed"

        return Reservation(
            reservation_id=str(raw.get("id") or ""),
            guest_name=guest_name,
            arrival_date=min(arrivals),
            departure_date=max(departures),
            room_id=str(first_rooms[0].get("id_room")) if first_rooms else None,
            room_type=str(
                first_room_type.get("name")
                or first_room_type.get("id_room_type")
                or ""
            )
            or None,
            adults=(
                max(1, int(first_rooms[0].get("adults") or 1))
                if first_rooms
                else 1
            ),
            children=(
                max(0, int(first_rooms[0].get("child") or 0))
                if first_rooms
                else 0
            ),
            status=status,
            extras=extras,
        )

    @staticmethod
    def _append_extras(
        target: list[ExtraLine],
        values: Any,
        prefix: str,
        identifier_field: str,
        stay_nights: int = 1,
    ) -> None:
        for item in _as_list(values):
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("name") or prefix.title())
            identifier = item.get(identifier_field) or label.casefold().replace(" ", "-")
            unit_price = _decimal(item.get("unit_price_without_tax"))
            if (
                prefix == "SERVICE"
                and stay_nights > 0
                and (
                    int(item.get("per_night") or 0) == 1
                    or int(item.get("price_mode") or 0) == 2
                )
            ):
                unit_price /= stay_nights
            target.append(
                ExtraLine(
                    code=f"{prefix}-{identifier}",
                    label=label,
                    quantity=max(1, int(item.get("quantity") or 1)),
                    unit_price=unit_price,
                )
            )

    @staticmethod
    def _success_payload(reservations: Iterable[Reservation]) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in reservations]

    def search_reservations(
        self, *, guest_name: str | None = None, reservation_id: str | None = None
    ) -> PMSResult:
        try:
            if reservation_id:
                candidates = [self._reservation(self.client.get_booking(reservation_id))]
            else:
                identifiers = self.client.list_booking_ids()
                if len(identifiers) > self.booking_scan_limit:
                    return PMSResult(
                        status=PMSStatus.ERROR,
                        message="La recherche depasse la limite de reservations analysables.",
                        error_code="qloapps_booking_scan_limit",
                    )
                candidates = [
                    self._reservation(self.client.get_booking(identifier))
                    for identifier in identifiers
                ]
        except QloAppsAPIError as error:
            if reservation_id and error.status_code == 404:
                return PMSResult(
                    status=PMSStatus.NOT_FOUND,
                    message=f"La reservation QloApps #{reservation_id} n'existe pas.",
                    error_code="qloapps_reservation_not_found",
                    verified=True,
                )
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Reservation QloApps impossible a interpreter: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        if guest_name:
            needle = re.sub(
                r"^\s*(?:m(?:onsieur)?|mr|mme|madame|mlle|mademoiselle)\.?\s+",
                "",
                guest_name.casefold(),
            ).strip()
            candidates = [
                reservation
                for reservation in candidates
                if needle in reservation.guest_name.casefold()
            ]
        if not candidates:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Aucune reservation QloApps correspondante.",
                verified=True,
            )
        payload = self._success_payload(candidates)
        if len(candidates) > 1:
            return PMSResult(
                status=PMSStatus.AMBIGUOUS,
                message="Plusieurs reservations QloApps correspondent a la recherche.",
                data={"reservations": payload},
                verified=True,
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation QloApps trouvee.",
            data={"reservation": payload[0]},
            verified=True,
        )

    def _resolve_service(self, extra_code: str, label: str) -> dict[str, Any]:
        normalized_code = extra_code.casefold().removeprefix("service-")
        normalized_label = label.casefold().strip()
        for service in self.client.list_services():
            identifier = str(service.get("id") or "")
            name = _localized_text(service.get("name"))
            if (
                normalized_code == identifier.casefold()
                or normalized_code == name.casefold()
                or normalized_label == name.casefold()
            ):
                resolved = dict(service)
                resolved["resolved_name"] = name
                return resolved
        raise QloAppsAPIError(
            f"Service QloApps introuvable: {extra_code}",
            status_code=404,
            error_code="qloapps_service_not_found",
        )

    def _service_quote(
        self,
        reservation: Reservation,
        service: Mapping[str, Any],
        quantity: int,
    ) -> dict[str, Decimal | int | bool]:
        catalog_price = _decimal(service.get("price"))
        nights = (reservation.departure_date - reservation.arrival_date).days
        per_day = int(service.get("price_calculation_method") or 0) == 2
        multiplier = nights if per_day else 1
        subtotal = catalog_price * quantity * multiplier
        tax_rate = self.client.get_uniform_tax_rate(
            int(service.get("id_tax_rules_group") or 0)
        )
        tax = (subtotal * tax_rate / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return {
            "catalog_price": catalog_price,
            "nights": nights,
            "per_day": per_day,
            "subtotal": subtotal,
            "tax_rate": tax_rate,
            "tax": tax,
            "total": subtotal + tax,
        }

    @staticmethod
    def _matches_quote(
        service_line: Mapping[str, Any], quote: Mapping[str, Any]
    ) -> bool:
        tolerance = Decimal("0.02")
        subtotal = _decimal(service_line.get("total_price_without_tax"))
        tax = _decimal(service_line.get("total_tax"))
        return (
            abs(subtotal - Decimal(quote["subtotal"])) <= tolerance
            and abs(tax - Decimal(quote["tax"])) <= tolerance
        )

    def preview_add_extra(
        self,
        *,
        reservation_id: str,
        extra_code: str,
        label: str,
        quantity: int,
        unit_price: Decimal | None,
    ) -> str:
        try:
            raw = self.client.get_booking(reservation_id)
            reservation = self._reservation(raw)
            service = self._resolve_service(extra_code, label)
            service_name = str(service["resolved_name"])
            quote = self._service_quote(reservation, service, quantity)
            mode = (
                f" par nuit x {quote['nights']} nuit(s)"
                if quote["per_day"]
                else " par sejour"
            )
            return (
                f"Ajouter {quantity} x {service_name} a la reservation "
                f"{reservation_id}. Prix QloApps: {quote['catalog_price']:.2f} "
                f"EUR HT{mode}; sous-total {quote['subtotal']:.2f} EUR HT + "
                f"TVA {quote['tax_rate']:.2f}% ({quote['tax']:.2f} EUR), "
                f"total {quote['total']:.2f} EUR TTC. Confirmer ?"
            )
        except (QloAppsAPIError, KeyError, TypeError, ValueError):
            return (
                f"Ajouter {quantity} x {label} a la reservation {reservation_id}. "
                "Le prix sera valide par QloApps avant l'ecriture. Confirmer ?"
            )

    def get_reservation(self, reservation_id: str) -> PMSResult:
        return self.search_reservations(reservation_id=reservation_id)

    def list_arrivals(self, *, arrival_date: date) -> PMSResult:
        try:
            identifiers = self.client.list_booking_ids()
            if len(identifiers) > self.booking_scan_limit:
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message="La recherche depasse la limite de reservations analysables.",
                    error_code="qloapps_booking_scan_limit",
                )
            arrivals = sorted(
                (
                    self._reservation(self.client.get_booking(identifier))
                    for identifier in identifiers
                ),
                key=lambda reservation: (
                    reservation.guest_name.casefold(),
                    reservation.reservation_id,
                ),
            )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Reservation QloApps impossible a interpreter: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        arrivals = [
            reservation
            for reservation in arrivals
            if reservation.arrival_date == arrival_date
            and reservation.status != "cancelled"
        ]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=(
                f"{len(arrivals)} arrivee(s) QloApps trouvee(s) "
                f"pour le {arrival_date.isoformat()}."
            ),
            data={
                "arrival_date": arrival_date.isoformat(),
                "reservations": self._success_payload(arrivals),
            },
            verified=True,
        )

    def list_departures(self, *, departure_date: date) -> PMSResult:
        try:
            identifiers = self.client.list_booking_ids()
            if len(identifiers) > self.booking_scan_limit:
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message="La recherche depasse la limite de reservations analysables.",
                    error_code="qloapps_booking_scan_limit",
                )
            departures = sorted(
                (
                    self._reservation(self.client.get_booking(identifier))
                    for identifier in identifiers
                ),
                key=lambda reservation: (
                    reservation.guest_name.casefold(),
                    reservation.reservation_id,
                ),
            )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Reservation QloApps impossible a interpreter: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        departures = [
            reservation
            for reservation in departures
            if reservation.departure_date == departure_date
            and reservation.status != "cancelled"
        ]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=(
                f"{len(departures)} depart(s) QloApps trouve(s) "
                f"pour le {departure_date.isoformat()}."
            ),
            data={
                "departure_date": departure_date.isoformat(),
                "reservations": self._success_payload(departures),
            },
            verified=True,
        )

    def get_occupancy(self, *, stay_date: date) -> PMSResult:
        result = self.get_occupancy_range(
            start_date=stay_date,
            end_date=stay_date,
        )
        if result.status != PMSStatus.SUCCESS:
            return result
        day = result.data["days"][0]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"Occupation QloApps calculee pour le {stay_date.isoformat()}.",
            data=day,
            verified=True,
        )

    def get_occupancy_range(
        self, *, start_date: date, end_date: date
    ) -> PMSResult:
        if end_date < start_date:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de fin doit suivre la date de debut.",
                error_code="invalid_date_range",
            )
        try:
            rooms = self.client.list_hotel_rooms(self.hotel_id)
            room_bookings = self.client.list_room_bookings(self.hotel_id)
            room_ids = {
                str(room.get("id"))
                for room in rooms
                if room.get("id") not in (None, "")
            }
            active_stays = []
            for booking in room_bookings:
                if booking.get("id_room") in (None, "") or int(
                    booking.get("is_cancelled") or 0
                ):
                    continue
                arrival = _date(booking.get("date_from"))
                departure = _date(booking.get("date_to"))
                if arrival <= end_date and departure > start_date:
                    active_stays.append(
                        (str(booking.get("id_room")), arrival, departure)
                    )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Occupation QloApps impossible a calculer: {error}",
                error_code="qloapps_invalid_occupancy_payload",
            )

        if not room_ids:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="QloApps ne contient aucune chambre pour cet hotel.",
                error_code="qloapps_room_inventory_empty",
            )
        unknown_rooms = {stay[0] for stay in active_stays} - room_ids
        if unknown_rooms:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="Des sejours QloApps referencent des chambres hors inventaire.",
                error_code="qloapps_occupancy_inventory_mismatch",
            )

        total_rooms = len(room_ids)
        days = []
        current = start_date
        while current <= end_date:
            occupied_rooms = len(
                {
                    room_id
                    for room_id, arrival, departure in active_stays
                    if arrival <= current < departure
                }
            )
            days.append(
                {
                    "stay_date": current.isoformat(),
                    "total_rooms": total_rooms,
                    "occupied_rooms": occupied_rooms,
                    "available_rooms": total_rooms - occupied_rooms,
                    "occupancy_rate_percent": round(
                        occupied_rooms / total_rooms * 100,
                        1,
                    ),
                }
            )
            current += timedelta(days=1)
        average = round(
            sum(day["occupied_rooms"] for day in days)
            / (total_rooms * len(days))
            * 100,
            1,
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=(
                f"Occupation QloApps calculee du {start_date.isoformat()} "
                f"au {end_date.isoformat()}."
            ),
            data={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "average_occupancy_rate_percent": average,
                "days": days,
            },
            verified=True,
        )

    def check_availability(
        self,
        *,
        arrival_date: date,
        departure_date: date,
        room_type: str | None = None,
        adults: int = 2,
        children: int = 0,
    ) -> PMSResult:
        if departure_date <= arrival_date:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de depart doit suivre la date d'arrivee.",
                error_code="invalid_date_range",
            )
        room_type_id = int(room_type) if room_type and room_type.isdigit() else None
        try:
            raw = self.client.query_availability(
                hotel_id=self.hotel_id,
                arrival_date=arrival_date,
                departure_date=departure_date,
                room_type_id=room_type_id,
                adults=adults,
                children=children,
            )
        except QloAppsAPIError as error:
            return _result_from_error(error)

        options: list[dict[str, Any]] = []
        categories: list[dict[str, Any]] = []
        for category in _as_list(raw.get("room_types")):
            if not isinstance(category, Mapping):
                continue
            category_name = str(category.get("name") or category.get("id_room_type") or "")
            if room_type and not room_type.isdigit() and room_type.casefold() not in category_name.casefold():
                continue
            rooms_container = category.get("rooms")
            if not isinstance(rooms_container, Mapping):
                continue
            available_rooms = [
                room
                for room in _as_list(rooms_container.get("available"))
                if isinstance(room, Mapping)
                and (
                    int(room.get("max_adult") or 0) <= 0
                    or int(room.get("max_adult") or 0) >= adults
                )
                and (
                    int(room.get("max_children") or 0) <= 0
                    or int(room.get("max_children") or 0) >= children
                )
            ]
            categories.append(
                {
                    "room_type_id": str(category.get("id_room_type") or ""),
                    "name": category_name,
                    "available_count": len(available_rooms),
                    "base_price_with_tax": str(
                        category.get("base_price_with_tax") or "0"
                    ),
                    "total_price_with_tax": str(
                        category.get("total_price_with_tax") or "0"
                    ),
                }
            )
            for room in available_rooms:
                options.append(
                    {
                        "room_id": str(room.get("id_room") or ""),
                        "room_number": str(room.get("room_num") or ""),
                        "room_type": category_name,
                        "room_type_id": str(category.get("id_room_type") or ""),
                        "max_adults": int(room.get("max_adult") or 0),
                        "max_children": int(room.get("max_children") or 0),
                        "available": True,
                    }
                )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(options)} chambre(s) QloApps disponible(s).",
            data={
                "hotel_id": self.hotel_id,
                "arrival_date": arrival_date.isoformat(),
                "departure_date": departure_date.isoformat(),
                "adults": adults,
                "children": children,
                "currency": str(raw.get("currency") or "EUR"),
                "room_types": categories,
                "rooms": options,
            },
            verified=True,
        )

    def quote_stay(
        self,
        *,
        arrival_date: date,
        departure_date: date,
        adults: int = 2,
        children: int = 0,
        include_breakfast: bool = False,
    ) -> PMSResult:
        if departure_date <= arrival_date:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de depart doit suivre la date d'arrivee.",
                error_code="invalid_date_range",
            )
        availability = self.check_availability(
            arrival_date=arrival_date,
            departure_date=departure_date,
            adults=adults,
            children=children,
        )
        if availability.status != PMSStatus.SUCCESS or not availability.verified:
            return availability

        nights = (departure_date - arrival_date).days
        guests = adults + children
        breakfast_data: dict[str, Any] = {
            "included": False,
            "quantity_per_charge": 0,
            "per_day": False,
            "total_with_tax": "0.00",
            "per_night_with_tax": "0.00",
        }
        breakfast_total = Decimal("0")
        breakfast_per_night = Decimal("0")
        if include_breakfast:
            try:
                service = self._resolve_service("BREAKFAST", "Breakfast")
                catalog_price = _decimal(service.get("price"))
                tax_rate = self.client.get_uniform_tax_rate(
                    int(service.get("id_tax_rules_group") or 0)
                )
                per_day = int(service.get("price_calculation_method") or 0) == 2
                charges = nights if per_day else 1
                subtotal = catalog_price * guests * charges
                tax = (subtotal * tax_rate / 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                breakfast_total = subtotal + tax
                breakfast_per_night = (
                    breakfast_total / nights if nights else Decimal("0")
                )
                breakfast_data = {
                    "included": True,
                    "service_id": str(service.get("id") or ""),
                    "name": str(service.get("resolved_name") or "Breakfast"),
                    "quantity_per_charge": guests,
                    "unit_price_excluding_tax": f"{catalog_price:.2f}",
                    "tax_rate_percent": f"{tax_rate:.2f}",
                    "per_day": per_day,
                    "subtotal_excluding_tax": f"{subtotal:.2f}",
                    "tax_total": f"{tax:.2f}",
                    "total_with_tax": f"{breakfast_total:.2f}",
                    "per_night_with_tax": f"{breakfast_per_night:.2f}",
                }
            except (QloAppsAPIError, KeyError, TypeError, ValueError) as error:
                return _result_from_error(
                    error
                    if isinstance(error, QloAppsAPIError)
                    else QloAppsAPIError(
                        "Le tarif du petit-dejeuner est incomplet dans QloApps.",
                        error_code="qloapps_breakfast_quote_unavailable",
                    )
                )

        currency = str(availability.data.get("currency") or "EUR")
        options: list[dict[str, Any]] = []
        lines: list[str] = []
        for category in availability.data.get("room_types", []):
            if not isinstance(category, Mapping) or int(
                category.get("available_count") or 0
            ) <= 0:
                continue
            room_per_night = _decimal(category.get("base_price_with_tax"))
            room_stay = _decimal(category.get("total_price_with_tax"))
            total_per_night = room_per_night + breakfast_per_night
            total_stay = room_stay + breakfast_total
            option = {
                "room_type_id": str(category.get("room_type_id") or ""),
                "name": str(category.get("name") or ""),
                "available_count": int(category.get("available_count") or 0),
                "room_per_night_with_tax": f"{room_per_night:.2f}",
                "room_stay_with_tax": f"{room_stay:.2f}",
                "breakfast_per_night_with_tax": f"{breakfast_per_night:.2f}",
                "breakfast_stay_with_tax": f"{breakfast_total:.2f}",
                "total_per_night_excluding_tourist_tax": f"{total_per_night:.2f}",
                "total_stay_excluding_tourist_tax": f"{total_stay:.2f}",
            }
            options.append(option)
            lines.append(
                f"- {option['name']} : chambre {room_per_night:.2f} {currency} "
                f"TTC/nuit"
                + (
                    f" + petit-dejeuner {breakfast_per_night:.2f} {currency} "
                    f"TTC/nuit pour {guests} personne(s)"
                    if include_breakfast
                    else ""
                )
                + f" = {total_per_night:.2f} {currency}/nuit; "
                f"total {nights} nuit(s) : {total_stay:.2f} {currency} TTC."
            )

        if not options:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Aucune chambre disponible pour etablir le devis.",
                data=availability.data,
                verified=True,
            )
        breakfast_detail = ""
        if include_breakfast:
            breakfast_detail = (
                f" Petit-dejeuner QloApps : {breakfast_data['quantity_per_charge']} x "
                f"{breakfast_data['unit_price_excluding_tax']} {currency} HT, "
                f"TVA {breakfast_data['tax_rate_percent']}%."
            )
        message = (
            f"Devis QloApps verifie du {arrival_date.isoformat()} au "
            f"{departure_date.isoformat()} ({nights} nuit(s), {guests} personne(s))."
            + breakfast_detail
            + "\n"
            + "\n".join(lines)
            + "\nTaxe de sejour : non configuree dans QloApps et exclue de ces "
            "totaux. Aucun montant n'a ete invente."
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=message,
            data={
                "arrival_date": arrival_date.isoformat(),
                "departure_date": departure_date.isoformat(),
                "nights": nights,
                "adults": adults,
                "children": children,
                "currency": currency,
                "breakfast": breakfast_data,
                "tourist_tax": {
                    "status": "not_configured",
                    "included_in_totals": False,
                },
                "room_quotes": options,
            },
            verified=True,
        )

    def _available_room_choice(
        self,
        *,
        arrival_date: date,
        departure_date: date,
        adults: int,
        children: int,
        room_type_id: str,
        room_id: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        raw = self.client.query_availability(
            hotel_id=self.hotel_id,
            arrival_date=arrival_date,
            departure_date=departure_date,
            room_type_id=int(room_type_id),
            adults=adults,
            children=children,
        )
        for category in _as_list(raw.get("room_types")):
            if not isinstance(category, Mapping) or str(
                category.get("id_room_type")
            ) != str(room_type_id):
                continue
            container = category.get("rooms")
            if not isinstance(container, Mapping):
                continue
            for room in _as_list(container.get("available")):
                if not isinstance(room, Mapping) or str(room.get("id_room")) != str(
                    room_id
                ):
                    continue
                if int(room.get("max_adult") or adults) < adults or int(
                    room.get("max_children") or children
                ) < children:
                    raise QloAppsAPIError(
                        "La capacite de la chambre est insuffisante.",
                        status_code=409,
                        error_code="qloapps_room_capacity_exceeded",
                    )
                return raw, category, room
        raise QloAppsAPIError(
            "La chambre selectionnee n'est plus disponible.",
            status_code=409,
            error_code="qloapps_room_no_longer_available",
        )

    def preview_create_reservation(
        self,
        *,
        guest_first_name: str,
        guest_last_name: str,
        email: str | None,
        phone: str | None,
        arrival_date: date,
        departure_date: date,
        adults: int,
        children: int,
        room_type_id: str,
        room_id: str,
    ) -> str:
        try:
            raw, category, room = self._available_room_choice(
                arrival_date=arrival_date,
                departure_date=departure_date,
                adults=adults,
                children=children,
                room_type_id=room_type_id,
                room_id=room_id,
            )
            room_name = str(category.get("name") or room_type_id)
            room_number = str(room.get("room_num") or room_id)
            currency = str(raw.get("currency") or "EUR")
            total = _decimal(category.get("total_price_with_tax"))
            contact = (
                f"Contact: {email}, {phone}."
                if email is not None and phone is not None
                else (
                    "Coordonnees reelles non renseignees; QloApps recevra des "
                    "identifiants techniques internes, non utilisables pour "
                    "contacter le client."
                )
            )
            return (
                f"Creer dans QloApps la reservation de {guest_first_name} "
                f"{guest_last_name}, du {arrival_date.isoformat()} au "
                f"{departure_date.isoformat()}, pour {adults} adulte(s) et "
                f"{children} enfant(s), {room_name} {room_number}. "
                f"Montant hebergement annonce par QloApps: {total:.2f} "
                f"{currency} TTC. {contact} Confirmer ?"
            )
        except (QloAppsAPIError, TypeError, ValueError):
            return super().preview_create_reservation(
                guest_first_name=guest_first_name,
                guest_last_name=guest_last_name,
                email=email,
                phone=phone,
                arrival_date=arrival_date,
                departure_date=departure_date,
                adults=adults,
                children=children,
                room_type_id=room_type_id,
                room_id=room_id,
            )

    def _booking_by_source(self, marker: str) -> dict[str, Any] | None:
        identifiers = self.client.list_booking_ids()
        if len(identifiers) > self.booking_scan_limit:
            raise QloAppsAPIError(
                "La verification depasse la limite de reservations analysables.",
                error_code="qloapps_booking_scan_limit",
            )
        for identifier in identifiers:
            booking = self.client.get_booking(identifier)
            if str(booking.get("source") or "") == marker:
                return booking
        return None

    def create_reservation(
        self,
        *,
        guest_first_name: str,
        guest_last_name: str,
        email: str | None,
        phone: str | None,
        arrival_date: date,
        departure_date: date,
        adults: int,
        children: int,
        room_type_id: str,
        room_id: str,
        idempotency_key: str,
    ) -> PMSResult:
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        if departure_date <= arrival_date:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de depart doit suivre la date d'arrivee.",
                error_code="invalid_date_range",
            )
        marker = "AgentPMS-" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
        technical_suffix = marker.removeprefix("AgentPMS-").casefold()
        effective_email = email or (
            f"sans-contact+{technical_suffix}@agentpms.invalid"
        )
        effective_phone = phone or "0000000000"
        try:
            verified_raw = self._booking_by_source(marker)
            write_error: QloAppsAPIError | None = None
            advertised_total: Decimal | None = None
            if verified_raw is None:
                raw_availability, category, room = self._available_room_choice(
                    arrival_date=arrival_date,
                    departure_date=departure_date,
                    adults=adults,
                    children=children,
                    room_type_id=room_type_id,
                    room_id=room_id,
                )
                advertised_total = _decimal(category.get("total_price_with_tax"))
                unit_without_tax = _decimal(category.get("base_price"))
                nights = (departure_date - arrival_date).days
                # QloApps 1.7 divise ce champ par le nombre de jours entre
                # l'arrivee et la veille du depart avant d'appliquer le prix a
                # toutes les nuits. Pour plus de deux nuits, il faut donc
                # compenser ce calcul interne afin de conserver le tarif par
                # nuit annonce par son propre endpoint de disponibilite.
                api_price_without_tax = unit_without_tax * max(nights - 1, 1)
                room_type_details = self.client.get_room_type(room_type_id)
                tax_rules_group_id = int(
                    room_type_details.get("id_tax_rules_group") or 0
                )
                if unit_without_tax <= 0 or tax_rules_group_id <= 0:
                    raise QloAppsAPIError(
                        "QloApps n'a pas fourni un tarif fiscal complet pour la chambre.",
                        error_code="qloapps_incomplete_room_price",
                    )
                try:
                    created = self.write_client.create_booking(
                        operation_marker=marker,
                        hotel_id=self.hotel_id,
                        guest_first_name=guest_first_name,
                        guest_last_name=guest_last_name,
                        email=effective_email,
                        phone=effective_phone,
                        room_type_id=int(room_type_id),
                        room_id=int(room_id),
                        arrival_date=arrival_date,
                        departure_date=departure_date,
                        adults=adults,
                        children=children,
                        room_unit_price_without_tax=api_price_without_tax,
                        room_tax_rules_group_id=tax_rules_group_id,
                    )
                    identifier = str(created["id"])
                except QloAppsAPIError as error:
                    write_error = error
                    recovered = self._booking_by_source(marker)
                    if recovered is None:
                        return _result_from_error(error)
                    identifier = str(recovered.get("id") or "")
                verified_raw = self.client.get_booking(identifier)
            else:
                raw_availability = {
                    "currency": str(verified_raw.get("currency") or "EUR")
                }
                category = {"total_price_with_tax": "0"}
                room = {"room_num": room_id}

            reservation = self._reservation(verified_raw)
            associations = verified_raw.get("associations")
            customer = (
                associations.get("customer_detail")
                if isinstance(associations, Mapping)
                else None
            )
            rooms = self._raw_rooms(verified_raw)
            verified_room = rooms[0] if len(rooms) == 1 else None
            actual_total: Decimal | None = None
            if isinstance(verified_room, Mapping) and verified_room.get(
                "unit_price_without_tax"
            ) not in (None, ""):
                actual_total = _decimal(
                    verified_room.get("unit_price_without_tax")
                ) + _decimal(verified_room.get("total_tax"))
            valid = (
                isinstance(customer, Mapping)
                and str(customer.get("firstname") or "").casefold()
                == guest_first_name.casefold()
                and str(customer.get("lastname") or "").casefold()
                == guest_last_name.casefold()
                and str(customer.get("email") or "").casefold()
                == effective_email.casefold()
                and str(customer.get("phone") or "") == effective_phone
                and reservation.arrival_date == arrival_date
                and reservation.departure_date == departure_date
                and reservation.room_id == str(room_id)
                and isinstance(verified_room, Mapping)
                and int(verified_room.get("adults") or 0) == adults
                and int(verified_room.get("child") or 0) == children
                and (
                    advertised_total is None
                    or (
                        actual_total is not None
                        and abs(actual_total - advertised_total) <= Decimal("0.01")
                    )
                )
            )
            if not valid:
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message=(
                        "QloApps a retourne une reservation differente de la "
                        "demande confirmee. La creation n'est pas declaree reussie."
                    ),
                    error_code="qloapps_unverified_booking_creation",
                )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Creation QloApps impossible a verifier: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        message = "Reservation creee et confirmee par relecture QloApps."
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=message,
            data={
                "reservation": reservation.model_dump(mode="json"),
                "contact_placeholders": {
                    "email": email is None,
                    "phone": phone is None,
                },
                "recovered_after_api_error": write_error is not None,
                "room_number": str(room.get("room_num") or room_id),
                "room_total_with_tax": str(
                    actual_total
                    if actual_total is not None
                    else category.get("total_price_with_tax") or "0"
                ),
                "currency": str(raw_availability.get("currency") or "EUR"),
            },
            external_operation_id=marker,
            verified=True,
        )

    def preview_update_reservation(
        self,
        *,
        reservation_id: str,
        arrival_date: date | None,
        departure_date: date | None,
        adults: int | None,
        children: int | None,
    ) -> str:
        try:
            current = self._reservation(self.client.get_booking(reservation_id))
        except (QloAppsAPIError, KeyError, TypeError, ValueError):
            return super().preview_update_reservation(
                reservation_id=reservation_id,
                arrival_date=arrival_date,
                departure_date=departure_date,
                adults=adults,
                children=children,
            )
        new_arrival = arrival_date or current.arrival_date
        new_departure = departure_date or current.departure_date
        new_adults = adults if adults is not None else current.adults
        new_children = children if children is not None else current.children
        return (
            f"Modifier dans QloApps la reservation #{reservation_id} de "
            f"{current.guest_name} : sejour du {current.arrival_date.isoformat()} "
            f"au {current.departure_date.isoformat()} vers du "
            f"{new_arrival.isoformat()} au {new_departure.isoformat()}, "
            f"{new_adults} adulte(s) et {new_children} enfant(s). "
            "La disponibilite sera reverifiee et le montant recalcule par QloApps. "
            "Confirmer ?"
        )

    def update_reservation(
        self,
        *,
        reservation_id: str,
        arrival_date: date | None,
        departure_date: date | None,
        adults: int | None,
        children: int | None,
        idempotency_key: str,
    ) -> PMSResult:
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        try:
            original = self.client.get_booking(reservation_id)
            current = self._reservation(original)
            associations = original.get("associations")
            if not isinstance(associations, Mapping):
                raise QloAppsAPIError(
                    "Associations de reservation absentes.",
                    error_code="qloapps_unsafe_booking_update",
                )
            room_types = [
                item
                for item in _as_list(associations.get("room_types"))
                if isinstance(item, Mapping)
            ]
            rooms = self._raw_rooms(original)
            if len(room_types) != 1 or len(rooms) != 1:
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        "La modification automatique est limitee aux reservations "
                        "contenant une seule chambre."
                    ),
                    error_code="qloapps_complex_booking_update_unsupported",
                    verified=True,
                )
            if _as_list(associations.get("cart_rules")) or _as_list(
                rooms[0].get("facilities")
            ):
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        "La reservation contient une remise ou une demande speciale "
                        "que cette modification ne peut pas garantir de preserver."
                    ),
                    error_code="qloapps_complex_booking_update_unsupported",
                    verified=True,
                )

            new_arrival = arrival_date or current.arrival_date
            new_departure = departure_date or current.departure_date
            new_adults = adults if adults is not None else current.adults
            new_children = children if children is not None else current.children
            if new_departure <= new_arrival:
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message="La date de depart doit suivre la date d'arrivee.",
                    error_code="invalid_date_range",
                    verified=True,
                )

            room = rooms[0]
            room_id = str(room.get("id_room") or "")
            own_stay_id = str(room.get("id_hotel_booking") or "")
            for stay in self.client.list_room_bookings(self.hotel_id):
                if str(stay.get("id") or "") == own_stay_id:
                    continue
                if str(stay.get("id_room") or "") != room_id:
                    continue
                if int(stay.get("is_cancelled") or 0) or int(
                    stay.get("is_refunded") or 0
                ):
                    continue
                if _date(stay.get("date_from")) < new_departure and _date(
                    stay.get("date_to")
                ) > new_arrival:
                    return PMSResult(
                        status=PMSStatus.CONFLICT,
                        message=(
                            "La chambre attribuee n'est pas disponible sur les "
                            "nouvelles dates. Aucune modification n'a ete effectuee."
                        ),
                        error_code="qloapps_room_no_longer_available",
                        verified=True,
                    )

            room_type = room_types[0]
            already_applied = (
                current.arrival_date == new_arrival
                and current.departure_date == new_departure
                and current.adults == new_adults
                and current.children == new_children
            )
            write_error: QloAppsAPIError | None = None
            if not already_applied:
                try:
                    self.write_client.update_booking_via_backoffice(
                        reservation_id=reservation_id,
                        arrival_date=new_arrival,
                        departure_date=new_departure,
                        adults=new_adults,
                        children=new_children,
                        idempotency_key=idempotency_key,
                    )
                except QloAppsAPIError as error:
                    write_error = error

            verified_raw = self.client.get_booking(reservation_id)
            verified = self._reservation(verified_raw)
            valid = (
                verified.arrival_date == new_arrival
                and verified.departure_date == new_departure
                and verified.adults == new_adults
                and verified.children == new_children
            )
            if not valid:
                if write_error is not None:
                    return _result_from_error(write_error)
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message=(
                        "QloApps n'a pas confirme toutes les modifications demandees."
                    ),
                    error_code="qloapps_unverified_booking_update",
                )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Modification QloApps impossible a verifier: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        message = "Reservation modifiee et confirmee par relecture QloApps."
        if already_applied:
            message = "Modification deja appliquee et confirmee par relecture QloApps."
        elif write_error is not None:
            message = (
                "Modification retrouvee et confirmee par relecture, malgre une "
                "erreur retournee par l'appel QloApps."
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=message,
            data={"reservation": verified.model_dump(mode="json")},
            external_operation_id=idempotency_key,
            verified=True,
        )

    def preview_check_in(self, *, reservation_id: str) -> str:
        return self._preview_stay_status(reservation_id, "check-in")

    def preview_check_out(self, *, reservation_id: str) -> str:
        return self._preview_stay_status(reservation_id, "check-out")

    def _preview_stay_status(self, reservation_id: str, operation: str) -> str:
        try:
            reservation = self._reservation(self.client.get_booking(reservation_id))
            return (
                f"Enregistrer dans QloApps le {operation} de "
                f"{reservation.guest_name}, reservation #{reservation_id}, "
                f"{reservation.room_type or 'chambre'} {reservation.room_id or ''}, "
                f"sejour du {reservation.arrival_date.isoformat()} au "
                f"{reservation.departure_date.isoformat()}. Confirmer ?"
            )
        except (QloAppsAPIError, KeyError, TypeError, ValueError):
            method = (
                super().preview_check_in
                if operation == "check-in"
                else super().preview_check_out
            )
            return method(reservation_id=reservation_id)

    def check_in_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        return self._change_stay_status(
            reservation_id=reservation_id,
            target_status=2,
            idempotency_key=idempotency_key,
        )

    def check_out_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        return self._change_stay_status(
            reservation_id=reservation_id,
            target_status=3,
            idempotency_key=idempotency_key,
        )

    def _change_stay_status(
        self,
        *,
        reservation_id: str,
        target_status: int,
        idempotency_key: str,
    ) -> PMSResult:
        operation = "check-in" if target_status == 2 else "check-out"
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        try:
            raw = self.client.get_booking(reservation_id)
            reservation = self._reservation(raw)
            rooms = self._raw_rooms(raw)
            if len(rooms) != 1 or rooms[0].get("id_hotel_booking") in (None, ""):
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        f"Le {operation} automatique exige une reservation avec "
                        "une seule chambre."
                    ),
                    error_code="qloapps_stay_room_ambiguous",
                    verified=True,
                )
            stay_id = str(rooms[0]["id_hotel_booking"])
            current = self.client.get_room_booking(stay_id)
            current_status = int(current.get("id_status") or 0)
            if current_status == target_status:
                verified = current
            else:
                if target_status == 2 and current_status != 1:
                    return PMSResult(
                        status=PMSStatus.CONFLICT,
                        message="Ce sejour ne peut plus passer en check-in.",
                        error_code="qloapps_invalid_check_in_transition",
                        verified=True,
                    )
                if target_status == 3 and current_status != 2:
                    return PMSResult(
                        status=PMSStatus.CONFLICT,
                        message="Le check-in doit etre enregistre avant le check-out.",
                        error_code="qloapps_invalid_check_out_transition",
                        verified=True,
                    )
                write_error: QloAppsAPIError | None = None
                try:
                    self.write_client.update_room_booking_status(
                        current,
                        status=target_status,
                        occurred_at=datetime.now(),
                    )
                except QloAppsAPIError as error:
                    write_error = error
                verified = self.client.get_room_booking(stay_id)
                if int(verified.get("id_status") or 0) != target_status:
                    if write_error is not None:
                        return _result_from_error(write_error)
                    return PMSResult(
                        status=PMSStatus.ERROR,
                        message=f"QloApps n'a pas confirme le {operation}.",
                        error_code=f"qloapps_unverified_{operation.replace('-', '_')}",
                    )
            timestamp_field = "check_in" if target_status == 2 else "check_out"
            timestamp = str(verified.get(timestamp_field) or "")
            if not timestamp or timestamp.startswith("0000-00-00"):
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message=f"QloApps n'a pas horodate le {operation}.",
                    error_code=f"qloapps_unverified_{operation.replace('-', '_')}_time",
                )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"{operation.title()} QloApps impossible a verifier: {error}",
                error_code="qloapps_invalid_room_booking_payload",
            )

        reservation.status = "checked_in" if target_status == 2 else "checked_out"
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{operation.title()} enregistre et confirme par relecture QloApps.",
            data={
                "reservation": reservation.model_dump(mode="json"),
                "occurred_at": timestamp,
                "room_booking_id": stay_id,
            },
            external_operation_id=idempotency_key,
            verified=True,
        )

    def _order_id_for_booking(self, raw: Mapping[str, Any]) -> str:
        stay_ids = {
            str(room.get("id_hotel_booking"))
            for room in self._raw_rooms(raw)
            if room.get("id_hotel_booking") not in (None, "")
        }
        if not stay_ids:
            raise QloAppsAPIError(
                "La reservation ne contient aucun sejour facturable.",
                error_code="qloapps_booking_without_billable_stay",
            )
        order_ids = {
            str(self.client.get_room_booking(stay_id).get("id_order") or "")
            for stay_id in stay_ids
        }
        order_ids.discard("")
        if len(order_ids) != 1:
            raise QloAppsAPIError(
                "La reservation correspond a plusieurs commandes QloApps.",
                error_code="qloapps_ambiguous_invoice_order",
            )
        return order_ids.pop()

    @staticmethod
    def _invoice_payload(
        *,
        invoice: Mapping[str, Any],
        order: Mapping[str, Any],
        reservation: Reservation,
        details: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        invoice_id = str(invoice.get("id") or invoice.get("id_order_invoice") or "")
        matching_details = [
            detail
            for detail in details
            if str(detail.get("id_order_invoice") or invoice_id) == invoice_id
        ]
        lines = []
        for detail in matching_details:
            quantity = int(detail.get("product_quantity") or 1)
            unit_excluding_tax = _decimal(detail.get("unit_price_tax_excl"))
            unit_including_tax = _decimal(detail.get("unit_price_tax_incl"))
            # QloApps 1.7 conserve parfois sur la ligne de service le total
            # d'une seule unite tout en portant la quantite reelle. Le total
            # fiable est donc recalcule a partir des deux champs atomiques.
            lines.append(
                {
                    "description": str(detail.get("product_name") or "Sejour"),
                    "quantity": quantity,
                    "unit_price_excluding_tax": str(unit_excluding_tax),
                    "unit_price_including_tax": str(unit_including_tax),
                    "total_excluding_tax": str(unit_excluding_tax * quantity),
                    "total_including_tax": str(unit_including_tax * quantity),
                }
            )
        invoice_total_excluding_tax = _decimal(invoice.get("total_paid_tax_excl"))
        invoice_total_including_tax = _decimal(invoice.get("total_paid_tax_incl"))
        if (
            abs(
                sum(_decimal(line["total_excluding_tax"]) for line in lines)
                - invoice_total_excluding_tax
            )
            > Decimal("0.01")
            or abs(
                sum(_decimal(line["total_including_tax"]) for line in lines)
                - invoice_total_including_tax
            )
            > Decimal("0.01")
        ):
            raise QloAppsAPIError(
                "Les lignes QloApps ne correspondent pas au total de la facture.",
                error_code="qloapps_invoice_total_mismatch",
            )
        return {
            "invoice_id": invoice_id,
            "number": int(invoice.get("number") or 0),
            "reservation_id": reservation.reservation_id,
            "order_id": str(invoice.get("id_order") or order.get("id") or ""),
            "guest_name": reservation.guest_name,
            "currency": str(order.get("currency") or "EUR"),
            "total_excluding_tax": str(invoice_total_excluding_tax),
            "total_including_tax": str(invoice_total_including_tax),
            "issued_at": str(invoice.get("date_add") or ""),
            "lines": lines,
        }

    def get_invoice(self, *, reservation_id: str) -> PMSResult:
        try:
            raw = self.client.get_booking(reservation_id)
            reservation = self._reservation(raw)
            order_id = self._order_id_for_booking(raw)
            order = self.client.get_order(order_id)
            invoices = self.client.list_order_invoices(order_id)
            if not invoices:
                return PMSResult(
                    status=PMSStatus.NOT_FOUND,
                    message=(
                        "Aucune facture QloApps n'a encore ete emise pour cette "
                        "reservation."
                    ),
                    error_code="invoice_not_issued",
                    verified=True,
                )
            details = self.client.list_order_details(order_id)
            latest = max(
                invoices,
                key=lambda item: int(item.get("id") or item.get("id_order_invoice") or 0),
            )
            payload = self._invoice_payload(
                invoice=latest,
                order=order,
                reservation=reservation,
                details=details,
            )
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Facture QloApps impossible a interpreter: {error}",
                error_code="qloapps_invalid_invoice_payload",
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Facture QloApps chargee et verifiee.",
            data={"invoice": payload},
            verified=True,
        )

    def preview_issue_invoice(self, *, reservation_id: str) -> str:
        try:
            reservation = self._reservation(self.client.get_booking(reservation_id))
            return (
                f"Emettre dans QloApps la facture de {reservation.guest_name}, "
                f"reservation #{reservation_id}, sejour du "
                f"{reservation.arrival_date.isoformat()} au "
                f"{reservation.departure_date.isoformat()}. Cette operation "
                "attribue un numero de facture. Confirmer ?"
            )
        except (QloAppsAPIError, KeyError, TypeError, ValueError):
            return super().preview_issue_invoice(reservation_id=reservation_id)

    def issue_invoice(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        try:
            raw = self.client.get_booking(reservation_id)
            order_id = self._order_id_for_booking(raw)
            existing = self.client.list_order_invoices(order_id)
            if not existing:
                self.write_client.issue_invoice(
                    order_id=order_id,
                    idempotency_key=idempotency_key,
                )
            verified = self.get_invoice(reservation_id=reservation_id)
        except QloAppsAPIError as error:
            return _result_from_error(error)
        if verified.status != PMSStatus.SUCCESS or not verified.verified:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="QloApps n'a pas confirme l'emission de la facture.",
                error_code="qloapps_unverified_invoice_issue",
            )
        return verified.model_copy(
            update={
                "message": (
                    "Facture deja emise et confirmee par QloApps."
                    if existing
                    else "Facture emise puis relue et confirmee dans QloApps."
                ),
                "external_operation_id": idempotency_key,
            }
        )

    @staticmethod
    def _thread_status(value: Any) -> str:
        return {
            "1": "open",
            "2": "pending",
            "3": "pending",
            "4": "closed",
            "open": "open",
            "pending": "pending",
            "closed": "closed",
        }.get(str(value or "").casefold(), "open")

    @staticmethod
    def _thread_message_ids(thread: Mapping[str, Any]) -> list[str]:
        associations = thread.get("associations")
        if not isinstance(associations, Mapping):
            return []
        values: Any = associations.get("customer_messages")
        if isinstance(values, Mapping) and "customer_message" in values:
            values = values["customer_message"]
        identifiers = []
        for item in _as_list(values):
            if isinstance(item, Mapping):
                identifier = item.get("id") or item.get("id_customer_message")
            else:
                identifier = item
            if identifier not in (None, ""):
                identifiers.append(str(identifier))
        return identifiers

    def _customer_request_payload(self, thread: Mapping[str, Any]) -> dict[str, Any]:
        messages = []
        thread_id = str(
            thread.get("id") or thread.get("id_customer_thread") or ""
        )
        raw_messages = self.client.list_customer_messages(thread_id)
        for message in raw_messages:
            messages.append(
                {
                    "message_id": str(
                        message.get("id")
                        or message.get("id_customer_message")
                        or ""
                    ),
                    "sender": (
                        "hotel" if int(message.get("id_employee") or 0) > 0 else "customer"
                    ),
                    "message": str(message.get("message") or ""),
                    "created_at": str(message.get("date_add") or ""),
                }
            )
        messages.sort(key=lambda item: item["created_at"])
        return {
            "request_id": str(
                thread_id
            ),
            "subject": str(thread.get("subject") or "Demande client"),
            "customer_name": str(thread.get("user_name") or "Client"),
            "email": str(thread.get("email") or ""),
            "phone": str(thread.get("phone") or ""),
            "reservation_id": (
                str(thread.get("id_order"))
                if thread.get("id_order") not in (None, "", 0, "0")
                else None
            ),
            "status": self._thread_status(thread.get("status")),
            "created_at": str(thread.get("date_add") or ""),
            "updated_at": str(thread.get("date_upd") or ""),
            "messages": messages,
        }

    def list_customer_requests(self, *, status: str = "open") -> PMSResult:
        try:
            threads = self.client.list_customer_threads()
            if len(threads) > self.booking_scan_limit:
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message="La liste des demandes clients depasse la limite autorisee.",
                    error_code="qloapps_customer_thread_scan_limit",
                )
            requests = [
                self._customer_request_payload(
                    self.client.get_customer_thread(
                        str(thread.get("id") or thread.get("id_customer_thread") or "")
                    )
                )
                for thread in threads
                if thread.get("id") not in (None, "")
                or thread.get("id_customer_thread") not in (None, "")
            ]
        except QloAppsAPIError as error:
            return _result_from_error(error)
        if status != "all":
            requests = [item for item in requests if item["status"] == status]
        requests.sort(key=lambda item: item["updated_at"], reverse=True)
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(requests)} demande(s) client QloApps trouvee(s).",
            data={"status_filter": status, "requests": requests},
            verified=True,
        )

    def get_customer_request(self, *, request_id: str) -> PMSResult:
        try:
            thread = self.client.get_customer_thread(request_id)
            payload = self._customer_request_payload(thread)
        except QloAppsAPIError as error:
            return _result_from_error(error)
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Demande client QloApps chargee et verifiee.",
            data={"request": payload},
            verified=True,
        )

    def preview_reply_customer_request(
        self, *, request_id: str, message: str
    ) -> str:
        try:
            payload = self._customer_request_payload(
                self.client.get_customer_thread(request_id)
            )
            return (
                f"Envoyer a {payload['customer_name']} la reponse suivante pour "
                f"la demande #{request_id} « {payload['subject']} » :\n« {message} »\n"
                "Confirmer l'envoi ?"
            )
        except QloAppsAPIError:
            return super().preview_reply_customer_request(
                request_id=request_id, message=message
            )

    def reply_customer_request(
        self,
        *,
        request_id: str,
        message: str,
        idempotency_key: str,
    ) -> PMSResult:
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        try:
            before = self.get_customer_request(request_id=request_id)
            if before.status != PMSStatus.SUCCESS:
                return before
            existing = next(
                (
                    item
                    for item in before.data["request"].get("messages", [])
                    if item.get("sender") == "hotel"
                    and str(item.get("message") or "").strip() == message.strip()
                ),
                None,
            )
            if existing is None:
                created = self.write_client.create_customer_message(
                    thread_id=request_id,
                    employee_id=self.employee_id,
                    message=message.strip(),
                )
                created_id = str(created.get("id") or "")
                verified_message = self.client.get_customer_message(created_id)
                if (
                    str(verified_message.get("id_customer_thread") or "") != str(request_id)
                    or int(verified_message.get("id_employee") or 0) <= 0
                    or str(verified_message.get("message") or "").strip()
                    != message.strip()
                ):
                    return PMSResult(
                        status=PMSStatus.ERROR,
                        message="QloApps n'a pas confirme exactement la reponse envoyee.",
                        error_code="qloapps_unverified_customer_reply",
                    )
            verified = self.get_customer_request(request_id=request_id)
        except QloAppsAPIError as error:
            return _result_from_error(error)
        if verified.status != PMSStatus.SUCCESS or not verified.verified:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La demande client n'a pas pu etre relue apres envoi.",
                error_code="qloapps_unverified_customer_thread",
            )
        return verified.model_copy(
            update={
                "message": (
                    "Cette reponse existait deja; aucune duplication n'a ete faite."
                    if existing is not None
                    else "Reponse envoyee puis relue et confirmee dans QloApps."
                ),
                "external_operation_id": idempotency_key,
            }
        )

    def _rollback_service_write(
        self,
        original_booking: Mapping[str, Any],
        reservation_id: str,
        service_id: str,
    ) -> bool:
        if self.write_client is None:
            return False
        try:
            self.write_client.update_booking_services(original_booking)
        except QloAppsAPIError:
            pass
        try:
            restored = self.client.get_booking(reservation_id)
        except QloAppsAPIError:
            return False
        return not any(
            str(service.get("id_service")) == service_id
            for room in self._raw_rooms(restored)
            for service in _as_list(room.get("services"))
            if isinstance(service, Mapping)
        )

    def add_extra(
        self,
        *,
        reservation_id: str,
        extra_code: str,
        label: str,
        quantity: int,
        unit_price: Decimal | None,
        idempotency_key: str,
    ) -> PMSResult:
        if self.write_client is None:
            return PMSResult(
                status=PMSStatus.FORBIDDEN,
                message="La cle d'ecriture QloApps n'est pas configuree.",
                error_code="qloapps_write_capability_not_enabled",
            )
        try:
            raw = self.client.get_booking(reservation_id)
            associations = raw.get("associations")
            if not isinstance(associations, Mapping):
                raise QloAppsAPIError(
                    "Associations de reservation absentes.",
                    error_code="qloapps_unsafe_booking_update",
                )
            if _as_list(associations.get("cart_rules")):
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        "La reservation contient une remise que cette V1 ne peut "
                        "pas garantir de preserver."
                    ),
                    error_code="qloapps_booking_discounts_unsupported",
                )
            rooms = self._raw_rooms(raw)
            if len(rooms) != 1:
                return PMSResult(
                    status=PMSStatus.AMBIGUOUS,
                    message=(
                        "La reservation contient plusieurs chambres; precisez "
                        "la chambre avant d'ajouter le service."
                    ),
                    error_code="qloapps_service_room_ambiguous",
                )
            room = rooms[0]
            if _as_list(room.get("facilities")):
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        "La reservation contient des prestations que cette V1 "
                        "ne peut pas garantir de preserver."
                    ),
                    error_code="qloapps_booking_facilities_unsupported",
                )
            service = self._resolve_service(extra_code, label)
            service_id = str(service.get("id"))
            service_name = str(service["resolved_name"])
            reservation = self._reservation(raw)
            quote = self._service_quote(reservation, service, quantity)
            catalog_price = Decimal(quote["catalog_price"])
            if unit_price is not None and unit_price != catalog_price:
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        f"Prix refuse: {service_name} coute {catalog_price:.2f} EUR "
                        "dans QloApps."
                    ),
                    error_code="qloapps_service_price_mismatch",
                )

            existing_services = [
                item
                for item in _as_list(room.get("services"))
                if isinstance(item, Mapping)
            ]
            existing = next(
                (
                    item
                    for item in existing_services
                    if str(item.get("id_service")) == service_id
                ),
                None,
            )
            if existing is not None:
                if int(existing.get("quantity") or 1) != quantity:
                    return PMSResult(
                        status=PMSStatus.CONFLICT,
                        message=(
                            f"{service_name} existe deja avec une quantite "
                            "differente; aucune modification effectuee."
                        ),
                        error_code="qloapps_service_quantity_conflict",
                    )
                if not self._matches_quote(existing, quote):
                    return PMSResult(
                        status=PMSStatus.CONFLICT,
                        message=(
                            f"{service_name} existe deja, mais sa facturation "
                            "ne correspond pas au catalogue QloApps."
                        ),
                        error_code="qloapps_existing_service_price_mismatch",
                    )
                return PMSResult(
                    status=PMSStatus.SUCCESS,
                    message=(
                        f"{service_name} etait deja present; aucune duplication."
                    ),
                    data={"reservation": reservation.model_dump(mode="json")},
                    external_operation_id=idempotency_key,
                    verified=True,
                )
            if existing_services:
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message=(
                        "La reservation contient deja un autre service. Cette V1 "
                        "refuse de reecrire une facturation de services complexe."
                    ),
                    error_code="qloapps_existing_services_unsupported",
                )

            updated_raw = deepcopy(raw)
            updated_room = self._raw_rooms(updated_raw)[0]
            updated_services = [
                dict(item)
                for item in _as_list(updated_room.get("services"))
                if isinstance(item, Mapping)
            ]
            updated_services.append(
                {
                    "id_service": int(service_id),
                    "quantity": quantity,
                    "_agentpms_new": True,
                }
            )
            updated_room["services"] = updated_services
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Mise a jour QloApps impossible a preparer: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        write_error: QloAppsAPIError | None = None
        try:
            self.write_client.update_booking_services(updated_raw)
        except QloAppsAPIError as error:
            write_error = error

        try:
            verified_raw = self.client.get_booking(reservation_id)
            verified_service = next(
                (
                    item
                    for verified_room in self._raw_rooms(verified_raw)
                    for item in _as_list(verified_room.get("services"))
                    if isinstance(item, Mapping)
                    and str(item.get("id_service")) == service_id
                    and int(item.get("quantity") or 1) == quantity
                ),
                None,
            )
            if verified_service is None:
                if write_error is not None:
                    return _result_from_error(write_error)
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message="QloApps n'a pas confirme l'ajout du service.",
                    error_code="qloapps_unverified_service_write",
                )
            if not self._matches_quote(verified_service, quote):
                restored = self._rollback_service_write(
                    raw, reservation_id, service_id
                )
                return PMSResult(
                    status=PMSStatus.ERROR,
                    message=(
                        "QloApps a retourne une facturation differente du devis; "
                        + (
                            "le service a ete retire et l'etat initial restaure."
                            if restored
                            else "la restauration automatique n'a pas pu etre confirmee."
                        )
                    ),
                    error_code="qloapps_service_price_verification_failed",
                )
            reservation = self._reservation(verified_raw)
        except QloAppsAPIError as error:
            return _result_from_error(error)
        except (KeyError, TypeError, ValueError) as error:
            return PMSResult(
                status=PMSStatus.ERROR,
                message=f"Relecture QloApps impossible: {error}",
                error_code="qloapps_invalid_booking_payload",
            )

        message = f"{service_name} ajoute et confirme par relecture QloApps."
        if write_error is not None:
            message = (
                f"{service_name} est present et confirme par relecture, malgre "
                "une erreur retournee par l'appel PUT."
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=message,
            data={"reservation": reservation.model_dump(mode="json")},
            external_operation_id=idempotency_key,
            verified=True,
        )
