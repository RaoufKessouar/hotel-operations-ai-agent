"""Port metier que chaque PMS reel devra implementer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any

from .models import PMSResult, PMSStatus


class PMSAdapter(ABC):
    """Aucune methode ne doit exposer directement un endpoint fournisseur."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    def search_reservations(
        self, *, guest_name: str | None = None, reservation_id: str | None = None
    ) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def get_reservation(self, reservation_id: str) -> PMSResult:
        raise NotImplementedError

    def query_stays(
        self,
        *,
        stay_date: date | None = None,
        arrival_date: date | None = None,
        departure_date: date | None = None,
        guest_name: str | None = None,
        room_type: str | None = None,
        status: str = "all",
    ) -> PMSResult:
        """Filtre les reservations avec une interface metier generique.

        Les adaptateurs peuvent surcharger cette lecture; par defaut, elle
        reutilise leur recherche existante et applique la convention hoteliere
        de presence [arrivee, depart).
        """
        result = self.search_reservations()
        if result.status not in {PMSStatus.SUCCESS, PMSStatus.AMBIGUOUS}:
            if result.status == PMSStatus.NOT_FOUND:
                return PMSResult(
                    status=PMSStatus.SUCCESS,
                    message="Aucun sejour ne correspond aux filtres.",
                    data={"reservations": []},
                    verified=True,
                )
            return result
        raw = result.data.get("reservations")
        if not isinstance(raw, list):
            item = result.data.get("reservation")
            raw = [item] if isinstance(item, dict) else []
        needle = (guest_name or "").casefold().strip()
        type_needle = (room_type or "").casefold().strip()
        filtered: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                arrival = date.fromisoformat(str(item.get("arrival_date"))[:10])
                departure = date.fromisoformat(str(item.get("departure_date"))[:10])
            except (TypeError, ValueError):
                continue
            item_status = str(item.get("status") or "confirmed")
            if status != "all" and item_status != status:
                continue
            if status == "all" and item_status in {"cancelled", "refunded"}:
                continue
            if stay_date is not None and not (arrival <= stay_date < departure):
                continue
            if arrival_date is not None and arrival != arrival_date:
                continue
            if departure_date is not None and departure != departure_date:
                continue
            if needle and needle not in str(item.get("guest_name") or "").casefold():
                continue
            if type_needle and type_needle not in str(item.get("room_type") or "").casefold():
                continue
            filtered.append(item)
        filtered.sort(
            key=lambda item: (
                str(item.get("arrival_date") or ""),
                str(item.get("guest_name") or "").casefold(),
                str(item.get("reservation_id") or ""),
            )
        )
        return PMSResult(
            status=PMSStatus.SUCCESS,
            message=f"{len(filtered)} sejour(s) correspondent aux filtres.",
            data={
                "stay_date": stay_date.isoformat() if stay_date else None,
                "arrival_date": arrival_date.isoformat() if arrival_date else None,
                "departure_date": departure_date.isoformat() if departure_date else None,
                "guest_name": guest_name,
                "room_type": room_type,
                "status": status,
                "reservations": filtered,
            },
            verified=True,
        )

    @abstractmethod
    def list_arrivals(self, *, arrival_date: date) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def list_departures(self, *, departure_date: date) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def get_occupancy(self, *, stay_date: date) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def get_occupancy_range(
        self, *, start_date: date, end_date: date
    ) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def check_availability(
        self,
        *,
        arrival_date: date,
        departure_date: date,
        room_type: str | None = None,
        adults: int = 2,
        children: int = 0,
    ) -> PMSResult:
        raise NotImplementedError

    def quote_stay(
        self,
        *,
        arrival_date: date,
        departure_date: date,
        adults: int = 2,
        children: int = 0,
        include_breakfast: bool = False,
    ) -> PMSResult:
        return PMSResult(
            status=PMSStatus.CONFLICT,
            message="Le PMS ne fournit pas de devis detaille.",
            error_code="stay_quote_not_supported",
        )

    @abstractmethod
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
        raise NotImplementedError

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
        return (
            f"Creer la reservation de {guest_first_name} {guest_last_name}, "
            f"du {arrival_date.isoformat()} au {departure_date.isoformat()}, "
            f"pour {adults} adulte(s) et {children} enfant(s), "
            f"chambre {room_id}. Confirmer ?"
        )

    @abstractmethod
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
        raise NotImplementedError

    def preview_update_reservation(
        self,
        *,
        reservation_id: str,
        arrival_date: date | None,
        departure_date: date | None,
        adults: int | None,
        children: int | None,
    ) -> str:
        changes = []
        if arrival_date is not None:
            changes.append(f"arrivee au {arrival_date.isoformat()}")
        if departure_date is not None:
            changes.append(f"depart au {departure_date.isoformat()}")
        if adults is not None:
            changes.append(f"{adults} adulte(s)")
        if children is not None:
            changes.append(f"{children} enfant(s)")
        return (
            f"Modifier la reservation {reservation_id} : "
            + ", ".join(changes)
            + ". Le PMS recalculera le montant. Confirmer ?"
        )

    @abstractmethod
    def check_in_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        raise NotImplementedError

    def preview_check_in(self, *, reservation_id: str) -> str:
        return f"Enregistrer le check-in de la reservation {reservation_id}. Confirmer ?"

    @abstractmethod
    def check_out_reservation(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        raise NotImplementedError

    def preview_check_out(self, *, reservation_id: str) -> str:
        return f"Enregistrer le check-out de la reservation {reservation_id}. Confirmer ?"

    @abstractmethod
    def get_invoice(self, *, reservation_id: str) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def issue_invoice(
        self, *, reservation_id: str, idempotency_key: str
    ) -> PMSResult:
        raise NotImplementedError

    def preview_issue_invoice(self, *, reservation_id: str) -> str:
        return (
            f"Emettre la facture QloApps de la reservation {reservation_id}. "
            "Cette action attribue un numero de facture. Confirmer ?"
        )

    @abstractmethod
    def list_customer_requests(self, *, status: str = "open") -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def get_customer_request(self, *, request_id: str) -> PMSResult:
        raise NotImplementedError

    @abstractmethod
    def reply_customer_request(
        self,
        *,
        request_id: str,
        message: str,
        idempotency_key: str,
    ) -> PMSResult:
        raise NotImplementedError

    def preview_reply_customer_request(
        self, *, request_id: str, message: str
    ) -> str:
        return (
            f"Envoyer au client la reponse suivante pour la demande {request_id} : "
            f"« {message} » Confirmer ?"
        )

    @abstractmethod
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
        raise NotImplementedError

    def preview_add_extra(
        self,
        *,
        reservation_id: str,
        extra_code: str,
        label: str,
        quantity: int,
        unit_price: Decimal | None,
    ) -> str:
        if unit_price is None:
            return (
                f"Ajouter {quantity} x {label} a la reservation {reservation_id}. "
                "Le prix sera lu et valide dans le PMS."
            )
        total = unit_price * quantity
        return (
            f"Ajouter {quantity} x {label} ({total:.2f} EUR) "
            f"a la reservation {reservation_id}."
        )
