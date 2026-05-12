# Runbook: Cost controls

How to keep this project's AWS bill in single-digit dollars on the Free Track, and when to plan for promotion.

## Expected spend per month (Free Track, < 1k events/day)

| Service | Free tier coverage | Expected post-tier | Notes |
|---|---|---|---|
| Lambda invocations | 1M/month | $0.20/M | < 100k/month for us |
| Lambda GB-seconds | 400k/month | $0.0000167/GB-s | 256 MB × 200 ms × 100k = 5k → free |
| SQS requests | 1M/month | $0.40/M | Producer + consumer + fulfillment ≈ 300k/month |
| CloudWatch Logs ingest | 5 GB/month | $0.50/GB | Heavily impacted by structlog verbosity |
| CloudWatch Logs storage | 5 GB-month | $0.03/GB-month | 14-day retention helps |
| ECR storage | 500 MB | $0.10/GB-month | 3 repos × 50 MB each → free |
| SSM Parameter Store (Standard) | 10k params, free | $0 | We use ≤ 30 |
| KMS requests | 20k/month | $0.03/10k | Tied to SSM reads, mostly free |
| AWS Budgets | 2 free | $0.02/budget/day | We use 3 → still ~$0.36/month |
| Data transfer out | 100 GB/month | $0.09/GB | Payment data is tiny |

**Realistic monthly bill on Free Track: $0–$2 once Free Tier credits run out (after 12 months).** Inside the 12-month window, the only line items are AWS Budgets ($0.36) and possibly KMS at edge cases.

## Where money actually leaks

1. **Logs**, by a wide margin. Structlog with `level=DEBUG` will churn through 5 GB/month easily during incidents.
2. **NAT Gateway** if you ever promote to VPC. ~$32/month minimum, charged hourly regardless of traffic. The Free Track design explicitly avoids this.
3. **Interface Endpoints** if you go VPC. ~$7/month per endpoint per AZ.
4. **CloudFront** if you add a CDN for the widget bundle. Has its own free tier (10 M reqs / 50 GB-out free first 12 months); after that, ~$1/GB.

## CDK-provisioned budgets

`FoundationStack` creates three budgets in your workload account:

- **$5 / month** — alarms at 80% ($4). "Something is off." Investigate before it grows.
- **$25 / month** — alarms at 80% ($20). Likely a misconfiguration (forgot to delete a NAT GW after testing, runaway log group). Page the on-call.
- **$100 / month** — alarms at 80% ($80). Hard halt. Disable Lambdas, freeze deploys, root cause before any new spend.

Notifications go to:
- The email in `cdk.json` context `razorpay:ops_email` (if set).
- The `OpsAlerts` SNS topic (always, for paging integrations).

## Free Track promotion triggers

Move to Paid Track / RDS-in-VPC when **any** of these are true:

- **Volume:** sustained > 5k payment events / day for two consecutive weeks. Free Track Lambda + Neon serverless DB stops keeping up.
- **Compliance:** an auditor or contract requires PCI DSS / ISO 27001 controls that the Free Track posture can't satisfy. The most common trigger is "data must be in a private subnet". CLAUDE.md and DESIGN_v5 Section 7 cover this gate.
- **Cost crossover:** monthly AWS bill exceeds $80 reliably (mostly logs + Lambda). At that point, RDS reserved instance economics start beating Neon serverless.
- **Latency:** cold starts on the consumer cross 2s p99. Move to provisioned concurrency, which requires per-Lambda config changes documented in Section 16 of DESIGN_v5.

When you flip the switch, the CDK side is a context change:

```jsonc
// cdk.json
"razorpay:env": {
  "paid-track": {
    "account": "...",
    "region": "ap-south-1",
    "track": "paid"
  }
}
```

Phase 16's `PaymentsStack` reads `track` and adds VPC + RDS constructs only when `track == "paid"`. Application code is unchanged (per the architecture invariant).

## Daily cost discipline

Three habits that compound:

1. **Delete `cdk.out/` after every deploy.** Synth artifacts are ~100 MB and accumulate.
2. **Run `cdk destroy` on dev stacks at end of day** if you don't need them overnight. The Foundation stack stays, but PaymentsStack going down during off-hours costs nothing extra (Lambdas don't bill while idle) — but ensures you don't forget a half-broken state.
3. **Set log group retention on every new log group**, never default. AWS defaults to "Never expire", which is a slow money leak.

## Cost Explorer tagging

Every stack is tagged `Project=razorpay-checkout, ManagedBy=cdk, Track=<free|paid>`. The budget filter uses `user:Project=razorpay-checkout` so multi-project accounts attribute correctly.

Activate the tag in Cost Explorer **once**, immediately after deploy. Console → Billing → Cost allocation tags → Activate `Project` and `Track`.

## Free Tier expiration

12 months after account creation, the always-free tier shrinks. The lines most likely to surprise you:

- **EC2 instance hours** become charged. We don't use EC2 directly; Lambda is unaffected.
- **EBS gp2** falls off. Again, no direct use.
- **S3 standard** keeps 5 GB free indefinitely.
- **CloudFront** loses 50 GB out / 2M reqs free; reverts to 1 TB free always-free.

For this project, the 12-month expiration is mostly a non-event. Bill will jump from ~$0.50 to ~$2 the month after.

## Emergency cost cut

If billing alarm shows $80 and you don't know why:

```bash
# 1. Pause traffic
aws lambda update-function-configuration \
  --function-name razorpay-webhook-producer \
  --reserved-concurrent-executions 0

# 2. Suspend EventBridge schedules
aws events disable-rule --name razorpay-stale-paid-monitor

# 3. Cost explorer → group by service → spot the outlier
# Usually CloudWatch Logs or NAT Gateway.

# 4. Address the leak; re-enable.
```

Don't `cdk destroy` in a panic — that risks data loss. The reserved-concurrency=0 trick halts compute without touching state.

## Related

- [`account-setup.md`](account-setup.md) — manual budget setup as safety net.
- [`incident-checklist.md`](../runbooks/incident-checklist.md) — full incident response.
- DESIGN_v5 Section 7 — Free Track vs Paid Track architecture.
