# Documentation index

Everything under `docs/` plus pointers to the design folder and the repo root.

## By question

| I want to | Read |
|---|---|
| Run the system locally for the first time | [QUICKSTART.md](QUICKSTART.md) |
| Look up "how do I do X right now" | [COMMANDS.md](COMMANDS.md) |
| Debug an error or unexpected behavior | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Understand the architecture in 10 minutes | [ARCHITECTURE.md](ARCHITECTURE.md) |
| See a picture instead of prose | [diagrams/](diagrams/) |
| Read the formal design spec | [../designs/DESIGN_v6.md](../designs/DESIGN_v6.md) |
| Respond to an incident as on-call | [runbooks/](runbooks/) |
| Deploy to AWS (plan + rationale) | [../designs/MIGRATION_WRITEUP.md](../designs/MIGRATION_WRITEUP.md) |
| Deploy to AWS (prescriptive blueprint) | [../designs/MIGRATION_ARCHITECTURE.md](../designs/MIGRATION_ARCHITECTURE.md) |
| Estimate the monthly AWS bill | [cost-analysis.md](cost-analysis.md) |
| Wire CI safety nets and dependency automation | [devops-tooling-setup.md](devops-tooling-setup.md) |
| Know what each phase shipped + why | [PHASES.md](PHASES.md) |
| Walk through the forward-looking phase plan | [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) |
| Plan a prod cutover | [PROD_MIGRATION.md](PROD_MIGRATION.md) |
| Browse the test suites | [`razorpay_checkout/tests/`](../razorpay_checkout/tests/) (pytest) and [`checkout-widget/src/__tests__/`](../packages/checkout-widget/src/__tests__/) (vitest) |
| Work on the codebase as an AI agent | [../CLAUDE.md](../CLAUDE.md) and [../.claude/rules/](../.claude/rules/) |

## By directory

### `docs/diagrams/`

Mermaid diagrams. All render on GitHub directly.

- [system-architecture.md](diagrams/system-architecture.md) - components, queues, DB
- [webhook-flow.md](diagrams/webhook-flow.md) - sequence diagram of one event
- [state-machine.md](diagrams/state-machine.md) - order status transitions
- [deployment-topology.md](diagrams/deployment-topology.md) - AWS resources

### `docs/runbooks/`

Incident response playbooks. Each has Symptom -> Detect -> Diagnose -> Resolve -> Prevent.

- [README.md](runbooks/README.md) - index
- [incident-checklist.md](runbooks/incident-checklist.md) - generic on-call response
- [dlq-drain.md](runbooks/dlq-drain.md) - DLQ landings
- [stuck-paid-order.md](runbooks/stuck-paid-order.md) - orders not reaching fulfilled
- [webhook-secret-rotation.md](runbooks/webhook-secret-rotation.md) - zero-downtime rotation
- [tenant-onboarding.md](runbooks/tenant-onboarding.md) - add a new tenant

### `docs/cloud/`

AWS deploy prerequisites and reference patterns.

- [README.md](cloud/README.md) - index + decision tree
- [account-setup.md](cloud/account-setup.md) - one-time AWS account bootstrap
- [iam-bootstrap.md](cloud/iam-bootstrap.md) - IAM + SSM + KMS patterns
- [cost-controls.md](cloud/cost-controls.md) - budgets, free-tier monitoring, promotion triggers

## Top-level files (in repo root)

- [README.md](../README.md) - project landing page
- [CLAUDE.md](../CLAUDE.md) - rules for AI agents working on the codebase
- [docker-compose.yml](../docker-compose.yml) - local Postgres + LocalStack
- [.pre-commit-config.yaml](../.pre-commit-config.yaml) - local hooks
- [renovate.json](../renovate.json) - dependency update automation

## The `designs/` folder

Source-of-truth design docs and the AWS migration plan. See [`../designs/README.md`](../designs/README.md).

## Suggested reading order

**Day 1 (orientation):**

1. [../README.md](../README.md)
2. [ARCHITECTURE.md](ARCHITECTURE.md)
3. [QUICKSTART.md](QUICKSTART.md) - run the system locally

**Day 2 (deeper):**

1. [PHASES.md](PHASES.md)
2. One runbook of your choice from [runbooks/](runbooks/)
3. [../designs/DESIGN_v6.md](../designs/DESIGN_v6.md) Section 17 (the lessons learned)

**When planning AWS deploy:**

1. [../designs/MIGRATION_WRITEUP.md](../designs/MIGRATION_WRITEUP.md) (the why)
2. [../designs/MIGRATION_ARCHITECTURE.md](../designs/MIGRATION_ARCHITECTURE.md) (the what)
3. [cost-analysis.md](cost-analysis.md) (the bill)

**Whenever needed:**

- [COMMANDS.md](COMMANDS.md) (bookmark this)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) (bookmark this too)

## Cross-reference style

Every doc has a "Related" section linking to adjacent material. If you find a missing link, that is a doc bug: open an issue.
