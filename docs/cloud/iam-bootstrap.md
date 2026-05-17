# Runbook: IAM bootstrap

How IAM roles, policies, and SSM access are structured for this project.

## Principle

**No persistent IAM users for human operators.** Use IAM Identity Center (SSO) with permission sets. Programmatic actors (CDK, Lambdas, scheduled events) use IAM roles, scoped to the minimum permissions they need.

## Identity model

```text
Operator (human)
  └─ SSO session
       └─ AssumeRole → RazorpayAdmin (4-hour TTL)
              └─ cdk deploy, console clicks

CDK CLI
  └─ Uses operator credentials at deploy time

Each Lambda at runtime
  └─ Assumes its own IamRole created by CDK
       └─ Tightly scoped, see below

CI (GitHub Actions, when wired)
  └─ OIDC trust → CI deploy role (subset of RazorpayAdmin)
       └─ cdk deploy without long-lived credentials
```

## Roles created by CDK

### `WebhookProducerRole` (Phase 16 stack)

The producer holds **zero database credentials**. Required permissions:

- `sqs:SendMessage` on `webhook-events.fifo` only.
- `ssm:GetParameter` on `/razorpay/<tenant_id>/key_id`, `key_secret`, `webhook_secret`, `webhook_secret_previous` for any tenant.
- `secretsmanager:GetSecretValue` if you migrate from SSM later.
- `kms:Decrypt` on the KMS key wrapping SSM SecureStrings.
- `logs:CreateLogStream`, `logs:PutLogEvents` on `/aws/lambda/razorpay-webhook-producer`.
- Optional: `xray:PutTraceSegments` if OTel exporter = xray.

Explicit deny on:

- All RDS / DynamoDB actions.
- `sqs:ReceiveMessage` (producer never reads queues).
- `sqs:*` on the fulfillment queue.

### `WebhookConsumerRole` (Phase 16 stack)

The consumer reads SQS and writes to Postgres. Required:

- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`, `sqs:ChangeMessageVisibility` on `webhook-events.fifo`.
- `sqs:SendMessage` on `fulfillment-queue.fifo`.
- `ssm:GetParameter` on tenant secrets (same as producer).
- `secretsmanager:GetSecretValue` on the DB credentials secret.
- `kms:Decrypt`.
- CloudWatch Logs write.
- Optional: `xray:PutTraceSegments`.

### `FulfillmentRole` (Phase 16 stack)

Same DB access pattern as consumer, plus:

- `sqs:ReceiveMessage`, etc. on `fulfillment-queue.fifo`.
- Permission to invoke any external service your `FulfillmentHook` needs (S3 write for receipts, SES send, third-party API access via Secrets Manager). Default is none; the host application narrows this.

### `AdminRole` (Phase 16 stack, if hosted as separate Lambda)

Reconciliation, replay-event, retry-fulfillment admin endpoints. Same DB access as consumer plus `secretsmanager:GetSecretValue` on `admin_api_key`. Routed via API Gateway with an IAM authorizer.

## Breakglass role

For incidents where the SSO-issued admin permissions aren't enough (rare).

1. Create a separate IAM role `RazorpayBreakglass` in the workload account.
2. Trust policy: only the root account user can assume it, MFA required.
3. Inline policy: `AdministratorAccess`.
4. Session duration: 1 hour (forces frequent re-auth).
5. **CloudTrail alert:** any `sts:AssumeRole` against `RazorpayBreakglass` triggers an SNS alert to the security channel.

Usage: only when a deploy goes catastrophically wrong and the regular admin path is blocked.

## SSM Parameter Store layout

Tenant-scoped path: `/razorpay/<tenant_id>/<key>`.

- `/razorpay/acme-india/key_id`
- `/razorpay/acme-india/key_secret`           — SecureString
- `/razorpay/acme-india/webhook_secret`       — SecureString
- `/razorpay/acme-india/webhook_secret_previous` — SecureString (during rotation)
- `/razorpay/acme-india/mode`                 — String, `test` or `live`

Each parameter is encrypted with a customer-managed KMS key (Phase 16 provisions this). Lambdas get `ssm:GetParameter` scoped to `arn:aws:ssm:<region>:<account>:parameter/razorpay/*` to avoid per-tenant policy churn.

### Why not Secrets Manager for tenant secrets?

- SSM SecureString is free up to 10k parameters; Secrets Manager costs $0.40/secret/month. With 5 tenants we'd pay $24/year for the same thing.
- Secrets Manager has automatic rotation hooks; we don't use them (manual rotation via runbook is fine for low-tenant counts).
- The DB password lives in Secrets Manager because the RDS rotation Lambda integrates only with Secrets Manager.

## KMS key strategy

- One customer-managed KMS key per environment: `alias/razorpay-{track}`.
- Used to encrypt SSM SecureStrings and Secrets Manager secrets.
- Key policy: only roles in the IAM model above can decrypt. The console root user has admin (per AWS recommendation).
- Free Tier: 20k requests/month free; we'll use < 1k.

## Policy linting

Before every `cdk deploy`:

```bash
cd /c/Users/you/Documents/razorpay-payment-integration/infra
AWS_PROFILE=razorpay-prod uv run cdk diff > /tmp/cdk-diff.txt
grep -E "Effect|Action|Resource|NotAction|NotResource" /tmp/cdk-diff.txt | less
```

Anything that adds wildcards (`Action: "*"` or `Resource: "*"` outside of `logs:CreateLogStream` patterns) needs a second pair of eyes.

## Common mistakes to avoid

- **Granting `s3:*` because S3 doesn't support fine-grained Resource ARNs in some actions.** It does; use `arn:aws:s3:::bucket-name/*`.
- **Sharing roles across Lambdas to save typing.** Defeats least-privilege. CDK makes per-Lambda role creation a one-liner.
- **Putting secrets in Lambda env vars instead of SSM.** They appear in plaintext in CloudFormation drift checks. Use SSM with `securestring` and read at startup.
- **Forgetting `kms:Decrypt`** when adding a new SecureString parameter. Lambda will fail with `AccessDeniedException` not `KMSException`, so the error message lies about the cause.

## Related

- [`account-setup.md`](account-setup.md) — Identity Center setup.
- [`cost-controls.md`](cost-controls.md) — how IAM choices affect billing.
- `.claude/rules/tenant-isolation.md` — why per-tenant SSM paths are load-bearing.
