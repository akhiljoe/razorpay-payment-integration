# Documentation index

Everything under `docs/` plus pointers to top-level files.

## By question

| I want to | Read |
|---|---|
| Run the system locally for the first time | [QUICKSTART.md](../QUICKSTART.md) |
| Look up "how do I do X right now" | [COMMANDS.md](../COMMANDS.md) |
| Debug an error or unexpected behavior | [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) |
| Understand the architecture in 10 minutes | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| See a picture instead of prose | [diagrams/](diagrams/) |
| Read the formal design spec | [designs/DESIGN_v5.md](../designs/DESIGN_v5.md) |
| Respond to an incident as on-call | [runbooks/](runbooks/) |
| Deploy to AWS | [cloud/](cloud/) |
| Know what each phase shipped + why | [PHASES.md](../PHASES.md) |
| Work on the codebase as an AI agent | [CLAUDE.md](../CLAUDE.md) and [.claude/rules/](../.claude/rules/) |

## By directory

### `docs/diagrams/`

Mermaid diagrams. All render on GitHub directly.

- [system-architecture.md](diagrams/system-architecture.md) — components, queues, DB
- [webhook-flow.md](diagrams/webhook-flow.md) — sequence diagram of one event
- [state-machine.md](diagrams/state-machine.md) — order status transitions
- [deployment-topology.md](diagrams/deployment-topology.md) — AWS resources (Phase 17+)

### `docs/runbooks/`

Incident response playbooks. Each has Symptom → Detect → Diagnose → Resolve → Prevent.

- [README.md](runbooks/README.md) — index
- [incident-checklist.md](runbooks/incident-checklist.md) — generic on-call response
- [dlq-drain.md](runbooks/dlq-drain.md) — DLQ landings
- [stuck-paid-order.md](runbooks/stuck-paid-order.md) — orders not reaching fulfilled
- [webhook-secret-rotation.md](runbooks/webhook-secret-rotation.md) — zero-downtime rotation
- [tenant-onboarding.md](runbooks/tenant-onboarding.md) — add a new tenant

### `docs/cloud/`

AWS deploy prerequisites.

- [README.md](cloud/README.md) — index + decision tree
- [account-setup.md](cloud/account-setup.md) — one-time AWS account bootstrap
- [iam-bootstrap.md](cloud/iam-bootstrap.md) — IAM + SSM + KMS patterns
- [cost-controls.md](cloud/cost-controls.md) — budgets, free-tier monitoring, promotion triggers

## Top-level files (in repo root)

- [README.md](../README.md) — project landing page
- [ARCHITECTURE.md](../ARCHITECTURE.md) — system overview + critical invariants
- [QUICKSTART.md](../QUICKSTART.md) — clone-to-running in 10 minutes
- [COMMANDS.md](../COMMANDS.md) — every useful command
- [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) — symptom-indexed fixes
- [PHASES.md](../PHASES.md) — what shipped in each phase and why
- [CLAUDE.md](../CLAUDE.md) — rules for AI agents working on the codebase
- [IMPLEMENTATION_PLAN_V2.md](../IMPLEMENTATION_PLAN_V2.md) — forward-looking phase plan
- [designs/DESIGN_v5.md](../designs/DESIGN_v5.md) — formal spec with Section 17 learnings

## Suggested reading order

**Day 1 (orientation):**

1. README.md
2. ARCHITECTURE.md
3. QUICKSTART.md — run the system locally

**Day 2 (deeper):**

4. PHASES.md
5. One runbook of your choice from `docs/runbooks/`
6. DESIGN_v5.md Section 17 (the lessons learned)

**Whenever needed:**

- COMMANDS.md (bookmark this)
- TROUBLESHOOTING.md (bookmark this too)

## Cross-reference style

Every doc has a "Related" section linking to adjacent material. If you find a missing link, that's a doc bug — open an issue.
