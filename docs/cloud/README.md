# Cloud onboarding documentation

Manual steps and reference material for moving the payment integration from local dev to AWS.

| Document | When to read |
|---|---|
| [account-setup.md](account-setup.md) | Once, before any `cdk deploy`. AWS account creation, Identity Center, CLI config, CDK bootstrap. |
| [iam-bootstrap.md](iam-bootstrap.md) | Before Phase 16 (PaymentsStack). Role structure, SSM layout, KMS strategy. |
| [cost-controls.md](cost-controls.md) | Before first deploy and on every billing surprise. Free Tier expectations, promotion triggers, emergency cost cuts. |

## Phase ordering

1. **Phase 15 (done):** Foundation stack, runbooks. Local synth only.
2. **Phase 16 (next):** PaymentsStack — Lambdas, SQS, EventBridge, secrets.
3. **Phase 17:** First real deploy + smoke test.
4. **Phase 18:** TEST_PLAN.md + SCALE_PLAN.md closeout docs.

## Decision: deploy now or later?

You can ship Phases 1-14 as a library without ever deploying to AWS. The local dev loop (uvicorn + local-pump + dashboard) is a complete, testable system that runs against Postgres + LocalStack.

Deploy to AWS only if:

- You need a real webhook endpoint reachable from Razorpay's servers (i.e. you're going live).
- You want CloudWatch + AWS Budgets visibility on real traffic.
- A specific compliance or contract requirement dictates AWS hosting.

Otherwise, the LocalStack-based testing is sufficient.

## Related

- `infra/README.md` — CDK operational guide.
- `docs/runbooks/` — operator playbooks (DLQ, stuck orders, secret rotation, tenant onboarding, incident response).
