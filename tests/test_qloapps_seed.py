from pathlib import Path
import runpy


def _seed_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "infra"
        / "qloapps"
        / "seed_demo.py"
    )
    return runpy.run_path(str(path))


def test_seed_payload_contains_qloapps_required_workaround():
    module = _seed_module()
    xml = module["_booking_xml"](module["DEMO_BOOKINGS"][0])

    assert b"<id_property>1</id_property>" in xml
    assert b"<total_tax>0</total_tax>" in xml
    assert b"jean.dupont@agentpms.test" in xml


def test_seed_defines_only_synthetic_reserved_domain_addresses():
    bookings = _seed_module()["DEMO_BOOKINGS"]

    assert len(bookings) == 3
    assert all(item.email.endswith("@agentpms.test") for item in bookings)


def test_seed_customer_request_is_synthetic_and_bounded():
    module = _seed_module()
    booking = {
        "id": 1,
        "associations": {"customer_detail": {"id_customer": 3}},
    }

    thread = module["_customer_thread_xml"](booking)
    message = module["_customer_message_xml"]("12")

    assert b"agentpmsfixt" in thread
    assert b"jean.dupont@agentpms.test" in thread
    assert b"<id_customer_thread>12</id_customer_thread>" in message
    assert b"<id_employee>0</id_employee>" in message
