# AWS Deployment Progress

Live tracker of what's actually provisioned where. Updated as phases land.

## Account Inventory

| Field | Dev | Prod |
|---|---|---|
| AWS Account ID | `111111111111` | `222222222222` |
| Account display name | Turing | Kugelblitz |
| Root login email | `aws-dev@example.com` | `aws-prod@example.com` |
| Account age | ~1 week (12-month free tier active) | ~1 week (12-month free tier active) |

## Phase Status

| Phase | What | Status |
|---|---|---|
| 1 | Account hardening (root MFA, alt contacts, password policy, CloudTrail with Object Lock, security alarms) | **Complete** |
| 2 | IAM admin user + AWS CLI profiles + GitHub OIDC + per-env deploy role | **Complete** |
| 3 | Foundation CDK stack (KMS CMKs, ECR, CloudTrail S3, security alarms, base SNS topics, Budgets) | Pending |
| 4 | Neon database + Alembic migrations (incl. `0008_create_readonly_role`) | Pending |
| 5 | Secrets bootstrap (tenant Razorpay + DB credentials) | Pending |
| 6 | Compute + messaging CDK (6 Lambdas, 4 SQS queues, API Gateway, dashboards, alarms) | Pending |
| 7 | Frontend widget (CloudFront + S3) | Pending |
| 8 | Observability (CloudWatch dashboards, SNS subscriptions, log archive Firehose) | Pending |
| 9 | Dev end-to-end with Razorpay test mode | Pending |
| 10 | Prod cutover with Razorpay live mode | Pending |

## Phase 1 + 2 Deliverables (Done)

### Root user hardening (both accounts)

- Root MFA enabled (virtual authenticator)
- Root access keys: none
- Alternate contacts set (Billing / Operations / Security)
- Password policy: 14-char min, complexity, 90-day rotation, prevent reuse 24
- IAM user access to Billing: activated

### Daily-driver IAM users

| Account | IAM user | MFA enforcement policy attached | AWS CLI profile |
|---|---|---|---|
| Dev | `admin-dev` | `enforce-mfa` (inline) | `razorpay-dev` |
| Prod | `admin-prod` | `enforce-mfa` (inline) | `razorpay-prod` |

Local AWS CLI config:

- `~/.aws/credentials` has both profiles
- `~/.aws/config` has `mfa_serial` + `duration_seconds=43200` per profile

For real work, get MFA-authenticated session credentials with:

```bash
eval $(aws sts get-session-token \
  --serial-number arn:aws:iam::<acct>:mfa/<device> \
  --token-code <6-digit> \
  --duration-seconds 43200 \
  --profile razorpay-<env> \
  --query 'Credentials.[`export AWS_ACCESS_KEY_ID=`+AccessKeyId,`export AWS_SECRET_ACCESS_KEY=`+SecretAccessKey,`export AWS_SESSION_TOKEN=`+SessionToken]' \
  --output text | tr '\t' '\n')
```

### GitHub OIDC providers + deploy roles

| Account | OIDC provider ARN | Deploy role ARN |
|---|---|---|
| Dev | `arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com` | `arn:aws:iam::111111111111:role/razorpay-dev-github-deploy` |
| Prod | `arn:aws:iam::222222222222:oidc-provider/token.actions.githubusercontent.com` | `arn:aws:iam::222222222222:role/razorpay-prod-github-deploy` |

Trust policy intent:

| Account | What's allowed to assume the role |
|---|---|
| Dev | Any branch on `akhiljoe/razorpay-payment-integration` OR GitHub Environment `dev` |
| Prod | Only `main` branch OR GitHub Environment `prod` (with required reviewers) |

GitHub repo: `akhiljoe/razorpay-payment-integration`

These role ARNs will be used by `.github/workflows/deploy.yml` (added in Phase 6) via `aws-actions/configure-aws-credentials`. No long-lived AWS keys in GitHub Secrets.

## Outstanding for Phase 3

Before we run `cdk bootstrap`:

1. Confirm example.com Cloudflare DNS access is available (we'll add CNAME records for ACM cert validation during foundation deploy).
2. Decide on initial KMS key rotation cadence (default annual; can stay).
3. Review `cdk.json` features block (everything off by default; cost-protected).

Phase 3 will:

- Bootstrap CDK in both accounts (`cdk bootstrap`)
- Refactor `infra/stacks/foundation.py` to provision: 5 KMS CMKs per env, ECR repos, CloudTrail-to-local-S3 with Object Lock, AWS Budgets, Cost Anomaly Detection, ops SNS topics (page + warn), CloudWatch security metric filters + alarms, base ACM certs (deferred until DNS validation can complete via Cloudflare)
- `cdk deploy razorpay-dev-foundation` then `razorpay-prod-foundation`
- Verify all resources visible in Console

## Cost burn so far

Phase 1 + 2 cost: **$0**. No billable resources created yet.

Phase 3 will create the first billable resources (KMS CMKs at $1/key/month, S3 storage for CloudTrail at <$1/month). Estimated month-one cost after Phase 3: ~$6-10 per account.
