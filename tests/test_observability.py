from agentpms.observability.tracing import TraceEvent


def test_trace_payload_redacts_personal_and_secret_values():
    event = TraceEvent(
        event_type="tool_finished",
        request_id="request",
        tool_name="search_reservations",
        fields={
            "guest_name": "Jean Dupont",
            "api_key": "secret",
            "reservation_id": "R-4587",
        },
    )

    payload = event.safe_payload()

    assert payload["fields"]["guest_name"] == "<redacted>"
    assert payload["fields"]["api_key"] == "<redacted>"
    assert payload["fields"]["reservation_id"] == "R-4587"
    assert payload["tool_name"] == "search_reservations"
