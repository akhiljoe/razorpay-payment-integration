# Local end-to-end test — demo-tenant reserve flow

Everything runs on your machine. No AWS. You will see a real Razorpay test modal, but money never moves.

## Prereqs

- **Docker Desktop** running (for Postgres + LocalStack)
- **Python 3.11+** with **uv** (`pip install uv` if you don't have it)
- Browser

The Razorpay test keys are already in `example/backend/.env` and matched in `demo-tenant/app.js`.

## What you'll spin up

| Terminal | Process | Purpose |
|---|---|---|
| 1 | `docker-compose up` | Postgres + LocalStack (fake SQS) |
| 2 | `uvicorn main:app` | FastAPI backend on :8000 |
| 3 | `python scripts/local-pump.py` | Drains SQS into the consumer (since Lambda doesn't run locally) |
| 4 | `python -m http.server 5500` | Static-serves `demo-tenant/` |

## Run order

### Terminal 1 — Postgres + LocalStack

```bash
cd C:\Users\you\Documents\razorpay-payment-integration
docker-compose up -d
```

Wait ~10 seconds, then verify:
```bash
docker ps
```
You should see one Postgres container and one LocalStack container.

### Terminal 2 — Backend

```bash
cd C:\Users\you\Documents\razorpay-payment-integration\razorpay_checkout
uv sync
uv run alembic upgrade head
cd ..\example\backend
uv run --project ..\..\razorpay_checkout uvicorn main:app --reload --port 8000
```

Leave it running. Once it logs `Application startup complete`, seed the local tenant in **a one-off shell**:
```bash
curl -X POST http://localhost:8000/dev/reset
```
(This truncates and inserts the `local-dev` tenant row. Re-run any time you want a clean slate.)

### Terminal 3 — SQS pump

```bash
cd C:\Users\you\Documents\razorpay-payment-integration
uv run --project razorpay_checkout python scripts/local-pump.py
```

This polls the LocalStack queues and pushes messages into the consumer/fulfillment handlers (in prod these are real Lambda triggers).

### Terminal 4 — Static-serve demo-tenant

```bash
cd C:\Users\you\Documents\demo-tenant
python -m http.server 5500
```

## Drive the flow

1. Open **http://localhost:5500/reserve.html** in your browser.
2. **Pick a date** from the calendar (Fridays/Saturdays/Sundays are open by default; today + 30 days range).
3. Fill in name/guests/phone/email. The total updates as you change guest count.
4. Click **"Send the RSVP"**.
5. Razorpay's real test modal opens. Use:
   - **Card:** `5267 3181 8797 5449`, any name, any future expiry, any CVV
   - **UPI success:** `success@razorpay`
   - **UPI failure:** `failure@razorpay`
6. After "Success" in the modal, you land on **success.html**. It polls `/payments/orders/{id}` every 1.5s.
7. Watch the status walk: `created → attempted → paid → fulfilled` (takes ~3 seconds).

## What to watch while it runs

- **http://localhost:8000/dev/dashboard** — live Mission Control. Shows orders, webhook events, SQS queue depth, state transitions.
- **Terminal 2 logs** — JSON-formatted, one line per webhook event, with `correlation_id` tying the whole chain together.
- **Terminal 3 logs** — shows the pump picking up messages off `webhook-events.fifo` and `fulfillment-queue.fifo`.

## Why does this work without AWS?

| Real prod | Local equivalent |
|---|---|
| Razorpay servers POST your webhook URL | Browser self-fires `/dev/simulate` after success (signed with the local webhook secret) |
| AWS SQS FIFO | LocalStack SQS, same API |
| Lambda runtime triggers the consumer | `scripts/local-pump.py` polls and invokes the handler in-process |
| RDS Postgres | docker-compose Postgres |

The application code is identical. Only the runtime substrate differs.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Razorpay modal: "key/order id mismatch" | `rzp_test_*` in `example/backend/.env` and `demo-tenant/app.js:213` disagree. Re-sync them. |
| CORS preflight fails in browser console | `demo-tenant` isn't being served from port 5500. Use `python -m http.server 5500`, not `file://`. |
| `POST /payments/orders` → 400 `unknown_tenant` | You didn't run `curl -X POST http://localhost:8000/dev/reset`. Seed the tenant row. |
| Order stays in `paid` forever | Terminal 3 (local-pump) isn't running, so fulfillment-queue messages aren't drained. |
| Order stays in `attempted` forever | `/dev/simulate` failed silently — open browser devtools Network tab and check the response. Backend log will have the reason. |
| `docker-compose up` fails on port 5432 | You have local Postgres running. Stop it or change the compose port mapping. |

## Reset between runs

```bash
curl -X POST http://localhost:8000/dev/reset
```

Truncates all order/payment/webhook tables and purges SQS queues. Tenant row is recreated.
