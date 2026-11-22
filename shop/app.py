"""Demo shop that accepts crypto via FaucetPay Merchant.

Run:  flask --app shop.app run
"""
from __future__ import annotations

import hmac
import logging
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from flask import Flask, Response, abort, g, render_template, request

from . import faucetpay

log = logging.getLogger("shop")

# Demo catalogue. Prices are in currency1 (what you want to receive).
PRODUCTS = {
    "coffee": {"name": "Buy me a coffee", "price": Decimal("1.00"), "currency": "USDT"},
    "ebook": {"name": "PDF cheat-sheet (demo)", "price": Decimal("3.50"), "currency": "USDT"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product TEXT NOT NULL,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',   -- created | paid (a rejected payment leaves a note)
  transaction_id TEXT UNIQUE,
  payer TEXT, paid_amount TEXT, paid_currency TEXT,
  created_at TEXT NOT NULL, paid_at TEXT, note TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MERCHANT_USERNAME=os.environ.get("FAUCETPAY_MERCHANT_USERNAME", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000"),
        DATABASE=os.environ.get("SHOP_DB", "shop.db"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        VERIFY=faucetpay.verify_token,  # replaceable in tests
    )
    if config:
        app.config.update(config)

    def db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.executescript(SCHEMA)
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.get("/")
    def index():
        return render_template("index.html", products=PRODUCTS,
                               configured=bool(app.config["MERCHANT_USERNAME"]))

    @app.post("/buy/<product_id>")
    def buy(product_id):
        p = PRODUCTS.get(product_id) or abort(404)
        if not app.config["MERCHANT_USERNAME"]:
            abort(503, "FAUCETPAY_MERCHANT_USERNAME is not set")
        cur = db().execute("INSERT INTO orders(product, amount, currency, created_at) VALUES (?,?,?,?)",
                           (product_id, str(p["price"]), p["currency"], now()))
        db().commit()
        fields = faucetpay.checkout_fields(
            merchant_username=app.config["MERCHANT_USERNAME"], order_id=cur.lastrowid,
            description=p["name"], amount=p["price"], currency=p["currency"],
            base_url=app.config["PUBLIC_BASE_URL"])
        return render_template("redirect.html", action=faucetpay.CHECKOUT_URL, fields=fields)

    @app.post("/faucetpay/callback")
    def callback():
        token = request.form.get("token", "")
        try:
            pay = app.config["VERIFY"](token)
        except ValueError:
            return "bad token", 400
        except faucetpay.VerificationUnavailable as e:
            log.warning("verification unavailable: %s", e)
            return "retry later", 503  # FaucetPay will retry the callback

        def reject(order_id, reason):
            log.warning("payment rejected (order %s): %s", order_id, reason)
            if order_id:
                db().execute("UPDATE orders SET note=? WHERE id=? AND status='created'", (reason, order_id))
                db().commit()
            return "ok", 200  # acknowledge, do not make FaucetPay retry a bad payment

        if not pay.valid:
            return reject(None, "token not valid")
        merchant = app.config["MERCHANT_USERNAME"]
        if not merchant or pay.merchant_username.lower() != merchant.lower():
            return reject(None, "wrong merchant")
        if not pay.transaction_id:
            return reject(None, "no transaction id")
        if not pay.custom.isdigit():
            return reject(None, "no order id")
        order = db().execute("SELECT * FROM orders WHERE id=?", (int(pay.custom),)).fetchone()
        if order is None:
            return reject(None, f"unknown order {pay.custom}")
        if order["status"] == "paid":
            return "ok", 200  # duplicate callback - already processed
        if pay.currency1 != order["currency"] or pay.amount1 < Decimal(order["amount"]):
            return reject(order["id"], f"amount mismatch {pay.amount1} {pay.currency1}")
        try:
            db().execute(
                "UPDATE orders SET status='paid', transaction_id=?, payer=?, paid_amount=?, paid_currency=?,"
                " paid_at=?, note=NULL WHERE id=? AND status='created'",
                (pay.transaction_id, pay.payer_username, str(pay.amount2), pay.currency2, now(), order["id"]))
            db().commit()
        except sqlite3.IntegrityError:
            return reject(order["id"], f"transaction {pay.transaction_id} already used")
        log.info("order %s paid by %s", order["id"], pay.payer_username)
        return "ok", 200

    @app.get("/orders/<int:order_id>")
    def order_status(order_id):
        # Never trust this redirect: status comes only from the verified callback.
        order = db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone() or abort(404)
        return render_template("order.html", order=order, product=PRODUCTS.get(order["product"]),
                               cancelled=request.args.get("cancelled") == "1")

    @app.get("/admin/orders")
    def admin_orders():
        # Basic-Auth with any username; the page does not exist unless ADMIN_PASSWORD is set.
        # Only safe behind HTTPS - Basic-Auth sends the password with every request.
        password = app.config["ADMIN_PASSWORD"]
        if not password:
            abort(404)
        auth = request.authorization
        if auth is None or auth.type != "basic" or not hmac.compare_digest(
                (auth.password or "").encode(), password.encode()):
            return Response("login required", 401, {"WWW-Authenticate": 'Basic realm="shop admin"'})
        rows = db().execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall()
        return render_template("admin.html", rows=rows)

    return app


app = create_app()
