"""NessieBackend quirk handling, tested offline with a mock HTTP transport."""

import json
from datetime import date

import httpx
import pytest
from nessie_tools import (
    Account,
    AccountType,
    AmountError,
    BillStatus,
    NessieBackend,
    NessieClient,
    NessieDataError,
    NessieError,
    NotFoundError,
    compute_balance,
    is_ambiguous,
    is_temporary,
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

    with pytest.raises(NessieDataError):
        backend_with(handler).list_purchases("acc")


def test_amount_gate_runs_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for an invalid amount")

    with pytest.raises(AmountError):
        # deliberately the wrong type: the gate must catch it at runtime too
        backend_with(handler).create_withdrawal("acc", 4.75, date(2026, 11, 1), "x")  # type: ignore[arg-type]


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


def test_create_on_unknown_parent_is_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Account not found"})

    with pytest.raises(NotFoundError):
        backend_with(handler).create_deposit("gone", 5, date(2026, 11, 1), "x")


def test_unreadable_create_response_is_ambiguous_nessie_error():
    """R1-04: the server accepted the write; a bad response must not escape as pydantic."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"objectCreated": {"_id": "d-1", "amount": "lots"}})

    with pytest.raises(NessieDataError) as info:
        backend_with(handler).create_deposit("acc", 5, date(2026, 11, 1), "x")
    assert is_ambiguous(info.value)  # the write was probably accepted: check, don't redo
    assert not is_temporary(info.value)  # D41: retrying gives the same bad data


def test_bad_shared_merchants_are_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"_id": "m-1", "name": None}, {"_id": "m-2", "name": "Good"}]
        )

    assert [m.name for m in backend_with(handler).list_merchants()] == ["Good"]


def test_bad_item_on_our_account_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"_id": "p-1", "amount": "lots"}])

    with pytest.raises(NessieDataError):
        backend_with(handler).list_purchases("acc")


def test_account_kept_when_read_back_fails():
    """R1-19: never lose the id of an account that was created."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"objectCreated": {"_id": "acc-9"}})
        return httpx.Response(503, text="busy")

    account = backend_with(handler).create_account("cust-1", AccountType.CHECKING, "Main", 0)
    assert account.id == "acc-9"
    assert account.account_number == ""


def test_create_without_an_id_is_ambiguous_but_not_temporary():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"message": "Created"})

    with pytest.raises(NessieDataError) as info:
        backend_with(handler).create_withdrawal("acc", 5, date(2026, 11, 1), "x")
    assert is_ambiguous(info.value)
    assert not is_temporary(info.value)


def test_non_json_body_is_a_data_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway hiccup</html>")

    with pytest.raises(NessieDataError) as info:
        backend_with(handler).list_deposits("acc")
    assert not is_temporary(info.value)


def test_connection_failure_is_temporary_not_a_data_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(NessieError) as info:
        backend_with(handler).list_deposits("acc")
    assert not isinstance(info.value, NessieDataError)
    assert is_temporary(info.value)


def test_malformed_purchase_makes_the_balance_unreadable_permanently():
    """V2 finding 5: the exact repro. Retrying would read the same bad record forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/purchases"):
            return httpx.Response(200, json=[{"_id": "p-1", "amount": "lots"}])
        return httpx.Response(200, json=[])

    backend = backend_with(handler)
    account = Account.model_validate(
        {"_id": "acc", "type": "Checking", "nickname": "c", "balance": 100, "customer_id": "cu"}
    )
    with pytest.raises(NessieDataError) as info:
        compute_balance(backend, "acc", account=account)
    assert info.value.status_code is None  # looks like a network error by status alone...
    assert not is_temporary(info.value)  # ...but is classified as permanent


def test_create_reads_back_when_account_number_is_empty():
    """V1-11: an empty string is as useless as a missing value; read the account back."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                201, json={"objectCreated": {"_id": "acc-3", "account_number": ""}}
            )
        return httpx.Response(
            200,
            json={
                "_id": "acc-3",
                "type": "Checking",
                "nickname": "c",
                "balance": 0,
                "customer_id": "cust-1",
                "account_number": "1111222233334444",
            },
        )

    account = backend_with(handler).create_account("cust-1", AccountType.CHECKING, "c", 0)
    assert calls == ["POST", "GET"]
    assert account.last4 == "4444"


def test_bill_status_is_sent_as_its_plain_value():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"objectCreated": {"_id": "b-1", **sent}})

    bill = backend_with(handler).create_bill(
        "acc", "Power", "power", 10, date(2026, 11, 1), 1, status=BillStatus.RECURRING
    )
    assert sent["status"] == "recurring"
    assert bill.status is BillStatus.RECURRING


@pytest.mark.parametrize("day", [None, 0, 32, True, "5"])
def test_bill_without_a_valid_recurring_day_sends_nothing(day):
    """D49: Nessie would accept it and then fail every bill list on the account."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a bill without a recurring day")

    with pytest.raises(ValueError, match="recurring_date"):
        backend_with(handler).create_bill("acc", "Power", "power", 10, date(2026, 11, 1), day)


def test_bill_payload_always_carries_the_recurring_day():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"objectCreated": {"_id": "b-2", **sent}})

    backend_with(handler).create_bill("acc", "Power", "power", 10, date(2026, 11, 1), 31)
    assert sent["recurring_date"] == 31
