"""Client HTTP minimal et borne pour l'API QloApps 1.7.x."""

from __future__ import annotations

import base64
import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ALLOWED_RESOURCES = frozenset(
    {
        "addresses",
        "booking_extra_demands",
        "bookings",
        "customers",
        "customer_messages",
        "customer_threads",
        "extra_demands",
        "hotel_ari",
        "hotel_rooms",
        "hotels",
        "order_details",
        "order_invoices",
        "orders",
        "room_bookings",
        "room_types",
        "services",
        "tax_rules",
        "taxes",
    }
)


def _nodes(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return [value]


def _xml_text(parent: ElementTree.Element, name: str, value: Any) -> None:
    ElementTree.SubElement(parent, name).text = str(value)


@dataclass(frozen=True)
class QloAppsAPIError(Exception):
    message: str
    status_code: int | None = None
    error_code: str = "qloapps_api_error"
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class JSONTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Any: ...


def _error_details(body: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "QloApps a retourne une erreur HTTP.", "qloapps_http_error"
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if isinstance(errors, dict):
        errors = [errors]
    if errors and isinstance(errors[0], dict):
        message = str(errors[0].get("message") or "Erreur QloApps.")
        code = str(errors[0].get("code") or "qloapps_http_error")
        return message, f"qloapps_{code}"
    if isinstance(payload, dict) and payload.get("error"):
        message = str(payload.get("message") or payload["error"])
        code = str(payload["error"])
        return message, f"qloapps_bridge_{code}"
    return "QloApps a retourne une erreur HTTP.", "qloapps_http_error"


class UrllibJSONTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Any:
        request = Request(url=url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            message, error_code = _error_details(raw)
            raise QloAppsAPIError(
                message=message,
                status_code=exc.code,
                error_code=error_code,
                retryable=exc.code >= 500 or exc.code == 429,
            ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise QloAppsAPIError(
                message="L'API QloApps est indisponible ou a expire.",
                error_code="qloapps_unavailable",
                retryable=True,
            ) from exc

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QloAppsAPIError(
                message="La reponse QloApps n'est pas un JSON valide.",
                error_code="qloapps_invalid_json",
            ) from exc


class QloAppsClient:
    """Expose uniquement les appels necessaires au port metier PMSAdapter."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 15,
        transport: JSONTransport | None = None,
    ) -> None:
        if not api_key or len(api_key) != 32 or not api_key.isalnum():
            raise ValueError("La cle API QloApps doit contenir 32 caracteres.")
        normalized = base_url.rstrip("/")
        if not normalized.lower().endswith("/api"):
            normalized += "/api"
        self.base_url = normalized
        self.root_url = normalized[:-4]
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJSONTransport()
        token = base64.b64encode(f"{api_key}:".encode("ascii")).decode("ascii")
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
        }

    def _request(
        self,
        method: str,
        resource: str | None = None,
        *,
        resource_id: str | int | None = None,
        query: Mapping[str, str | int] | None = None,
        xml_body: bytes | None = None,
    ) -> Any:
        if resource is not None and resource not in ALLOWED_RESOURCES:
            raise ValueError(f"Ressource QloApps non autorisee: {resource}")
        url = self.base_url + "/"
        if resource is not None:
            url += quote(resource, safe="") + "/"
        if resource_id is not None:
            url += quote(str(resource_id), safe="")
        parameters: dict[str, str | int] = {"output_format": "JSON"}
        if query:
            parameters.update(query)
        url += "?" + urlencode(parameters)
        headers = dict(self._headers)
        if xml_body is not None:
            headers["Content-Type"] = "application/xml"
        return self._transport.request(
            method=method,
            url=url,
            headers=headers,
            body=xml_body,
            timeout=self.timeout_seconds,
        )

    def discover_resources(self) -> set[str]:
        payload = self._request("GET")
        if not isinstance(payload, list):
            raise QloAppsAPIError(
                "Le catalogue de ressources QloApps est invalide.",
                error_code="qloapps_invalid_resource_catalog",
            )
        return {str(item) for item in payload}

    def list_booking_ids(self) -> list[str]:
        payload = self._request("GET", "bookings")
        if payload == []:
            return []
        nodes: Any
        if isinstance(payload, dict):
            nodes = payload.get("bookings", [])
        else:
            nodes = payload
        if isinstance(nodes, dict):
            nodes = [nodes]
        if not isinstance(nodes, list):
            raise QloAppsAPIError(
                "La liste des reservations QloApps est invalide.",
                error_code="qloapps_invalid_booking_list",
            )
        identifiers: list[str] = []
        for node in nodes:
            if isinstance(node, dict) and node.get("id") is not None:
                identifiers.append(str(node["id"]))
            elif isinstance(node, (str, int)):
                identifiers.append(str(node))
        return identifiers

    def get_booking(self, reservation_id: str) -> dict[str, Any]:
        payload = self._request("GET", "bookings", resource_id=reservation_id)
        booking = payload.get("booking") if isinstance(payload, dict) else None
        if not isinstance(booking, dict):
            raise QloAppsAPIError(
                "La reservation QloApps est invalide.",
                error_code="qloapps_invalid_booking",
            )
        return booking

    def get_room_type(self, room_type_id: str | int) -> dict[str, Any]:
        payload = self._request("GET", "room_types", resource_id=room_type_id)
        room_type = payload.get("room_type") if isinstance(payload, dict) else None
        if not isinstance(room_type, dict):
            raise QloAppsAPIError(
                "Le type de chambre QloApps est invalide.",
                error_code="qloapps_invalid_room_type",
            )
        return room_type

    def list_hotel_rooms(self, hotel_id: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "hotel_rooms",
            query={
                "display": "full",
                "filter[id_hotel]": f"[{hotel_id}]",
            },
        )
        rooms = payload.get("rooms") if isinstance(payload, dict) else None
        if rooms == []:
            return []
        if isinstance(rooms, dict):
            rooms = [rooms]
        if not isinstance(rooms, list) or not all(
            isinstance(item, dict) for item in rooms
        ):
            raise QloAppsAPIError(
                "L'inventaire des chambres QloApps est invalide.",
                error_code="qloapps_invalid_room_inventory",
            )
        return [
            room for room in rooms if int(room.get("id_hotel") or 0) == hotel_id
        ]

    def list_room_bookings(self, hotel_id: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "room_bookings",
            query={
                "display": "full",
                "filter[id_hotel]": f"[{hotel_id}]",
            },
        )
        bookings = payload.get("bookings") if isinstance(payload, dict) else None
        if bookings == []:
            return []
        if isinstance(bookings, dict):
            bookings = [bookings]
        if not isinstance(bookings, list) or not all(
            isinstance(item, dict) for item in bookings
        ):
            raise QloAppsAPIError(
                "La liste des sejours QloApps est invalide.",
                error_code="qloapps_invalid_room_booking_list",
            )
        return [
            booking
            for booking in bookings
            if int(booking.get("id_hotel") or 0) == hotel_id
        ]

    def get_room_booking(self, room_booking_id: str | int) -> dict[str, Any]:
        payload = self._request(
            "GET", "room_bookings", resource_id=room_booking_id
        )
        booking = payload.get("booking") if isinstance(payload, dict) else None
        if not isinstance(booking, dict):
            raise QloAppsAPIError(
                "Le sejour QloApps est invalide.",
                error_code="qloapps_invalid_room_booking",
            )
        return booking

    def get_order(self, order_id: str | int) -> dict[str, Any]:
        payload = self._request("GET", "orders", resource_id=order_id)
        order = payload.get("order") if isinstance(payload, dict) else None
        if not isinstance(order, dict):
            raise QloAppsAPIError(
                "La commande QloApps associee au sejour est invalide.",
                error_code="qloapps_invalid_order",
            )
        return order

    def list_order_invoices(self, order_id: str | int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "order_invoices",
            query={
                "display": "full",
                "filter[id_order]": f"[{order_id}]",
            },
        )
        invoices = payload.get("order_invoices") if isinstance(payload, dict) else None
        if invoices in (None, []):
            return []
        if isinstance(invoices, dict):
            invoices = [invoices]
        if not isinstance(invoices, list) or not all(
            isinstance(item, dict) for item in invoices
        ):
            raise QloAppsAPIError(
                "La liste des factures QloApps est invalide.",
                error_code="qloapps_invalid_invoice_list",
            )
        return invoices

    def list_order_details(self, order_id: str | int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "order_details",
            query={
                "display": "full",
                "filter[id_order]": f"[{order_id}]",
            },
        )
        details = payload.get("order_details") if isinstance(payload, dict) else None
        if details in (None, []):
            return []
        if isinstance(details, dict):
            details = [details]
        if not isinstance(details, list) or not all(
            isinstance(item, dict) for item in details
        ):
            raise QloAppsAPIError(
                "Les lignes de facture QloApps sont invalides.",
                error_code="qloapps_invalid_order_details",
            )
        return details

    def issue_invoice(
        self,
        *,
        order_id: str | int,
        idempotency_key: str,
    ) -> list[dict[str, Any]]:
        body = json.dumps(
            {
                "action": "issue_invoice",
                "order_id": int(order_id),
                "idempotency_key": idempotency_key,
            }
        ).encode("utf-8")
        payload = self._transport.request(
            method="POST",
            url=self.root_url + "/agentpms-bridge.php",
            headers={**self._headers, "Content-Type": "application/json"},
            body=body,
            timeout=self.timeout_seconds,
        )
        invoices = payload.get("invoices") if isinstance(payload, dict) else None
        if isinstance(invoices, dict):
            invoices = [invoices]
        if not isinstance(invoices, list) or not invoices or not all(
            isinstance(item, dict) for item in invoices
        ):
            raise QloAppsAPIError(
                "QloApps n'a pas confirme l'emission de la facture.",
                error_code="qloapps_invalid_invoice_issue",
            )
        return invoices

    def update_booking_via_backoffice(
        self,
        *,
        reservation_id: str | int,
        arrival_date: date,
        departure_date: date,
        adults: int,
        children: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Utilise l'operation metier QloApps qui sait exclure le sejour courant."""
        body = json.dumps(
            {
                "action": "update_booking",
                "reservation_id": int(reservation_id),
                "arrival_date": arrival_date.isoformat(),
                "departure_date": departure_date.isoformat(),
                "adults": adults,
                "children": children,
                "idempotency_key": idempotency_key,
            }
        ).encode("utf-8")
        payload = self._transport.request(
            method="POST",
            url=self.root_url + "/agentpms-bridge.php",
            headers={**self._headers, "Content-Type": "application/json"},
            body=body,
            timeout=self.timeout_seconds,
        )
        if not isinstance(payload, dict) or payload.get("result") not in (True, 1, "1"):
            message = (
                str(payload.get("error"))
                if isinstance(payload, dict) and payload.get("error")
                else "QloApps n'a pas confirme la modification de la reservation."
            )
            raise QloAppsAPIError(
                message,
                error_code="qloapps_invalid_backoffice_update",
            )
        return payload

    def list_customer_threads(self) -> list[dict[str, Any]]:
        fields = (
            "[id,id_lang,id_shop,id_customer,id_order,id_contact,user_name,phone,"
            "subject,id_employee,email,token,status,date_add,date_upd]"
        )
        payload = self._request(
            "GET",
            "customer_threads",
            query={"display": fields},
        )
        threads = payload.get("customer_threads") if isinstance(payload, dict) else None
        if threads in (None, []):
            return []
        if isinstance(threads, dict):
            threads = [threads]
        if not isinstance(threads, list) or not all(
            isinstance(item, dict) for item in threads
        ):
            raise QloAppsAPIError(
                "La liste des demandes clients QloApps est invalide.",
                error_code="qloapps_invalid_customer_thread_list",
            )
        return threads

    def get_customer_thread(self, thread_id: str | int) -> dict[str, Any]:
        fields = (
            "[id,id_lang,id_shop,id_customer,id_order,id_contact,user_name,phone,"
            "subject,id_employee,email,token,status,date_add,date_upd]"
        )
        payload = self._request(
            "GET",
            "customer_threads",
            query={
                "display": fields,
                "filter[id]": f"[{thread_id}]",
            },
        )
        threads = payload.get("customer_threads") if isinstance(payload, dict) else None
        if isinstance(threads, dict):
            threads = [threads]
        if not isinstance(threads, list) or len(threads) != 1 or not isinstance(
            threads[0], dict
        ):
            raise QloAppsAPIError(
                "La demande client QloApps est introuvable ou invalide.",
                status_code=404 if threads in (None, []) else None,
                error_code="qloapps_invalid_customer_thread",
            )
        return threads[0]

    def list_customer_messages(self, thread_id: str | int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "customer_messages",
            query={
                "display": "full",
                "filter[id_customer_thread]": f"[{thread_id}]",
            },
        )
        messages = payload.get("customer_messages") if isinstance(payload, dict) else None
        if messages in (None, []):
            return []
        if isinstance(messages, dict):
            messages = [messages]
        if not isinstance(messages, list) or not all(
            isinstance(item, dict) for item in messages
        ):
            raise QloAppsAPIError(
                "L'historique de la demande client QloApps est invalide.",
                error_code="qloapps_invalid_customer_message_list",
            )
        return messages

    def get_customer_message(self, message_id: str | int) -> dict[str, Any]:
        payload = self._request("GET", "customer_messages", resource_id=message_id)
        message = payload.get("customer_message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise QloAppsAPIError(
                "Le message client QloApps est invalide.",
                error_code="qloapps_invalid_customer_message",
            )
        return message

    def create_customer_message(
        self,
        *,
        thread_id: str | int,
        employee_id: int,
        message: str,
    ) -> dict[str, Any]:
        root = ElementTree.Element("qloapps")
        node = ElementTree.SubElement(root, "customer_message")
        for name, value in {
            "id_customer_thread": thread_id,
            "id_employee": employee_id,
            "message": message,
            "private": 0,
            "read": 1,
        }.items():
            _xml_text(node, name, value)
        payload = self._request(
            "POST",
            "customer_messages",
            xml_body=ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True
            ),
        )
        created = payload.get("customer_message") if isinstance(payload, dict) else None
        if not isinstance(created, dict) or created.get("id") in (None, ""):
            raise QloAppsAPIError(
                "QloApps n'a pas confirme l'envoi de la reponse client.",
                error_code="qloapps_invalid_customer_message_creation",
            )
        return created

    def update_room_booking_status(
        self,
        booking: Mapping[str, Any],
        *,
        status: int,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        if status not in {2, 3}:
            raise ValueError("Le statut de sejour doit etre check-in ou check-out")
        identifier = booking.get("id")
        if identifier in (None, ""):
            raise QloAppsAPIError(
                "L'identifiant du sejour QloApps est absent.",
                error_code="qloapps_incomplete_room_booking_update",
            )
        root = ElementTree.Element("qloapps")
        node = ElementTree.SubElement(root, "booking")
        writable_fields = (
            "id",
            "id_product",
            "id_hotel",
            "id_order",
            "id_order_detail",
            "id_cart",
            "id_room",
            "id_customer",
            "booking_type",
            "id_status",
            "comment",
            "check_in",
            "check_out",
            "date_from",
            "date_to",
            "total_price_tax_excl",
            "total_price_tax_incl",
            "total_paid_amount",
            "is_refunded",
            "is_cancelled",
            "is_back_order",
            "room_num",
            "room_type_name",
            "hotel_name",
            "city",
            "state",
            "country",
            "zipcode",
            "phone",
            "email",
            "check_in_time",
            "check_out_time",
            "planned_check_out",
            "adults",
            "children",
            "child_ages",
        )
        values = dict(booking)
        values["id_status"] = status
        timestamp = occurred_at.replace(microsecond=0).isoformat(sep=" ")
        values["check_in" if status == 2 else "check_out"] = timestamp
        for field in writable_fields:
            value = values.get(field)
            if value is not None:
                _xml_text(node, field, value)
        payload = self._request(
            "PUT",
            "room_bookings",
            resource_id=identifier,
            xml_body=ElementTree.tostring(
                root, encoding="utf-8", xml_declaration=True
            ),
        )
        updated = payload.get("booking") if isinstance(payload, dict) else None
        if not isinstance(updated, dict):
            raise QloAppsAPIError(
                "QloApps n'a pas confirme le changement de statut du sejour.",
                error_code="qloapps_invalid_room_booking_update",
            )
        return updated

    def list_services(self) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "services",
            query={"display": "full"},
        )
        services = payload.get("services") if isinstance(payload, dict) else None
        if services == []:
            return []
        if isinstance(services, dict):
            services = [services]
        if not isinstance(services, list) or not all(
            isinstance(item, dict) for item in services
        ):
            raise QloAppsAPIError(
                "Le catalogue de services QloApps est invalide.",
                error_code="qloapps_invalid_service_catalog",
            )
        return services

    def get_uniform_tax_rate(self, tax_rules_group_id: int) -> Decimal:
        """Retourne le taux uniquement si toutes les regles du groupe concordent."""
        if tax_rules_group_id == 0:
            return Decimal("0")
        payload = self._request(
            "GET",
            "tax_rules",
            query={
                "display": "full",
                "filter[id_tax_rules_group]": f"[{tax_rules_group_id}]",
            },
        )
        rules = payload.get("tax_rules") if isinstance(payload, dict) else None
        if isinstance(rules, dict):
            rules = [rules]
        if not isinstance(rules, list) or not rules:
            raise QloAppsAPIError(
                "Aucune regle fiscale QloApps ne permet de calculer le total.",
                error_code="qloapps_tax_rules_unavailable",
            )
        tax_ids = {
            str(rule.get("id_tax"))
            for rule in rules
            if isinstance(rule, Mapping) and rule.get("id_tax") not in (None, "")
        }
        if not tax_ids:
            raise QloAppsAPIError(
                "Les regles fiscales QloApps sont invalides.",
                error_code="qloapps_invalid_tax_rules",
            )

        rates: set[Decimal] = set()
        for tax_id in tax_ids:
            tax_payload = self._request("GET", "taxes", resource_id=tax_id)
            tax = tax_payload.get("tax") if isinstance(tax_payload, dict) else None
            if not isinstance(tax, Mapping) or not int(tax.get("active") or 0):
                raise QloAppsAPIError(
                    "Une taxe QloApps requise est absente ou inactive.",
                    error_code="qloapps_tax_unavailable",
                )
            try:
                rates.add(Decimal(str(tax["rate"])))
            except (KeyError, InvalidOperation) as exc:
                raise QloAppsAPIError(
                    "Le taux de taxe QloApps est invalide.",
                    error_code="qloapps_invalid_tax_rate",
                ) from exc
        if len(rates) != 1:
            raise QloAppsAPIError(
                "Le taux fiscal depend du contexte; le total exact est ambigu.",
                error_code="qloapps_ambiguous_tax_rate",
            )
        return rates.pop()

    def create_booking(
        self,
        *,
        operation_marker: str,
        hotel_id: int,
        guest_first_name: str,
        guest_last_name: str,
        email: str,
        phone: str,
        room_type_id: int,
        room_id: int,
        arrival_date: date,
        departure_date: date,
        adults: int,
        children: int,
        room_unit_price_without_tax: Decimal,
        room_tax_rules_group_id: int,
    ) -> dict[str, Any]:
        root = ElementTree.Element("qloapps")
        booking = ElementTree.SubElement(root, "booking")
        for name, value in {
            "id_property": hotel_id,
            "currency": "EUR",
            "booking_status": 1,
            "payment_status": 3,
            "source": operation_marker,
            "remark": "Reservation creee par AgentPMS apres confirmation",
            "id_language": 1,
        }.items():
            _xml_text(booking, name, value)

        associations = ElementTree.SubElement(booking, "associations")
        customer = ElementTree.SubElement(associations, "customer_detail")
        for name, value in {
            "firstname": guest_first_name,
            "lastname": guest_last_name,
            "email": email,
            "phone": phone,
        }.items():
            _xml_text(customer, name, value)

        room_types = ElementTree.SubElement(associations, "room_types")
        room_type = ElementTree.SubElement(room_types, "room_type")
        for name, value in {
            "id_room_type": room_type_id,
            "checkin_date": arrival_date.isoformat(),
            "checkout_date": departure_date.isoformat(),
            "number_of_rooms": 1,
        }.items():
            _xml_text(room_type, name, value)
        rooms = ElementTree.SubElement(room_type, "rooms")
        room = ElementTree.SubElement(rooms, "room")
        _xml_text(room, "id_room", room_id)
        _xml_text(room, "adults", adults)
        _xml_text(room, "child", children)
        # QloApps transforme ce champ en prix journalier via createFeaturePrice.
        # L'adaptateur compense ce calcul particulier avant d'appeler le client.
        _xml_text(room, "unit_price_without_tax", room_unit_price_without_tax)
        _xml_text(room, "id_tax_rules_group", room_tax_rules_group_id)

        payload = self._request(
            "POST",
            "bookings",
            xml_body=ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
        )
        created = payload.get("booking") if isinstance(payload, dict) else None
        if not isinstance(created, dict) or created.get("id") in (None, ""):
            raise QloAppsAPIError(
                "QloApps n'a pas confirme la creation de la reservation.",
                error_code="qloapps_invalid_booking_creation",
            )
        return created

    def update_booking(self, booking: Mapping[str, Any]) -> dict[str, Any]:
        """Met a jour un dossier en preservant ses chambres et ses services."""
        identifier = booking.get("id")
        property_id = booking.get("id_property")
        currency = booking.get("currency")
        associations = booking.get("associations")
        if not identifier or not property_id or not currency or not isinstance(
            associations, Mapping
        ):
            raise QloAppsAPIError(
                "La reservation ne contient pas les champs requis pour PUT.",
                error_code="qloapps_incomplete_booking_update",
            )
        customer = associations.get("customer_detail")
        if not isinstance(customer, Mapping):
            raise QloAppsAPIError(
                "Le client de la reservation est absent du payload PUT.",
                error_code="qloapps_incomplete_booking_customer",
            )

        root = ElementTree.Element("qloapps")
        booking_node = ElementTree.SubElement(root, "booking")
        _xml_text(booking_node, "id", identifier)
        _xml_text(booking_node, "id_property", property_id)
        _xml_text(booking_node, "currency", currency)
        associations_node = ElementTree.SubElement(booking_node, "associations")
        customer_node = ElementTree.SubElement(associations_node, "customer_detail")
        for field in ("id_customer", "firstname", "lastname", "email", "phone"):
            if customer.get(field) not in (None, ""):
                _xml_text(customer_node, field, customer[field])

        room_types_node = ElementTree.SubElement(associations_node, "room_types")
        room_types = [
            item
            for item in _nodes(associations.get("room_types"))
            if isinstance(item, Mapping)
        ]
        if not room_types:
            raise QloAppsAPIError(
                "Aucune chambre a preserver dans le payload PUT.",
                error_code="qloapps_incomplete_booking_rooms",
            )
        for room_type in room_types:
            rooms = [
                item
                for item in _nodes(room_type.get("rooms"))
                if isinstance(item, Mapping)
            ]
            room_type_node = ElementTree.SubElement(room_types_node, "room_type")
            for field in ("id_room_type", "checkin_date", "checkout_date"):
                if room_type.get(field) in (None, ""):
                    raise QloAppsAPIError(
                        f"Champ {field} absent du payload PUT.",
                        error_code="qloapps_incomplete_booking_rooms",
                    )
                _xml_text(room_type_node, field, room_type[field])
            _xml_text(room_type_node, "number_of_rooms", len(rooms))
            rooms_node = ElementTree.SubElement(room_type_node, "rooms")
            for room in rooms:
                room_node = ElementTree.SubElement(rooms_node, "room")
                for field in ("id_room", "adults", "child"):
                    if room.get(field) not in (None, ""):
                        _xml_text(room_node, field, room[field])
                # Contournement d'un acces PHP non garde dans QloApps 1.7.0.
                _xml_text(room_node, "total_tax", 0)
                services = [
                    item
                    for item in _nodes(room.get("services"))
                    if isinstance(item, Mapping)
                ]
                if services:
                    services_node = ElementTree.SubElement(room_node, "services")
                    for service in services:
                        service_node = ElementTree.SubElement(services_node, "service")
                        _xml_text(service_node, "id_service", service["id_service"])
                        _xml_text(service_node, "quantity", service.get("quantity") or 1)
                        if not service.get("_agentpms_new") and service.get(
                            "total_price_without_tax"
                        ) not in (None, ""):
                            _xml_text(
                                service_node,
                                "total_price_without_tax",
                                service["total_price_without_tax"],
                            )
                            if service.get("total_tax") not in (None, ""):
                                _xml_text(service_node, "total_tax", service["total_tax"])

        payload = self._request(
            "PUT",
            "bookings",
            resource_id=identifier,
            xml_body=ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
        )
        updated = payload.get("booking") if isinstance(payload, dict) else None
        if not isinstance(updated, dict):
            raise QloAppsAPIError(
                "QloApps n'a pas confirme la mise a jour de la reservation.",
                error_code="qloapps_invalid_booking_update",
            )
        return updated

    def update_booking_services(self, booking: Mapping[str, Any]) -> dict[str, Any]:
        """Compatibilite : mise a jour bornee utilisee par l'ajout de service."""
        return self.update_booking(booking)

    def query_availability(
        self,
        *,
        hotel_id: int,
        arrival_date: date,
        departure_date: date,
        room_type_id: int | None = None,
        adults: int = 2,
        children: int = 0,
    ) -> dict[str, Any]:
        root = ElementTree.Element("qloapps")
        ari = ElementTree.SubElement(root, "hotel_ari")
        values: list[tuple[str, str]] = [
            ("id_hotel", str(hotel_id)),
            ("date_from", arrival_date.isoformat()),
            ("date_to", departure_date.isoformat()),
            ("get_available_rooms", "1"),
        ]
        if room_type_id is not None:
            values.insert(1, ("id_room_type", str(room_type_id)))
        for name, value in values:
            ElementTree.SubElement(ari, name).text = value
        associations = ElementTree.SubElement(ari, "associations")
        occupancies = ElementTree.SubElement(associations, "room_occupancies")
        occupancy = ElementTree.SubElement(occupancies, "room_occupancy")
        ElementTree.SubElement(occupancy, "adults").text = str(adults)
        ElementTree.SubElement(occupancy, "children").text = str(children)

        payload = self._request(
            "POST",
            "hotel_ari",
            xml_body=ElementTree.tostring(root, encoding="utf-8"),
        )
        hotel_ari = payload.get("hotel_ari") if isinstance(payload, dict) else None
        if not isinstance(hotel_ari, dict):
            raise QloAppsAPIError(
                "La reponse de disponibilite QloApps est invalide.",
                error_code="qloapps_invalid_availability",
            )
        return hotel_ari
