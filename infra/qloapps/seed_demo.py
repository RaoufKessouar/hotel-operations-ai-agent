"""Cree les reservations fictives du laboratoire via l'API QloApps."""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


@dataclass(frozen=True)
class DemoBooking:
    marker: str
    firstname: str
    lastname: str
    email: str
    phone: str
    room_type_id: int
    room_id: int
    arrival_date: str
    departure_date: str
    service_id: int | None = None


DEMO_BOOKINGS = (
    DemoBooking(
        marker="AGENTPMS_FIXTURE_JEAN_DUPONT",
        firstname="Jean",
        lastname="Dupont",
        email="jean.dupont@agentpms.test",
        phone="0100000001",
        room_type_id=2,
        room_id=6,
        arrival_date="2026-10-10",
        departure_date="2026-10-12",
    ),
    DemoBooking(
        marker="AGENTPMS_FIXTURE_PAUL_DUPONT",
        firstname="Paul",
        lastname="Dupont",
        email="paul.dupont@agentpms.test",
        phone="0100000002",
        room_type_id=1,
        room_id=1,
        arrival_date="2026-10-11",
        departure_date="2026-10-13",
    ),
    DemoBooking(
        marker="AGENTPMS_FIXTURE_MARIE_MARTIN",
        firstname="Marie",
        lastname="Martin",
        email="marie.martin@agentpms.test",
        phone="0100000003",
        room_type_id=4,
        room_id=16,
        arrival_date="2026-10-15",
        departure_date="2026-10-17",
        service_id=9,
    ),
)

DEMO_CUSTOMER_REQUEST = {
    "token": "agentpmsfixt",
    "subject": "Arrivee tardive",
    "message": "Bonjour, puis-je arriver apres 22 h ?",
}


class SeedError(RuntimeError):
    pass


def _request(
    *,
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    body: bytes | None = None,
    parameters: dict[str, str] | None = None,
    accept_payload_on_error: bool = False,
) -> Any:
    query = urlencode({"output_format": "JSON", **(parameters or {})})
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}?{query}"
    token = base64.b64encode(f"{api_key}:".encode("ascii")).decode("ascii")
    headers = {"Accept": "application/json", "Authorization": f"Basic {token}"}
    if body is not None:
        headers["Content-Type"] = "application/xml"
    try:
        with urlopen(Request(url, data=body, headers=headers, method=method), timeout=30) as response:
            raw = response.read()
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        if accept_payload_on_error:
            try:
                payload = json.loads(details)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and any(
                key != "errors" for key in payload
            ):
                return payload
        raise SeedError(f"QloApps HTTP {error.code}: {details}") from error
    except (URLError, TimeoutError) as error:
        raise SeedError(f"QloApps unavailable: {error}") from error
    return json.loads(raw.decode("utf-8")) if raw else None


def _booking_ids(base_url: str, api_key: str) -> list[str]:
    payload = _request(
        base_url=base_url,
        api_key=api_key,
        method="GET",
        path="bookings/",
    )
    if payload == []:
        return []
    nodes = payload.get("bookings", []) if isinstance(payload, dict) else payload
    if isinstance(nodes, dict):
        nodes = [nodes]
    return [str(node["id"]) for node in nodes if isinstance(node, dict) and "id" in node]


def _booking(base_url: str, api_key: str, identifier: str) -> dict[str, Any]:
    payload = _request(
        base_url=base_url,
        api_key=api_key,
        method="GET",
        path=f"bookings/{identifier}",
    )
    booking = payload.get("booking") if isinstance(payload, dict) else None
    if not isinstance(booking, dict):
        raise SeedError(f"Invalid booking payload for {identifier}")
    return booking


def _booking_xml(seed: DemoBooking) -> bytes:
    root = ElementTree.Element("qloapps")
    booking = ElementTree.SubElement(root, "booking")
    fields = {
        "id_property": "1",
        "currency": "EUR",
        "booking_status": "1",
        "payment_status": "3",
        "source": "AgentPMS synthetic fixture",
        "remark": seed.marker,
        "id_language": "1",
    }
    for name, value in fields.items():
        ElementTree.SubElement(booking, name).text = value

    associations = ElementTree.SubElement(booking, "associations")
    customer = ElementTree.SubElement(associations, "customer_detail")
    customer_fields = {
        "firstname": seed.firstname,
        "lastname": seed.lastname,
        "email": seed.email,
        "phone": seed.phone,
    }
    for name, value in customer_fields.items():
        ElementTree.SubElement(customer, name).text = value

    room_types = ElementTree.SubElement(associations, "room_types")
    room_type = ElementTree.SubElement(room_types, "room_type")
    for name, value in {
        "id_room_type": str(seed.room_type_id),
        "checkin_date": seed.arrival_date,
        "checkout_date": seed.departure_date,
        "number_of_rooms": "1",
    }.items():
        ElementTree.SubElement(room_type, name).text = value

    rooms = ElementTree.SubElement(room_type, "rooms")
    room = ElementTree.SubElement(rooms, "room")
    ElementTree.SubElement(room, "id_room").text = str(seed.room_id)
    ElementTree.SubElement(room, "adults").text = "2"
    ElementTree.SubElement(room, "child").text = "0"
    # QloApps 1.7.0 reads this optional schema field without checking that it
    # exists (WebserviceSpecificManagementBookings.php). Sending zero avoids
    # the upstream warning while preserving the PMS price calculation.
    ElementTree.SubElement(room, "total_tax").text = "0"
    if seed.service_id is not None:
        services = ElementTree.SubElement(room, "services")
        service = ElementTree.SubElement(services, "service")
        ElementTree.SubElement(service, "id_service").text = str(seed.service_id)
        ElementTree.SubElement(service, "quantity").text = "1"
        ElementTree.SubElement(service, "total_tax").text = "0"
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _resource_nodes(payload: Any, key: str) -> list[dict[str, Any]]:
    if payload == []:
        return []
    nodes = payload.get(key, []) if isinstance(payload, dict) else payload
    if isinstance(nodes, dict):
        nodes = [nodes]
    return [item for item in nodes if isinstance(item, dict)]


def _customer_thread_xml(booking: dict[str, Any]) -> bytes:
    customer = booking.get("associations", {}).get("customer_detail", {})
    root = ElementTree.Element("qloapps")
    thread = ElementTree.SubElement(root, "customer_thread")
    fields = {
        "id_lang": "1",
        "id_contact": "1",
        "id_shop": "1",
        "id_customer": str(customer.get("id_customer") or "0"),
        "id_order": str(booking["id"]),
        "user_name": "Jean Dupont",
        "email": "jean.dupont@agentpms.test",
        "phone": "0100000001",
        "subject": DEMO_CUSTOMER_REQUEST["subject"],
        "status": "1",
        "token": DEMO_CUSTOMER_REQUEST["token"],
    }
    for name, value in fields.items():
        ElementTree.SubElement(thread, name).text = value
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _customer_message_xml(thread_id: str) -> bytes:
    root = ElementTree.Element("qloapps")
    message = ElementTree.SubElement(root, "customer_message")
    fields = {
        "id_customer_thread": thread_id,
        "id_employee": "0",
        "message": DEMO_CUSTOMER_REQUEST["message"],
        "private": "0",
        "read": "0",
    }
    for name, value in fields.items():
        ElementTree.SubElement(message, name).text = value
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _seed_customer_request(
    base_url: str,
    api_key: str,
    booking: dict[str, Any],
) -> dict[str, Any]:
    threads_payload = _request(
        base_url=base_url,
        api_key=api_key,
        method="GET",
        path="customer_threads/",
        parameters={"display": "full"},
        accept_payload_on_error=True,
    )
    threads = _resource_nodes(threads_payload, "customer_threads")
    existing = next(
        (
            item
            for item in threads
            if str(item.get("token") or "") == DEMO_CUSTOMER_REQUEST["token"]
        ),
        None,
    )
    created = False
    if existing is None:
        try:
            payload = _request(
                base_url=base_url,
                api_key=api_key,
                method="POST",
                path="customer_threads/",
                body=_customer_thread_xml(booking),
                accept_payload_on_error=True,
            )
            existing = (
                payload.get("customer_thread") if isinstance(payload, dict) else None
            )
            if not isinstance(existing, dict) or existing.get("id") in (None, ""):
                raise SeedError("QloApps did not confirm the customer request fixture")
            created = True
        except SeedError:
            # Comme pour bookings, QloApps peut persister l'objet avant de
            # retourner un warning de serialisation. La relecture par token
            # est la seule preuve acceptee et empeche tout doublon.
            recovered_payload = _request(
                base_url=base_url,
                api_key=api_key,
                method="GET",
                path="customer_threads/",
                parameters={"display": "full"},
                accept_payload_on_error=True,
            )
            existing = next(
                (
                    item
                    for item in _resource_nodes(
                        recovered_payload, "customer_threads"
                    )
                    if str(item.get("token") or "")
                    == DEMO_CUSTOMER_REQUEST["token"]
                ),
                None,
            )
            if existing is None:
                raise
            created = True
    thread_id = str(existing["id"])
    full_thread_payload = _request(
        base_url=base_url,
        api_key=api_key,
        method="GET",
        path=f"customer_threads/{thread_id}",
        accept_payload_on_error=True,
    )
    full_thread = (
        full_thread_payload.get("customer_thread")
        if isinstance(full_thread_payload, dict)
        else None
    )
    if not isinstance(full_thread, dict):
        raise SeedError("Customer request fixture cannot be read back")
    associations = full_thread.get("associations", {})
    message_nodes = associations.get("customer_messages", [])
    if isinstance(message_nodes, dict) and "customer_message" in message_nodes:
        message_nodes = message_nodes["customer_message"]
    if isinstance(message_nodes, dict):
        message_nodes = [message_nodes]
    if not isinstance(message_nodes, list):
        message_nodes = []
    messages = []
    for node in message_nodes:
        message_id = node.get("id") if isinstance(node, dict) else node
        if message_id in (None, ""):
            continue
        payload = _request(
            base_url=base_url,
            api_key=api_key,
            method="GET",
            path=f"customer_messages/{message_id}",
            accept_payload_on_error=True,
        )
        message = payload.get("customer_message") if isinstance(payload, dict) else None
        if isinstance(message, dict):
            messages.append(message)
    if not any(
        str(item.get("message") or "") == DEMO_CUSTOMER_REQUEST["message"]
        for item in messages
    ):
        payload = _request(
            base_url=base_url,
            api_key=api_key,
            method="POST",
            path="customer_messages/",
            body=_customer_message_xml(thread_id),
            accept_payload_on_error=True,
        )
        message = payload.get("customer_message") if isinstance(payload, dict) else None
        if not isinstance(message, dict) or message.get("id") in (None, ""):
            raise SeedError("QloApps did not confirm the customer message fixture")
    return {"id": thread_id, "created": created, "verified": True}


def seed_demo(
    base_url: str, api_key: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing: dict[str, str] = {}
    for identifier in _booking_ids(base_url, api_key):
        booking = _booking(base_url, api_key, identifier)
        associations = booking.get("associations", {})
        customer = associations.get("customer_detail", {})
        email = str(customer.get("email") or "").casefold()
        if email:
            existing[email] = identifier

    summary: list[dict[str, Any]] = []
    for seed in DEMO_BOOKINGS:
        identifier = existing.get(seed.email.casefold())
        created = False
        recovered_after_error = False
        if identifier is None:
            try:
                payload = _request(
                    base_url=base_url,
                    api_key=api_key,
                    method="POST",
                    path="bookings/",
                    body=_booking_xml(seed),
                )
                booking = payload.get("booking") if isinstance(payload, dict) else None
                if not isinstance(booking, dict) or not booking.get("id"):
                    raise SeedError(f"QloApps did not confirm creation for {seed.marker}")
                identifier = str(booking["id"])
                created = True
            except SeedError:
                # QloApps 1.7.0 peut persister la reservation puis retourner
                # une erreur. Une relecture empeche un retry de la dupliquer.
                for candidate_id in _booking_ids(base_url, api_key):
                    candidate = _booking(base_url, api_key, candidate_id)
                    candidate_customer = candidate.get("associations", {}).get(
                        "customer_detail", {}
                    )
                    if (
                        str(candidate_customer.get("email") or "").casefold()
                        == seed.email.casefold()
                    ):
                        identifier = candidate_id
                        created = True
                        recovered_after_error = True
                        break
                if identifier is None:
                    raise

        verified = _booking(base_url, api_key, identifier)
        customer = verified.get("associations", {}).get("customer_detail", {})
        if str(customer.get("email") or "").casefold() != seed.email.casefold():
            raise SeedError(f"Read-after-write verification failed for {seed.marker}")
        summary.append(
            {
                "id": identifier,
                "guest": f"{seed.firstname} {seed.lastname}",
                "created": created,
                "recovered_after_error": recovered_after_error,
                "verified": True,
            }
        )
    jean_identifier = next(
        item["id"] for item in summary if item["guest"] == "Jean Dupont"
    )
    jean_booking = _booking(base_url, api_key, str(jean_identifier))
    customer_request = _seed_customer_request(base_url, api_key, jean_booking)
    return summary, customer_request


def main() -> int:
    base_url = os.getenv("QLOAPPS_BASE_URL", "http://127.0.0.1:8088/api")
    api_key = os.getenv("QLOAPPS_SEED_API_KEY", "")
    if len(api_key) != 32 or not api_key.isalnum():
        print("QLOAPPS_SEED_API_KEY is missing or invalid.", file=sys.stderr)
        return 2
    try:
        summary, customer_request = seed_demo(base_url, api_key)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"bookings": summary, "customer_request": customer_request},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
