# Razorpay Payment Integration

A production-grade Razorpay payment integration shipped as a Python library plus a TypeScript checkout widget. Multi-tenant, idempotent, observable, locally testable end-to-end without any cloud dependency.

**Stack:** Python 3 (FastAPI, asyncpg, Alembic, OpenTelemetry) · PostgreSQL · AWS (Lambda, SQS FIFO, KMS, SSM, CDK) · TypeScript (React + vanilla widget) · LocalStack · GitHub Actions (cdk-nag, Checkov, gitleaks)

## Authorship

Designed and architected by [Akhil Joe](https://github.com/akhiljoe): the system design, architecture, invariants, security model, AWS migration plan and every trade-off documented in [`designs/`](designs/) are my decisions.

Implementation was built with [Claude Code](https://claude.com/claude-code) as a pair programmer, working from those designs phase by phase. The agent configuration used for that ([`CLAUDE.md`](CLAUDE.md) and [`.claude/`](.claude/): project rules, review commands, a security-auditor agent) is kept in the repo on purpose, as part of how the project was engineered.

## Status

**16 phases complete, fully tested locally.** Cloud deploy (Phase 17) is documented but not executed — the artifact is shippable as-is for any host that has its own AWS infrastructure.

- 240+ pytest tests + 3 chaos tests pass against Postgres + LocalStack
- 11/11 dashboard scenarios prove every Critical Invariant end-to-end
- 15 vitest tests + 0 npm-audit runtime vulnerabilities for the widget
- 5 operator runbooks + 4 cloud onboarding docs + 13 documented Section 17 learnings

## What's in the box

| Component | Purpose |
|---|---|
| `razorpay_checkout/` | Python package — FastAPI router, async DB, OTel, idempotent consumer + fulfillment processors |
| `packages/checkout-widget/` | TypeScript npm package — React + vanilla adapters around Razorpay's checkout.js |
| `example/backend/` | FastAPI dev backend wiring the whole flow + a 17-step Payment Journey dashboard + a buyer-perspective storefront |
| `scripts/` | Operator CLIs: `local-fixtures`, `local-pump`, `razorpay-simulator`, `dlq-replay` |
| `infra/` | AWS CDK Python (FoundationStack + PaymentsStack) for cloud deploy |
| `docs/` | All operational and planning docs (runbooks, cloud onboarding, diagrams, architecture overview, quickstart, commands, troubleshooting, test/scale/prod-migration plans, phase history, cost analysis, devops tooling setup) |
| `designs/` | Formal design + AWS migration architecture + IAM policies. Source of truth lives here. |

## Where to start, by role

| You are | Read |
|---|---|
| New developer running locally | [docs/QUICKSTART.md](docs/QUICKSTART.md) |
| Looking up "how do I X" | [docs/COMMANDS.md](docs/COMMANDS.md) |
| Debugging a failure | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Wanting the system overview | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| On-call, something broke | [docs/runbooks/](docs/runbooks/) |
| Preparing to deploy to AWS | [designs/MIGRATION_WRITEUP.md](designs/MIGRATION_WRITEUP.md) and [designs/MIGRATION_ARCHITECTURE.md](designs/MIGRATION_ARCHITECTURE.md) |
| Reading the formal spec | [designs/DESIGN_v6.md](designs/DESIGN_v6.md) |
| Trying to understand past decisions | [docs/PHASES.md](docs/PHASES.md) + `git log` |
| Estimating monthly AWS spend | [docs/cost-analysis.md](docs/cost-analysis.md) |
| AI agent working on the codebase | [CLAUDE.md](CLAUDE.md) |

## Architecture in one sentence

A FastAPI router receives Razorpay webhooks, fast-verifies HMAC signatures **without any database access**, publishes onto an SQS FIFO queue; a separate consumer Lambda drains the queue, applies an event-type-aware idempotency guard inside a `SELECT ... FOR UPDATE` transaction, advances the order's state machine, and publishes to a second SQS FIFO queue; a fulfillment Lambda drains that, invokes the host's `FulfillmentHook`, and transitions to terminal status.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full diagram and the 10 Critical Invariants that make this safe.

## Run it locally

```bash
docker compose up -d postgres localstack
cd razorpay_checkout && uv sync --extra dev && uv run alembic upgrade head
```

Three terminals:

```bash
# Terminal A: backend
cd example/backend && uv run --project ../../razorpay_checkout uvicorn main:app --reload --port 8000

# Terminal B: consumer pump
uv run --project razorpay_checkout python scripts/local-pump.py

# Terminal C: open browser
# http://localhost:8000/dev/dashboard   — Mission Control: KPIs + 17-step Payment Journey + 11 scenarios
# http://localhost:8000/admin/dashboard — read-only ops view (gated by X-Admin-Api-Key)
# http://localhost:8000/dev/shop        — buyer-perspective storefront
```

The dev dashboard is gated on `RAZORPAY_DEV_ROUTES_ENABLED=true` (off by
default). The admin dashboard is gated on `X-Admin-Api-Key` matching
`RAZORPAY_ADMIN_API_KEY`. Both are safe to expose in different
environments: dev locally, admin behind a per-operator proxy.

Full step-by-step in [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Key invariants (the load-bearing ones)

1. The **Webhook Producer holds no database credentials.** This is enforced in IAM, not just in code.
2. Every query against `orders`, `payments`, `refunds`, `webhook_events`, `order_events` filters by `tenant_id`. Enforced by `OrderService` and `WebhookConsumer`, never the caller.
3. **Event-type-aware idempotency** inside a `SELECT ... FOR UPDATE` transaction. `event_id ON CONFLICT DO NOTHING` is the fast path; the primary guard runs after the row lock.
4. **DB commit before any side effect.** SQS publishes, logs, external calls all happen after the consumer's transaction commits.
5. **Correlation IDs propagate end to end** across the Producer → SQS → Consumer → SQS → Fulfillment hops via MessageAttributes.

Full list in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Each invariant has at least one test asserting it.

## How to evaluate quickly

If you have 5 minutes:

1. `git log --oneline -10` — see the phase commits.
2. Open `http://localhost:8000/dev/dashboard` (after running the 3 terminals above), click **Run all scenarios in sequence**. Watch 11 invariants prove themselves.

If you have 30 minutes:

1. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
2. Read one runbook of your choice from `docs/runbooks/`.
3. Skim [designs/DESIGN_v6.md](designs/DESIGN_v6.md) Section 17 (the lessons learned).

## Notes on this public version

Account IDs, domains and contact addresses in the docs and `infra/cdk.json` are placeholders (`111111111111`, `example.com`, `Demo Store`). See [infra/README.md](infra/README.md#context-overrides) for supplying real values at deploy time.

## License

[MIT](LICENSE). Bug reports and design discussions are welcome as issues.
