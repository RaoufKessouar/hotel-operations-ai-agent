"""PMS en memoire pour les tests et les demonstrations sans donnees reelles."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from .base import PMSAdapter
from .models import AvailabilityOption, ExtraLine, PMSResult, PMSStatus, Reservation


class InMemoryPMSAdapter(PMSAdapter):
    def __init__(
        self,
        reservations: list[Reservation] | None = None,
        rooms: list[AvailabilityOption] | None = None,
        customer_requests: list[dict[str, object]] | None = None,
    ) -> None:
        self._reservations = {
            item.reservation_id: item.model_copy(deep=True)
            for item in (reservations or [])
        }
        self._rooms = [item.model_copy(deep=True) for item in (rooms or [])]
        self._idempotent_results: dict[str, PMSResult] = {}
        self._invoices: dict[str, dict[str, object]] = {}
        self._customer_requests = {
            str(item["request_id"]): dict(item)
            for item in (customer_requests or [])
        }

    @property
    def name(self) -> str:
        return "in_memory"

    def capabilities(self) -> set[str]:
        return {
            "search_reservations",
            "get_reservation",
            "query_stays",
            "list_arrivals",
            "list_departures",
            "get_occupancy",
            "get_occupancy_range",
            "check_availability",
            "create_reservation",
            "update_reservation",
            "check_in_reservation",
            "check_out_reservation",
            "get_invoice",
            "issue_invoice",
            "list_customer_requests",
            "get_customer_request",
            "reply_customer_request",
            "add_extra",
        }

    def search_reservations(
        self, *, guest_name: str | None = None, reservation_id: str | None = None
    ) -> PMSResult:
        candidates = list(self._reservations.values())
        if reservation_id:
            candidates = [
                item for item in candidates if item.reservation_id == reservation_id
            ]
        if guest_name:
            needle = guest_name.casefold().strip()
            candidates = [
                item for item in candidates if needle in item.guest_name.casefold()
            ]
        if not candidates:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Aucune reservation correspondante.",
            )
        payload = [item.model_dump(mode="json") for item in candidates]
        if len(candidates) > 1:
            return PMSResult(
                status=PMSStatus.AMBIGUOUS,
                message="Plusieurs reservations correspondent a la recherche.",
                data={"reservations": payload},
                verified=True,
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation trouvee.",
            data={"reservation": payload[0]},
            verified=True,
        )

    def get_reservation(self, reservation_id: str) -> PMSResult:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable.",
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation chargee.",
            data={"reservation": reservation.model_dump(mode="json")},
            verified=True,
        )

    def list_arrivals(self, *, arrival_date: date) -> PMSResult:
        arrivals = sorted(
            (
                reservation
                for reservation in self._reservations.values()
                if reservation.arrival_date == arrival_date
                and reservation.status != "cancelled"
            ),
            key=lambda reservation: (reservation.guest_name.casefold(), reservation.reservation_id),
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(arrivals)} arrivee(s) trouvee(s) pour le {arrival_date.isoformat()}.",
            data={
                "arrival_date": arrival_date.isoformat(),
                "reservations": [
                    reservation.model_dump(mode="json") for reservation in arrivals
                ],
            },
            verified=True,
        )

    def list_departures(self, *, departure_date: date) -> PMSResult:
        departures = sorted(
            (
                reservation
                for reservation in self._reservations.values()
                if reservation.departure_date == departure_date
                and reservation.status != "cancelled"
            ),
            key=lambda reservation: (
                reservation.guest_name.casefold(),
                reservation.reservation_id,
            ),
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=(
                f"{len(departures)} depart(s) trouve(s) pour le "
                f"{departure_date.isoformat()}."
            ),
            data={
                "departure_date": departure_date.isoformat(),
                "reservations": [
                    reservation.model_dump(mode="json")
                    for reservation in departures
                ],
            },
            verified=True,
        )

    def get_occupancy(self, *, stay_date: date) -> PMSResult:
        room_ids = {room.room_id for room in self._rooms}
        occupied_room_ids = {
            reservation.room_id
            for reservation in self._reservations.values()
            if reservation.room_id in room_ids
            and reservation.arrival_date <= stay_date < reservation.departure_date
            and reservation.status != "cancelled"
        }
        total_rooms = len(room_ids)
        if total_rooms == 0:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="Le PMS ne contient aucune chambre pour calculer l'occupation.",
                error_code="pms_room_inventory_empty",
            )
        occupied_rooms = len(occupied_room_ids)
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"Occupation calculee pour le {stay_date.isoformat()}.",
            data={
                "stay_date": stay_date.isoformat(),
                "total_rooms": total_rooms,
                "occupied_rooms": occupied_rooms,
                "available_rooms": total_rooms - occupied_rooms,
                "occupancy_rate_percent": round(occupied_rooms / total_rooms * 100, 1),
            },
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
        days = []
        current = start_date
        while current <= end_date:
            result = self.get_occupancy(stay_date=current)
            if result.status != PMSStatus.SUCCESS:
                return result
            days.append(result.data)
            current += timedelta(days=1)
        average = round(
            sum(day["occupied_rooms"] for day in days)
            / sum(day["total_rooms"] for day in days)
            * 100,
            1,
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=(
                f"Occupation calculee du {start_date.isoformat()} "
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
        rooms = [
            room
            for room in self._rooms
            if room.available and (room_type is None or room.room_type == room_type)
        ]
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(rooms)} chambre(s) disponible(s).",
            data={
                "arrival_date": arrival_date.isoformat(),
                "departure_date": departure_date.isoformat(),
                "adults": adults,
                "children": children,
                "currency": "EUR",
                "rooms": [
                    {
                        **room.model_dump(mode="json"),
                        "room_number": room.room_id,
                        "room_type_id": room.room_type,
                    }
                    for room in rooms
                ],
                "room_types": [
                    {
                        "room_type_id": category,
                        "name": category,
                        "available_count": sum(
                            room.room_type == category for room in rooms
                        ),
                        "total_price_with_tax": None,
                    }
                    for category in sorted({room.room_type for room in rooms})
                ],
            },
            verified=True,
        )

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
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        if departure_date <= arrival_date:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de depart doit suivre la date d'arrivee.",
                error_code="invalid_date_range",
            )
        room = next(
            (
                item
                for item in self._rooms
                if item.room_id == room_id
                and item.room_type == room_type_id
                and item.available
            ),
            None,
        )
        if room is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="La chambre demandee est introuvable.",
            )
        conflict = any(
            reservation.room_id == room_id
            and reservation.status != "cancelled"
            and reservation.arrival_date < departure_date
            and reservation.departure_date > arrival_date
            for reservation in self._reservations.values()
        )
        if conflict:
            return PMSResult(
                status=PMSStatus.CONFLICT,
                message="La chambre n'est plus disponible sur cette periode.",
                error_code="room_no_longer_available",
            )
        identifier = f"R-{len(self._reservations) + 1:04d}"
        reservation = Reservation(
            reservation_id=identifier,
            guest_name=f"{guest_first_name} {guest_last_name}",
            arrival_date=arrival_date,
            departure_date=departure_date,
            room_id=room_id,
            room_type=room_type_id,
            adults=adults,
            children=children,
        )
        self._reservations[identifier] = reservation
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation creee et relue avec succes.",
            data={"reservation": reservation.model_dump(mode="json")},
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

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
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable, aucune modification effectuee.",
                verified=True,
            )
        new_arrival = arrival_date or reservation.arrival_date
        new_departure = departure_date or reservation.departure_date
        if new_departure <= new_arrival:
            return PMSResult(
                status=PMSStatus.ERROR,
                message="La date de depart doit suivre la date d'arrivee.",
                error_code="invalid_date_range",
            )
        conflict = any(
            other.reservation_id != reservation_id
            and other.room_id == reservation.room_id
            and other.status != "cancelled"
            and other.arrival_date < new_departure
            and other.departure_date > new_arrival
            for other in self._reservations.values()
        )
        if conflict:
            return PMSResult(
                status=PMSStatus.CONFLICT,
                message="La chambre n'est pas disponible sur les nouvelles dates.",
                error_code="room_no_longer_available",
                verified=True,
            )
        reservation.arrival_date = new_arrival
        reservation.departure_date = new_departure
        if adults is not None:
            reservation.adults = adults
        if children is not None:
            reservation.children = children
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reservation modifiee et relue avec succes.",
            data={
                "reservation": reservation.model_dump(mode="json"),
                "occupancy": {
                    "adults": reservation.adults,
                    "children": reservation.children,
                },
            },
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

    def check_in_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable, aucun check-in effectue.",
                verified=True,
            )
        if reservation.status == "checked_out":
            return PMSResult(
                status=PMSStatus.CONFLICT,
                message="Le sejour est deja termine.",
                error_code="stay_already_checked_out",
                verified=True,
            )
        reservation.status = "checked_in"
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Check-in enregistre et relu avec succes.",
            data={"reservation": reservation.model_dump(mode="json")},
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

    def check_out_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable, aucun check-out effectue.",
                verified=True,
            )
        if reservation.status != "checked_in":
            return PMSResult(
                status=PMSStatus.CONFLICT,
                message="Le check-in doit etre enregistre avant le check-out.",
                error_code="stay_not_checked_in",
                verified=True,
            )
        reservation.status = "checked_out"
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Check-out enregistre et relu avec succes.",
            data={"reservation": reservation.model_dump(mode="json")},
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

    def get_invoice(self, *, reservation_id: str) -> PMSResult:
        if reservation_id not in self._reservations:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable.",
                verified=True,
            )
        invoice = self._invoices.get(reservation_id)
        if invoice is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Aucune facture n'a encore ete emise pour cette reservation.",
                error_code="invoice_not_issued",
                verified=True,
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Facture chargee et verifiee.",
            data={"invoice": dict(invoice)},
            verified=True,
        )

    def issue_invoice(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable, aucune facture emise.",
                verified=True,
            )
        invoice = self._invoices.setdefault(
            reservation_id,
            {
                "invoice_id": f"INV-{reservation_id}",
                "number": len(self._invoices) + 1,
                "reservation_id": reservation_id,
                "guest_name": reservation.guest_name,
                "currency": "EUR",
                "total_excluding_tax": "0.00",
                "total_including_tax": "0.00",
                "issued_at": date.today().isoformat(),
                "lines": [],
            },
        )
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Facture emise puis relue avec succes.",
            data={"invoice": dict(invoice)},
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

    def list_customer_requests(self, *, status: str = "open") -> PMSResult:
        requests = [dict(item) for item in self._customer_requests.values()]
        if status != "all":
            requests = [item for item in requests if item.get("status") == status]
        requests.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(requests)} demande(s) client trouvee(s).",
            data={"status_filter": status, "requests": requests},
            verified=True,
        )

    def get_customer_request(self, *, request_id: str) -> PMSResult:
        request = self._customer_requests.get(request_id)
        if request is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Demande client introuvable.",
                verified=True,
            )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message="Demande client chargee et verifiee.",
            data={"request": dict(request)},
            verified=True,
        )

    def reply_customer_request(
        self,
        *,
        request_id: str,
        message: str,
        idempotency_key: str,
    ) -> PMSResult:
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)
        request = self._customer_requests.get(request_id)
        if request is None:
            return PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Demande client introuvable, aucune reponse envoyee.",
                verified=True,
            )
        messages = list(request.get("messages") or [])
        messages.append(
            {
                "message_id": f"M-{len(messages) + 1}",
                "sender": "hotel",
                "message": message,
                "created_at": date.today().isoformat(),
            }
        )
        request["messages"] = messages
        request["status"] = "pending"
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Reponse client envoyee puis relue avec succes.",
            data={"request": dict(request)},
            external_operation_id=idempotency_key,
            verified=True,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)

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
        if unit_price is None:
            if extra_code != "BREAKFAST":
                return PMSResult(
                    status=PMSStatus.CONFLICT,
                    message="Le PMS de test ne contient pas de catalogue de prix.",
                    error_code="pms_catalog_price_unavailable",
                )
            unit_price = Decimal("15.00")
        previous = self._idempotent_results.get(idempotency_key)
        if previous is not None:
            return previous.model_copy(deep=True)

        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            result = PMSResult(
                status=PMSStatus.NOT_FOUND,
                message="Reservation introuvable, aucun extra ajoute.",
            )
            self._idempotent_results[idempotency_key] = result
            return result.model_copy(deep=True)

        reservation.extras.append(
            ExtraLine(
                code=extra_code,
                label=label,
                quantity=quantity,
                unit_price=unit_price,
            )
        )
        operation_id = uuid4().hex
        stored = self._reservations[reservation_id]
        verified = any(
            item.code == extra_code
            and item.quantity == quantity
            and item.unit_price == unit_price
            for item in stored.extras
        )
        result = PMSResult(
            status=PMSStatus.SUCCESS,
            message="Extra ajoute et reservation relue avec succes.",
            data={"reservation": stored.model_dump(mode="json")},
            external_operation_id=operation_id,
            verified=verified,
        )
        self._idempotent_results[idempotency_key] = result
        return result.model_copy(deep=True)
