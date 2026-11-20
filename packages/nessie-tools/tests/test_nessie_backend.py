"""NessieBackend quirk handling, tested offline with a mock HTTP transport."""

import json
from datetime import date

import httpx
import pytest
from nessie_tools import (
    AccountType,
    AmountError,
    NessieBackend,
    NessieClient,
    NessieError,
    NotFoundError,
)

KEY = "fake-test-key-123"  # gitleaks:allow (fake key, used only in offline tests)


def backend_with(handler) -> NessieBackend:
    http = httpx.Client(base_url="https://nessie.test", transport=httpx.MockTransport(handler))
    return NessieBackend(NessieClient(KEY, http=http))


def test_create_unwraps_objectcreated_and_fills_missing_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # Nessie's create response: wrapped, and without customer_id
            return httpx.Response(
                201,
                json={
                    "code": 201,
                    "objectCreated": {
                        "_id": "acc-1",
                        "type": "Savings",
                        "nickname": "Rainy day",
                        "balance": 500,
                        "account_number": "1234567890125790",
                    },
                },
            )
        raise AssertionError("unexpected request")

    account = backend_with(handler).create_account("cust-1", AccountType.SAVINGS, "Rainy day", 500)
    assert account.id == "acc-1"
    assert account.customer_id == "cust-1"  # filled in from what we know
    assert account.last4 == "5790"


def test_create_reads_back_when_account_number_missing():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"objectCreated": {"_id": "acc-2"}})
        return httpx.Response(
            200,
            json={
                "_id": "acc-2",
                "type": "Checking",
                "nickname": "Main",
                "balance": 0,
                "account_number": "0000111122223333",
                "customer_id": "cust-1",
            },
        )

    account = backend_with(handler).create_account("cust-1", AccountType.CHECKING, "Main", 0)
    assert calls == ["POST", "GET"]
    assert account.account_number == "0000111122223333"


def test_purchase_payload_uses_server_field_names():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"objectCreated": {"_id": "p-1"}})

    p = backend_with(handler).create_purchase("acc", "m-1", 12, date(2026, 11, 8), "latte")
    assert sent["purchase_date"] == "2026-11-08"  # not transaction_date (docs are wrong)
    assert sent["status"] == "completed"
    assert "transaction_date" not in sent
    assert p.amount == 12 and p.purchase_date == date(2026, 11, 8)


def test_empty_list_404_quirk_becomes_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json="No deposits found for this account")

    assert backend_with(handler).list_deposits("acc") == []


def test_other_404_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Account not found"})

    backend = backend_with(handler)
    with pytest.raises(NotFoundError):
        backend.list_deposits("missing")
    with pytest.raises(NotFoundError):
        backend.get_account("missing")


def test_non_list_response_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": True})

    with pytest.raises(NessieError):
        backend_with(handler).list_purchases("acc")


def test_amount_gate_runs_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for an invalid amount")

    with pytest.raises(AmountError):
        backend_with(handler).create_withdrawal("acc", 4.75, date(2026, 11, 1), "x")


def test_injected_http_client_is_not_closed_by_nessie_client():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with NessieClient(KEY, http=http):
        pass
    assert not http.is_closed  # caller owns it (F-23)
    http.close()


def test_own_http_client_is_closed():
    client = NessieClient(KEY)
    client.close()
    assert client._http.is_closed


def test_merchant_category_list_from_old_data_is_tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"_id": "m-1", "name": "Old", "category": ["food"]}])

    assert backend_with(handler).list_merchants()[0].category == "food"
