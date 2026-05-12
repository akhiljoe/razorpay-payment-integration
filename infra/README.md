# CDK infrastructure (Python)

CDK app provisioning the AWS resources for the Razorpay payment integration. Two stacks:

- **FoundationStack** (Phase 15) — account-level building blocks (ECR, log groups, ops SNS, billing budgets).
- **PaymentsStack** (Phase 16) — the runtime: KMS, secrets, SQS queue pairs, 3 Lambda functions, IAM roles, Function URL, EventBridge schedule.

## Layout

```
infra/
├── app.py                  # CDK app entry; reads cdk.json context
├── cdk.json                # Per-env config, feature flags, ops email
├── pyproject.toml          # Python deps (aws-cdk-lib, constructs)
├── stacks/
│   ├── __init__.py
│   ├── foundation.py       # ECR + log groups + ops SNS + 3 budgets
│   └── payments.py         # KMS + secrets + SQS + Lambdas + IAM + URL
└── README.md               # this file
```

## Quick start (local synth only)

```bash
cd infra
uv sync --extra dev

# Synth the CloudFormation template to ./cdk.out/ without touching AWS.
uv run cdk synth

# See what `cdk deploy` would do, given current AWS state.
# Requires AWS_PROFILE set; see ../docs/cloud/account-setup.md.
AWS_PROFILE=razorpay-prod uv run cdk diff
```

`cdk synth` does **not** require AWS credentials and never contacts AWS. It just generates CloudFormation. Use this in CI to lint stack changes.

## Deploy (Phase 17)

When you have an AWS account, IAM Identity Center configured, and have run `cdk bootstrap`:

```bash
AWS_PROFILE=razorpay-prod uv run cdk diff
AWS_PROFILE=razorpay-prod uv run cdk deploy razorpay-free-track-foundation
```

The stack creates retained resources (ECR repos, log groups). `cdk destroy` will not delete them; do that manually only if you're sure.

## Context overrides

`cdk.json` has placeholders. Override per environment:

```bash
# Inline
AWS_PROFILE=razorpay-prod CDK_DEFAULT_ACCOUNT=123456789012 \
  uv run cdk deploy --context razorpay:ops_email=ops@example.com

# Or edit cdk.json and commit
```

## What this stack provisions

See `stacks/foundation.py`. Summary:

- **3 ECR repositories** for the producer, consumer, fulfillment Lambda container images.
- **4 CloudWatch log groups** with 14-day retention.
- **1 SNS topic** `razorpay-ops-<track>` for alerts.
- **3 AWS Budgets** at $5, $25, $100 monthly with email + SNS notifications at 80% threshold.

All resources are tagged `Project=razorpay-checkout, ManagedBy=cdk, Track=<free|paid>`.

## What `PaymentsStack` provisions

See `stacks/payments.py`. Summary:

- **KMS customer-managed key** `alias/razorpay-<track>` wrapping SSM SecureStrings + Secrets Manager.
- **DB credentials Secret** in Secrets Manager (or imported via `db_dsn_secret_arn` context).
- **2 SQS FIFO queue pairs** with `maxReceiveCount=5`, content-based dedup, 14-day DLQ retention.
- **3 Lambda DockerImageFunctions** sourcing `:latest` from the Phase 15 ECR repos.
- **3 IAM roles** with least-privilege scoped policies. The producer role has explicit `Deny` on RDS and the DB secret (Critical Invariant 4).
- **Producer Function URL** with `auth_type=NONE` + CORS. Public HTTPS endpoint Razorpay POSTs to.
- **SQS event source mappings** for consumer + fulfillment (batch_size=1 for FIFO ordering).
- **EventBridge schedule** `rate(5 minutes)` for `stale-paid-monitor`.

## What's NOT in either stack (yet)

- VPC → Free Track has no VPC. Paid Track adds it; gated on `track == "paid"` context.
- RDS / Neon DB connection → managed outside CDK on Free Track; reference its DSN via SSM SecureString or imported Secrets Manager ARN.
- Admin route API Gateway → optional; for now admin lives in the same Lambda as the FastAPI app if deployed that way, or behind the consumer's IAM-protected handler.
- CloudFront in front of the Function URL → optional; Phase 17 decision.

## Cost expectations

See [`../docs/cloud/cost-controls.md`](../docs/cloud/cost-controls.md). The Foundation stack alone costs ~$0.36/month (3 AWS Budgets × $0.02/day, prorated). Everything else in this stack is in the always-free tier or under Free Tier limits for the first 12 months.

## Related runbooks

- [`../docs/cloud/account-setup.md`](../docs/cloud/account-setup.md) — one-time account bootstrap.
- [`../docs/cloud/iam-bootstrap.md`](../docs/cloud/iam-bootstrap.md) — IAM role and policy patterns.
- [`../docs/cloud/cost-controls.md`](../docs/cloud/cost-controls.md) — keeping the bill in single digits.

## Archive

`infra/archive-ts/` contains the obsolete TypeScript CDK skeleton from before Phase 15 reset. Kept for reference; don't deploy from it.
