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


def test_redirect_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://elsewhere.test/"})

    with make_client(handler) as client, pytest.raises(NessieError) as info:
        client.get_customers()
    assert info.value.status_code == 301


def test_non_json_body_is_an_error_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>oops {KEY}</html>")

    with make_client(handler) as client, pytest.raises(NessieError) as info:
        client.request("GET", "/customers")
    assert KEY not in str(info.value)


def test_empty_body_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with make_client(handler) as client:
        assert client.request("GET", "/customers") is None


def test_get_customers_rejects_non_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 404, "message": "not found"})

    with make_client(handler) as client, pytest.raises(NessieError):
        client.get_customers()


def test_key_straddling_truncation_boundary_is_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 495 + KEY + "y" * 100)

    with make_client(handler) as client, pytest.raises(NessieError) as info:
        client.get_customers()
    assert KEY not in str(info.value)
    assert KEY[:6] not in str(info.value)


def test_plain_http_base_url_is_rejected():
    with pytest.raises(ValueError):
        NessieClient("k", base_url="http://example.com")


def test_redact_key_ignores_params_that_only_end_in_key():
    assert redact_key("monkey=banana") == "monkey=banana"
