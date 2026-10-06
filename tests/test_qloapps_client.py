import json
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import pytest

from agentpms.pms.qloapps import QloAppsClient


API_KEY = "a" * 32


class RecordingTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, *, method, url, headers, body, timeout):
        call = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(call)
        path = urlsplit(url).path.rstrip("/")
        return self.responses[(method, path)]


def test_client_normalizes_url_and_uses_basic_auth():
    transport = RecordingTransport({("GET", "/api"): ["bookings"]})
    client = QloAppsClient(
        base_url="http://qloapps.local",
        api_key=API_KEY,
        transport=transport,
    )

    assert client.discover_resources() == {"bookings"}
    call = transport.calls[0]
    assert call["url"].startswith("http://qloapps.local/api/")
    assert parse_qs(urlsplit(call["url"]).query) == {"output_format": ["JSON"]}
    assert call["headers"]["Authorization"].startswith("Basic ")
    assert API_KEY not in call["url"]


def test_availability_is_a_bounded_xml_query():
    transport = RecordingTransport(
        {
            ("POST", "/api/hotel_ari"): {
                "hotel_ari": {"id_hotel": "1", "room_types": []}
            }
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    result = client.query_availability(
        hotel_id=1,
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 12),
        room_type_id=3,
    )

    assert result["id_hotel"] == "1"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Content-Type"] == "application/xml"
    assert b"<id_room_type>3</id_room_type>" in call["body"]
    assert b"<get_available_rooms>1</get_available_rooms>" in call["body"]


def test_client_lists_full_services():
    transport = RecordingTransport(
        {
            ("GET", "/api/services"): {
                "services": [{"id": 9, "name": "Breakfast", "price": "350.00"}]
            }
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    assert client.list_services()[0]["id"] == 9
    assert parse_qs(urlsplit(transport.calls[0]["url"]).query) == {
        "display": ["full"],
        "output_format": ["JSON"],
    }


def test_client_lists_hotel_inventory_and_room_bookings():
    transport = RecordingTransport(
        {
            ("GET", "/api/hotel_rooms"): {
                "rooms": [
                    {"id": 7, "id_hotel": 1, "room_num": "DR-102"},
                    {"id": 8, "id_hotel": 2, "room_num": "OTHER"},
                ]
            },
            ("GET", "/api/room_bookings"): {
                "bookings": [
                    {"id": 3, "id_hotel": 1, "id_room": 7},
                    {"id": 4, "id_hotel": 2, "id_room": 8},
                ]
            },
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    assert [room["id"] for room in client.list_hotel_rooms(1)] == [7]
    assert [booking["id"] for booking in client.list_room_bookings(1)] == [3]
    for call in transport.calls:
        query = parse_qs(urlsplit(call["url"]).query)
        assert query["display"] == ["full"]
        assert query["filter[id_hotel]"] == ["[1]"]


def test_client_reads_and_updates_a_room_booking_status():
    room_booking = {
        "id": 42,
        "id_product": 2,
        "id_hotel": 1,
        "id_order": 7,
        "id_order_detail": 8,
        "id_cart": 9,
        "id_room": 7,
        "id_customer": 4,
        "booking_type": 1,
        "id_status": 1,
        "check_in": "0000-00-00 00:00:00",
        "check_out": "0000-00-00 00:00:00",
        "date_from": "2026-09-10 00:00:00",
        "date_to": "2026-09-12 00:00:00",
        "is_cancelled": 0,
    }
    transport = RecordingTransport(
        {
            ("GET", "/api/room_bookings/42"): {"booking": room_booking},
            ("PUT", "/api/room_bookings/42"): {
                "booking": {**room_booking, "id_status": 2}
            },
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    loaded = client.get_room_booking(42)
    updated = client.update_room_booking_status(
        loaded,
        status=2,
        occurred_at=datetime(2026, 9, 10, 15, 30, 45),
    )

    assert updated["id_status"] == 2
    call = transport.calls[1]
    root = ElementTree.fromstring(call["body"])
    assert call["method"] == "PUT"
    assert root.findtext("./booking/id") == "42"
    assert root.findtext("./booking/id_status") == "2"
    assert root.findtext("./booking/check_in") == "2026-09-10 15:30:45"
    assert root.findtext("./booking/check_out") == "0000-00-00 00:00:00"


def test_client_reads_invoice_resources_and_uses_the_bounded_bridge():
    transport = RecordingTransport(
        {
            ("GET", "/api/orders/42"): {"order": {"id": 42}},
            ("GET", "/api/order_invoices"): {
                "order_invoices": [{"id": 3, "id_order": 42, "number": 1003}]
            },
            ("GET", "/api/order_details"): {
                "order_details": [{"id": 8, "id_order": 42}]
            },
            ("POST", "/agentpms-bridge.php"): {
                "invoices": [{"id": 3, "id_order": 42, "number": 1003}]
            },
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    assert client.get_order(42)["id"] == 42
    assert client.list_order_invoices(42)[0]["number"] == 1003
    assert client.list_order_details(42)[0]["id"] == 8
    assert client.issue_invoice(
        order_id=42,
        idempotency_key="request:0:issue_invoice",
    )[0]["id"] == 3

    invoice_query = parse_qs(urlsplit(transport.calls[1]["url"]).query)
    assert invoice_query["filter[id_order]"] == ["[42]"]
    assert transport.calls[3]["url"] == "http://qloapps.local/agentpms-bridge.php"
    assert b'"action": "issue_invoice"' in transport.calls[3]["body"]
    assert b'"order_id": 42' in transport.calls[3]["body"]


def test_client_uses_the_bounded_bridge_for_a_booking_update():
    transport = RecordingTransport(
        {("POST", "/agentpms-bridge.php"): {"result": 1}}
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    result = client.update_booking_via_backoffice(
        reservation_id=42,
        arrival_date=date(2026, 9, 10),
        departure_date=date(2026, 9, 14),
        adults=3,
        children=0,
        idempotency_key="request:0:update_booking",
    )

    assert result["result"] == 1
    body = json.loads(transport.calls[0]["body"])
    assert body == {
        "action": "update_booking",
        "reservation_id": 42,
        "arrival_date": "2026-09-10",
        "departure_date": "2026-09-14",
        "adults": 3,
        "children": 0,
        "idempotency_key": "request:0:update_booking",
    }


def test_client_reads_customer_threads_and_creates_a_bounded_reply():
    transport = RecordingTransport(
        {
            ("GET", "/api/customer_threads"): {
                "customer_threads": [{"id": 12, "subject": "Arrivee tardive"}]
            },
            ("GET", "/api/customer_messages"): {
                "customer_messages": [{"id": 21, "id_customer_thread": 12}]
            },
            ("GET", "/api/customer_messages/21"): {
                "customer_message": {"id": 21, "id_customer_thread": 12}
            },
            ("POST", "/api/customer_messages"): {
                "customer_message": {"id": 22}
            },
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    assert client.list_customer_threads()[0]["id"] == 12
    assert client.get_customer_thread(12)["id"] == 12
    assert client.list_customer_messages(12)[0]["id"] == 21
    assert client.get_customer_message(21)["id"] == 21
    assert client.create_customer_message(
        thread_id=12,
        employee_id=3,
        message="Oui, la reception sera prevenue.",
    )["id"] == 22

    root = ElementTree.fromstring(transport.calls[4]["body"])
    assert root.findtext("./customer_message/id_customer_thread") == "12"
    assert root.findtext("./customer_message/id_employee") == "3"
    assert root.findtext("./customer_message/message") == "Oui, la reception sera prevenue."
    assert root.findtext("./customer_message/private") == "0"


def test_booking_post_serializes_a_bounded_confirmed_creation():
    transport = RecordingTransport(
        {("POST", "/api/bookings"): {"booking": {"id": 51}}}
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    created = client.create_booking(
        operation_marker="AgentPMS-operation",
        hotel_id=1,
        guest_first_name="Pierre",
        guest_last_name="Delafitte",
        email="pierre@example.test",
        phone="0600000000",
        room_type_id=2,
        room_id=7,
        arrival_date=date(2026, 9, 2),
        departure_date=date(2026, 9, 10),
        adults=2,
        children=0,
        room_unit_price_without_tax=Decimal("1250.00"),
        room_tax_rules_group_id=1,
    )

    assert created["id"] == 51
    call = transport.calls[0]
    root = ElementTree.fromstring(call["body"])
    assert call["method"] == "POST"
    assert root.findtext("./booking/source") == "AgentPMS-operation"
    assert root.findtext(".//customer_detail/email") == "pierre@example.test"
    assert root.findtext(".//room_type/checkin_date") == "2026-09-02"
    assert root.findtext(".//room/id_room") == "7"
    assert root.findtext(".//room/adults") == "2"
    assert root.findtext(".//room/unit_price_without_tax") == "1250.00"
    assert root.findtext(".//room/id_tax_rules_group") == "1"
    assert root.find(".//room/total_tax") is None


def test_booking_put_preserves_structure_and_serializes_new_service():
    transport = RecordingTransport(
        {("PUT", "/api/bookings/42"): {"booking": {"id": 42}}}
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )
    booking = {
        "id": 42,
        "id_property": 1,
        "currency": "EUR",
        "associations": {
            "customer_detail": {
                "id_customer": 4,
                "firstname": "Alice",
                "lastname": "Dupont",
                "email": "alice@example.test",
            },
            "room_types": {
                "id_room_type": 2,
                "checkin_date": "2026-09-10",
                "checkout_date": "2026-09-12",
                "rooms": {
                    "id_room": 7,
                    "adults": 2,
                    "child": 0,
                    "services": [
                        {
                            "id_service": 6,
                            "quantity": 1,
                            "total_price_without_tax": "30.00",
                            "total_tax": "0.00",
                        },
                        {
                            "id_service": 9,
                            "quantity": 1,
                            "unit_price_without_tax": "350.00",
                            "_agentpms_new": True,
                        },
                    ],
                },
            },
        },
    }

    assert client.update_booking_services(booking)["id"] == 42
    call = transport.calls[0]
    root = ElementTree.fromstring(call["body"])
    assert call["method"] == "PUT"
    assert root.findtext("./booking/associations/room_types/room_type/number_of_rooms") == "1"
    assert root.findtext(".//room/total_tax") == "0"
    services = root.findall(".//room/services/service")
    assert [node.findtext("id_service") for node in services] == ["6", "9"]
    assert services[0].findtext("total_price_without_tax") == "30.00"
    assert services[1].findtext("unit_price_without_tax") is None


def test_client_resolves_a_uniform_tax_rate_for_the_service_group():
    transport = RecordingTransport(
        {
            ("GET", "/api/tax_rules"): {
                "tax_rules": [
                    {"id": 1, "id_tax_rules_group": 1, "id_tax": 4},
                    {"id": 2, "id_tax_rules_group": 1, "id_tax": 4},
                ]
            },
            ("GET", "/api/taxes/4"): {
                "tax": {"id": 4, "rate": "20.000", "active": 1}
            },
        }
    )
    client = QloAppsClient(
        base_url="http://qloapps.local/api",
        api_key=API_KEY,
        transport=transport,
    )

    assert client.get_uniform_tax_rate(1) == Decimal("20.000")
    query = parse_qs(urlsplit(transport.calls[0]["url"]).query)
    assert query["filter[id_tax_rules_group]"] == ["[1]"]


@pytest.mark.parametrize("api_key", ["", "too-short", "!" * 32])
def test_client_rejects_invalid_keys(api_key):
    with pytest.raises(ValueError):
        QloAppsClient(base_url="http://qloapps.local/api", api_key=api_key)
