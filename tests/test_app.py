"""Tests run fully offline: VERIFY is replaced via app config, verify_token gets a fake session."""
from __future__ import annotations

import base64
import re
import sqlite3
from decimal import Decimal

import pytest
import requests

from shop import faucetpay
from shop.app import create_app

MERCHANT = "demo_merchant"


def payment(**overrides) -> faucetpay.VerifiedPayment:
    data = dict(valid=True, transaction_id="tx-1", merchant_username=MERCHANT, payer_username="alice",
                amount1=Decimal("1.00"), currency1="USDT", amount2=Decimal("0.00001"), currency2="BTC",
                custom="1")
    data.update(overrides)
    return faucetpay.VerifiedPayment(**data)


class FakeVerify:
    """Stand-in for faucetpay.verify_token: token -> VerifiedPayment or exception."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.calls: list[str] = []

    def __call__(self, token):
        self.calls.append(token)
        if not token or not faucetpay.TOKEN_RE.fullmatch(token):
            raise ValueError("bad token")
        answer = self.answers[token]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def verify():
    return FakeVerify()


@pytest.fixture
def app(tmp_path, verify):
    return create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "MERCHANT_USERNAME": MERCHANT,
        "PUBLIC_BASE_URL": "https://shop.example/",
        "ADMIN_PASSWORD": "",
        "VERIFY": verify,
    })


@pytest.fixture
def client(app):
    return app.test_client()


def order_row(app, order_id):
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    finally:
        conn.close()


def buy(client, product="coffee") -> int:
    r = client.post(f"/buy/{product}")
    assert r.status_code == 200
    return int(re.search(r'name="custom" value="(\d+)"', r.get_data(as_text=True)).group(1))


def callback(client, token="tok1"):
    return client.post("/faucetpay/callback", data={"token": token, "custom": "999", "amount1": "1000"})


# --- pages -----------------------------------------------------------------

def test_index_lists_products(client):
    html = client.get("/").get_data(as_text=True)
    assert "Buy me a coffee" in html and "Not configured" not in html


def test_index_warns_when_not_configured(app, client):
    app.config["MERCHANT_USERNAME"] = ""
    html = client.get("/").get_data(as_text=True)
    assert "Not configured" in html
    assert client.post("/buy/coffee").status_code == 503


def test_buy_creates_order_and_renders_checkout_form(app, client):
    r = client.post("/buy/coffee")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert f'action="{faucetpay.CHECKOUT_URL}"' in html
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html))
    assert fields == {
        "merchant_username": MERCHANT,
        "item_description": "Buy me a coffee",
        "amount1": "1.00",
        "currency1": "USDT",
        "currency2": "",
        "custom": "1",
        "callback_url": "https://shop.example/faucetpay/callback",
        "success_url": "https://shop.example/orders/1",
        "cancel_url": "https://shop.example/orders/1?cancelled=1",
    }
    row = order_row(app, 1)
    assert (row["product"], row["amount"], row["currency"], row["status"]) == ("coffee", "1.00", "USDT", "created")


def test_buy_unknown_product_404(client):
    assert client.post("/buy/nope").status_code == 404


def test_order_page_does_not_change_status(app, client):
    oid = buy(client)
    r = client.get(f"/orders/{oid}")  # what success_url points to
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Waiting for payment confirmation" in html and 'http-equiv="refresh"' in html
    assert order_row(app, oid)["status"] == "created"
    html = client.get(f"/orders/{oid}?cancelled=1").get_data(as_text=True)
    assert "Payment cancelled" in html and 'http-equiv="refresh"' not in html
    assert order_row(app, oid)["status"] == "created"


def test_order_page_unknown_404(client):
    assert client.get("/orders/42").status_code == 404


# --- callback ---------------------------------------------------------------

def test_valid_callback_marks_paid_and_is_idempotent(app, client, verify):
    oid = buy(client)
    verify.answers["tok1"] = payment(custom=str(oid))
    assert callback(client).status_code == 200
    row = order_row(app, oid)
    assert row["status"] == "paid"
    assert (row["transaction_id"], row["payer"], row["paid_amount"], row["paid_currency"]) == \
        ("tx-1", "alice", "0.00001", "BTC")
    assert verify.calls == ["tok1"]

    # FaucetPay retries the same callback: still 200, nothing changes
    paid_at = row["paid_at"]
    assert callback(client).status_code == 200
    assert order_row(app, oid)["paid_at"] == paid_at
    html = client.get(f"/orders/{oid}").get_data(as_text=True)
    assert "Paid." in html and 'http-equiv="refresh"' not in html


@pytest.mark.parametrize("overrides", [
    {"valid": False},
    {"merchant_username": "someone_else"},
    {"amount1": Decimal("0.99")},
    {"amount1": Decimal("-1")},  # what _dec returns for garbage
    {"currency1": "TRX"},
    {"transaction_id": ""},
], ids=["invalid", "wrong-merchant", "underpaid", "bad-amount", "other-currency", "no-tx"])
def test_bad_payment_is_acknowledged_but_not_paid(app, client, verify, overrides):
    oid = buy(client)
    verify.answers["tok1"] = payment(custom=str(oid), **overrides)
    assert callback(client).status_code == 200
    assert order_row(app, oid)["status"] == "created"


def test_mismatch_leaves_note(app, client, verify):
    oid = buy(client)
    verify.answers["tok1"] = payment(custom=str(oid), amount1=Decimal("0.50"))
    callback(client)
    assert "amount mismatch" in order_row(app, oid)["note"]


@pytest.mark.parametrize("custom", ["999", "", "abc"])
def test_unknown_order_is_acknowledged(app, client, verify, custom):
    oid = buy(client)
    verify.answers["tok1"] = payment(custom=custom)
    assert callback(client).status_code == 200
    assert order_row(app, oid)["status"] == "created"


def test_merchant_must_be_configured(app, client, verify):
    oid = buy(client)
    app.config["MERCHANT_USERNAME"] = ""
    verify.answers["tok1"] = payment(custom=str(oid), merchant_username="")
    assert callback(client).status_code == 200
    assert order_row(app, oid)["status"] == "created"


def test_transaction_id_cannot_pay_two_orders(app, client, verify):
    first, second = buy(client), buy(client)
    verify.answers["tok1"] = payment(custom=str(first), transaction_id="tx-same")
    verify.answers["tok2"] = payment(custom=str(second), transaction_id="tx-same")
    assert callback(client, "tok1").status_code == 200
    assert callback(client, "tok2").status_code == 200
    assert order_row(app, first)["status"] == "paid"
    row = order_row(app, second)
    assert row["status"] == "created" and "already used" in row["note"]


def test_verification_unavailable_returns_503(app, client, verify):
    oid = buy(client)
    verify.answers["tok1"] = faucetpay.VerificationUnavailable("timeout")
    assert callback(client).status_code == 503
    assert order_row(app, oid)["status"] == "created"


@pytest.mark.parametrize("token", ["", "../etc", "a/b", "x" * 200, "tok en"])
def test_bad_token_returns_400(client, verify, token):
    assert callback(client, token).status_code == 400


def test_callback_without_token_returns_400(client):
    assert client.post("/faucetpay/callback", data={}).status_code == 400


# --- admin ------------------------------------------------------------------

def basic(password, user="admin"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_admin_hidden_without_password(client):
    assert client.get("/admin/orders").status_code == 404


def test_admin_requires_basic_auth(app, client):
    app.config["ADMIN_PASSWORD"] = "s3cret"
    buy(client)
    r = client.get("/admin/orders")
    assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
    assert client.get("/admin/orders", headers=basic("wrong")).status_code == 401
    r = client.get("/admin/orders", headers=basic("s3cret"))
    assert r.status_code == 200 and "coffee" in r.get_data(as_text=True)


# --- verify_token -----------------------------------------------------------

class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class FakeSession:
    def __init__(self, result):
        self.result, self.urls = result, []

    def get(self, url, timeout):
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


GOOD = {"valid": True, "transaction_id": 123, "merchant_username": MERCHANT, "payer_username": "alice",
        "amount1": "1.00", "currency1": "usdt", "amount2": "0.00001", "currency2": "btc", "custom": "7"}


def test_verify_token_parses_response():
    s = FakeSession(FakeResponse(GOOD))
    p = faucetpay.verify_token("abc123", session=s)
    assert s.urls == [f"{faucetpay.VERIFY_URL}/abc123"]
    assert p == faucetpay.VerifiedPayment(True, "123", MERCHANT, "alice", Decimal("1.00"), "USDT",
                                          Decimal("0.00001"), "BTC", "7")


@pytest.mark.parametrize("valid", ["false", "False", 0, "0", False, None, ""])
def test_verify_token_not_valid(valid):
    p = faucetpay.verify_token("abc", session=FakeSession(FakeResponse({**GOOD, "valid": valid})))
    assert p.valid is False


@pytest.mark.parametrize("amount", ["abc", None, "NaN", "Infinity", ""])
def test_verify_token_non_numeric_amount(amount):
    p = faucetpay.verify_token("abc", session=FakeSession(FakeResponse({**GOOD, "amount1": amount})))
    assert p.amount1 == Decimal("-1")


@pytest.mark.parametrize("result", [
    requests.ConnectionError("down"),
    requests.Timeout("slow"),
    FakeResponse(GOOD, status=502),
    FakeResponse(ValueError("not json")),
    FakeResponse(["not", "a", "dict"]),
], ids=["connection", "timeout", "http-502", "not-json", "not-dict"])
def test_verify_token_unavailable(result):
    with pytest.raises(faucetpay.VerificationUnavailable):
        faucetpay.verify_token("abc", session=FakeSession(result))


@pytest.mark.parametrize("token", ["", "a/b", "../x", "x?y=1", None])
def test_verify_token_rejects_bad_token_without_request(token):
    s = FakeSession(FakeResponse(GOOD))
    with pytest.raises(ValueError):
        faucetpay.verify_token(token, session=s)
    assert s.urls == []
