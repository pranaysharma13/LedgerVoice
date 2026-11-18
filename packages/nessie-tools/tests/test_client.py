import httpx
import pytest
from nessie_tools import NessieClient, NessieError, redact_key

KEY = "abc123secretKEY"  # gitleaks:allow (fake key, used only to test redaction)


def make_client(handler) -> NessieClient:
    http = httpx.Client(base_url="http://nessie.test", transport=httpx.MockTransport(handler))
    return NessieClient(KEY, http=http)


def test_key_is_sent_as_query_param():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.url.params.get("key")
        return httpx.Response(200, json=[{"_id": "c1"}])

    with make_client(handler) as client:
        assert client.get_customers() == [{"_id": "c1"}]
    assert seen["key"] == KEY


def test_http_error_message_never_contains_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {KEY}")

    with make_client(handler) as client, pytest.raises(NessieError) as info:
        client.get_customers()
    assert KEY not in str(info.value)
    assert info.value.status_code == 401
    assert KEY not in (info.value.body or "")


def test_transport_error_is_wrapped_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with make_client(handler) as client, pytest.raises(NessieError) as info:
        client.get_customers()
    assert KEY not in str(info.value)


def test_redact_key_handles_param_and_literal():
    assert redact_key("http://x/customers?key=zzz&a=1") == "http://x/customers?key=***&a=1"
    assert redact_key("token zzz leaked", "zzz") == "token *** leaked"
