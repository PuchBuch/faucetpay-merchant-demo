"""FaucetPay Merchant helpers.

Flow (https://faucetpay.io/api-docs, section "Merchant"):
1. Browser POSTs a plain HTML form to CHECKOUT_URL.
2. Buyer pays on the FaucetPay hosted page.
3. FaucetPay POSTs a callback (IPN) with a one-time `token` to our callback_url.
4. We call GET VERIFY_URL/{token}. ONLY this answer is trusted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

CHECKOUT_URL = "https://faucetpay.io/merchant/webscr"
VERIFY_URL = "https://faucetpay.io/merchant/get-payment"


class VerificationUnavailable(RuntimeError):
    """FaucetPay could not be reached - the callback should be retried later."""


@dataclass(frozen=True)
class VerifiedPayment:
    valid: bool
    transaction_id: str
    merchant_username: str
    payer_username: str
    amount1: Decimal
    currency1: str
    amount2: Decimal
    currency2: str
    custom: str


TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _dec(v) -> Decimal:
    """Parse an amount; anything non-numeric (incl. NaN/Infinity) becomes -1 and fails the price check."""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError):
        return Decimal("-1")
    return d if d.is_finite() else Decimal("-1")


def checkout_fields(*, merchant_username: str, order_id: int, description: str, amount: Decimal,
                    currency: str, base_url: str, pay_currency: str | None = None) -> dict[str, str]:
    """Hidden fields for the HTML form that is POSTed to CHECKOUT_URL."""
    base = base_url.rstrip("/")
    return {
        "merchant_username": merchant_username,
        "item_description": description,
        "amount1": f"{amount}",
        "currency1": currency,
        "currency2": pay_currency or "",  # empty = buyer chooses the coin
        "custom": str(order_id),
        "callback_url": f"{base}/faucetpay/callback",
        "success_url": f"{base}/orders/{order_id}",
        "cancel_url": f"{base}/orders/{order_id}?cancelled=1",
    }


def verify_token(token: str, session: requests.Session | None = None, timeout: float = 15.0) -> VerifiedPayment:
    """Ask FaucetPay whether the token belongs to a real, completed payment."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ValueError("bad token")
    s = session or requests.Session()
    try:
        r = s.get(f"{VERIFY_URL}/{token}", timeout=timeout)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise VerificationUnavailable(str(e)) from e
    if not isinstance(d, dict):
        raise VerificationUnavailable(f"unexpected response: {type(d).__name__}")
    return VerifiedPayment(
        valid=bool(d.get("valid")) and str(d.get("valid")).lower() not in ("false", "0"),
        transaction_id=str(d.get("transaction_id", "")),
        merchant_username=str(d.get("merchant_username", "")),
        payer_username=str(d.get("payer_username", "")),
        amount1=_dec(d.get("amount1")),
        currency1=str(d.get("currency1", "")).upper(),
        amount2=_dec(d.get("amount2")),
        currency2=str(d.get("currency2", "")).upper(),
        custom=str(d.get("custom", "")),
    )
