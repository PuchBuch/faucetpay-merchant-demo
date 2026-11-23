# faucetpay-merchant-demo

A small Flask shop that accepts crypto payments through
[FaucetPay Merchant](https://faucetpay.io/api-docs). It shows how to integrate a
hosted crypto checkout **safely**: every payment is verified server-side with
FaucetPay before an order is marked paid.

- Two demo products, SQLite order storage
- Hosted checkout: the buyer picks the coin on FaucetPay's page
- Callback (IPN) handler with token verification, idempotency and amount/currency/merchant checks
- Order status page that refreshes itself while waiting for confirmation
- `/admin/orders` table behind Basic-Auth
- Offline test suite (no real requests to FaucetPay)

## How the payment flow works

```
 Buyer's browser              Shop (this app)                     FaucetPay
 ───────────────              ───────────────                     ─────────
 POST /buy/coffee  ─────────▶ create order #7 (status=created)
                   ◀───────── auto-submitting HTML form
 POST /merchant/webscr ─────────────────────────────────────────▶ checkout page
 (merchant_username, amount1, currency1, custom=7, callback_url…)  buyer pays
                                                                        │
                              POST /faucetpay/callback  ◀───────────────┘
                              (token, custom, amounts…)
                                        │
                              GET /merchant/get-payment/{token} ─────▶ ┐
                              ◀──────────── valid, transaction_id,      │
                                            merchant, amount1, …  ◀────┘
                              checks pass → order #7 = paid
                              answer HTTP 200 (else FaucetPay retries)
 redirect to success_url
 GET /orders/7  ────────────▶ shows status from DB (never trusts the redirect)
```

FaucetPay retries a callback that does not get HTTP 200 (immediately, then after
5, 15, 30, 60, 120 and 240 minutes). The shop answers **503** only when FaucetPay
cannot be reached for verification, so that the callback is retried. A payment
that fails the checks gets **200**, so a bad payment is not retried.

## Before you start

- **Merchant access must be enabled.** Apply for a merchant account in your FaucetPay
  dashboard (<https://faucetpay.io/settings/merchant>). Payments arrive under your FaucetPay username.
- **Buyers pay from their FaucetPay balance**, not directly from an external wallet, so every
  buyer needs a FaucetPay account.

## Installation

Requires Python 3.11+.

```powershell
git clone https://github.com/PuchBuch/faucetpay-merchant-demo.git
cd faucetpay-merchant-demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Linux/macOS: source .venv/bin/activate
pip install -e ".[test]"
copy .env.example .env              # Linux/macOS: cp .env.example .env
```

Edit `.env`:

| Variable | Meaning |
|---|---|
| `FAUCETPAY_MERCHANT_USERNAME` | Your FaucetPay username (receives the coins). Checkout is disabled while it is empty. |
| `PUBLIC_BASE_URL` | Public URL of the shop; used for `callback_url`, `success_url`, `cancel_url`. |
| `SECRET_KEY` | Flask secret key. |
| `SHOP_DB` | SQLite file, default `shop.db`. |
| `ADMIN_PASSWORD` | Password for `/admin/orders` (Basic-Auth, any username). Empty = page returns 404. |

## Run locally

```powershell
flask --app shop.app run
```

Open <http://127.0.0.1:5000>. `flask run` reads `.env` automatically (via `python-dotenv`).

On `localhost` you can see the checkout form and order pages, but FaucetPay cannot
reach your callback, so orders stay `created`.

## Testing the callback with a public URL

FaucetPay must be able to POST to `PUBLIC_BASE_URL/faucetpay/callback`. For a quick
test you can expose the local server through a tunnel, for example:

- **ngrok:** `ngrok http 5000` gives you an `https://….ngrok-free.app` URL.
- **Cloudflare Tunnel:** `cloudflared tunnel --url http://localhost:5000` gives you an
  `https://….trycloudflare.com` URL.

Put that URL into `PUBLIC_BASE_URL`, restart the app, and make a small real payment from
another FaucetPay user's account (merchant access must already be approved).
Watch the app log and `/admin/orders`. Stop the tunnel afterwards: while it runs,
your local server is reachable from the internet.

## Running the tests

```powershell
pytest
ruff check .
```

The tests replace the verification function via `app.config["VERIFY"]` and give
`verify_token()` a fake HTTP session, so no network access is needed.

## Security notes

- **Verify by token only.** The callback body can be forged by anyone. The shop
  ignores its amounts and order id and uses only the answer from
  `GET https://faucetpay.io/merchant/get-payment/{token}`.
- **Check everything in that answer:** `valid` must be true, `merchant_username`
  must be yours, `custom` must be an existing order, `currency1` must match and
  `amount1` must be at least the order price. Non-numeric amounts are rejected.
- **Idempotency.** A repeated callback for a paid order is answered with 200 and
  changes nothing. `transaction_id` is `UNIQUE` in the database, so one payment
  cannot mark two orders as paid.
- **Never trust `success_url`.** The buyer can open `/orders/<id>` without paying.
  The page only shows the status stored by the verified callback.
- **Tokens are validated** (`[A-Za-z0-9_-]`, max 128 characters) before they are put
  into the verification URL.
- **No secrets in code.** All configuration comes from environment variables / `.env`,
  which is git-ignored.

## Limitations

This is a **demo**, not a production shop:

- No HTTPS of its own: use a reverse proxy with TLS. Basic-Auth over plain HTTP sends the password in clear text.
- No user accounts, carts, stock, refunds, emails or delivery of goods.
- Basic-Auth admin only; no CSRF protection, rate limiting or audit log.
- SQLite with the Flask development server; use a real WSGI server and database for real traffic.
- Underpaid payments are only logged and noted on the order; there is no refund flow.

## Related project

[faucetpay-ledger](https://github.com/PuchBuch/faucetpay-ledger) is a read-only
CLI that records your FaucetPay balances in EUR, builds monthly reports and exports payouts to CSV.

## FaucetPay account

Need a FaucetPay account to try this? Sign up here:
<https://faucetpay.io/?r=44490>

*This is a referral link: if you sign up through it, I may get a small reward from FaucetPay. It costs you nothing.*

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by FaucetPay**.
"FaucetPay" is used only to describe the API this code talks to. Use at your own
risk. Crypto payments are irreversible, so test with small amounts.

## License

[MIT](LICENSE) © 2026 PuchBuch
