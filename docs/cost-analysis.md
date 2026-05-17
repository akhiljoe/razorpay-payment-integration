# AWS Cost Analysis: Razorpay Payment Integration

Living cost document. The numbers below are stale the moment the architecture changes. PRs touching `infra/stacks/` or the architecture doc must update this file or include `Cost impact: none, verified against docs/cost-analysis.md` in the description.

| Field | Value |
|---|---|
| Last verified against architecture | 2026-05-16 against `designs/MIGRATION_ARCHITECTURE.md` |
| Stale-by trigger | Any change to stack contents, NFR targets, region, AWS service selection, account topology, or AWS published prices |
| Currency | 1 USD = 83.5 INR as of 2026-05-16. **Re-verify rate before quoting INR.** |
| Region | `ap-south-1` (Mumbai) |
| Tenant count | 2 (operator's own websites, no planned growth) |

---

## 1. Executive Summary

The AWS bill is **~$14/month** combined across dev + prod while running with no live payment traffic. When example.com goes live with real Razorpay payments and traffic exceeds ~1000 tx/mo, WAF gets enabled and the bill rises to **~$36/month**. Compliance-driven services (Inspector, AWS Config, GuardDuty, Security Hub, Provisioned Concurrency) remain off via feature toggles until an explicit trigger fires (compliance audit, real attack signal, SLA breach), at which point each adds a known per-service cost.

| Scenario | Monthly USD (both envs) | Monthly INR | Notes |
|---|---|---|---|
| **Today (no live payments)** | **$13 - $18** | **₹1,080 - ₹1,500** | Baseline; the empty-system cost |
| **Live with Razorpay, <1000 tx/mo** | **$13 - $18** | **₹1,080 - ₹1,500** | Same as above; WAF still off; free-tier alternatives cover the gap |
| **Live with WAF (1000+ tx/mo)** | **$35 - $42** | **₹2,920 - ₹3,510** | WAF enabled; per-tx cost remains negligible |
| **Compliance footprint (only on demand)** | **$65 - $110** | **₹5,430 - ₹9,190** | Inspector + AWS Config + GuardDuty + Security Hub all enabled |

**The platform cost is overwhelmingly fixed.** Per-transaction marginal cost is ~$0.00004 USD (₹0.003 per payment). At 100,000 transactions per month, variable cost is ~$4. Volume does not break the bill until Neon's free tier exhausts (~100k tx/mo).

---

## 2. Methodology and Assumptions

### 2.1 Pricing source and confidence

Confidence labels per claim:

| Label | Means |
|---|---|
| **Cited** | Quoted from a current AWS public pricing page |
| **Estimated** | Derived from public AWS pricing logic applied to assumed usage |
| **Measured** | Pulled from an actual AWS bill or AWS Pricing Calculator output |

This document is currently **Cited + Estimated**. Entries reclassify to **Measured** as real bills arrive.

### 2.2 NFR inputs

| Dimension | Value |
|---|---|
| Tenant count | 2 (operator's own websites) |
| Peak webhook EPS | 50 sustained, 200 burst |
| Average webhook payload | 4 KB, 95p 32 KB |
| Monthly payment volume target | 10k initially, may grow |
| Webhook ACK p95 SLA | 500 ms (Razorpay hard SLA) |
| Region | `ap-south-1` |
| Environments | dev + prod, 2 standalone AWS accounts |

### 2.3 What one transaction touches

| Component | Per transaction |
|---|---|
| Producer Lambda invocations | 1 |
| Consumer Lambda invocations | 1 |
| Fulfillment Lambda invocations | 1 |
| SQS messages (webhook-events) | 3 API calls (publish + receive + delete) |
| SQS messages (fulfillment) | 3 API calls |
| Neon DB transactions | ~2 (Consumer + Fulfillment) |
| Secrets Manager API calls | ~0 typically (cached) |
| KMS Decrypt calls | ~3-5 |
| CloudWatch log records | ~10-30 structured log lines |
| Bytes logged | ~3-15 KB total |

### 2.4 Currency

USD costs converted at the rate in the header. Re-fetch the rate same-day for any commitment to stakeholders. A 5% INR weakening shifts the prod bill by ~₹400-500; not meaningful at this scale.

### 2.5 What this document does NOT cost

- **Razorpay processing fees** (~2% domestic India). Out of scope.
- **Labor cost.** Out of scope.
- **Neon free tier database.** $0 today, modeled separately.
- **Domain registration** (Cloudflare-managed, free DNS).
- **Third-party tooling** (PagerDuty etc.). The architecture explicitly does not include any.

---

## 3. Cost Taxonomy

Slicing by **how cost responds to operational variables**, not by AWS service:

| Slice | Question it answers |
|---|---|
| §4.1 Always-on fixed cost | What you pay every month, with or without traffic |
| §4.2 Per-transaction marginal | What one more webhook costs |
| §4.3 Per-tenant marginal | What one more tenant costs (not currently expected to change) |
| §4.4 Free-tier covered today | What is $0 because of AWS free tier, with expiry dates |
| §4.5 Feature-toggled services | What you pay if you flip a toggle on |

A by-service appendix exists in §10 for cross-reference but is not the primary lens.

---

## 4. Per-Slice Cost Analysis

### 4.1 Always-On Fixed Cost (Per Environment, Both Envs Combined)

| Line | Unit price | Per-env monthly | Both envs | Source | Confidence |
|---|---|---|---|---|---|
| KMS CMKs (5 keys: secrets, sqs, s3-artifacts, cloudwatch-logs, db-reserved) | $1.00 per CMK/month | $5.00 | $10.00 | AWS KMS pricing | Cited |
| Secrets Manager (4 secrets: 2 tenant + 2 DB) | $0.40 per secret/month | $1.60 | $3.20 | AWS Secrets Manager pricing | Cited |
| Route 53 hosted zones | (none; using Cloudflare) | $0 | $0 | n/a | Cited |
| API Gateway HTTP API (idle) | Pay per request | $0 | $0 | AWS API Gateway pricing | Cited |
| CloudFront distribution (idle) | Pay per request | $0 | $0 | AWS CloudFront pricing | Cited |
| ACM certificates | $0 | $0 | $0 | AWS ACM pricing | Cited |
| Lambda functions (6 deployed, idle) | $0 | $0 | $0 | AWS Lambda pricing | Cited |
| SQS FIFO queues (4) | $0 fixed | $0 | $0 | AWS SQS pricing | Cited |
| ECR repos (3 with ~500 MB each) | $0.10/GB-month | $0.15 | $0.30 | AWS ECR pricing | Estimated |
| CloudWatch Log Groups (empty) | $0 (within 5 GB free tier) | $0 | $0 | AWS CloudWatch pricing | Cited |
| CloudWatch Dashboard | $0 (first 3 free) | $0 | $0 | AWS CloudWatch pricing | Cited |
| CloudWatch Alarms (~15 alarms) | $0 (first 10 free, next 5 × $0.10) | $0.50 | $1.00 | AWS CloudWatch pricing | Cited |
| CloudTrail (1 management trail per account) | Free trail + minimal S3 storage | $0.10 | $0.20 | AWS CloudTrail pricing | Cited |
| S3 buckets (widget, log archive, CloudTrail; near-empty) | $0.025/GB-month | $0.30 | $0.60 | AWS S3 pricing | Estimated |
| SNS topics (ops-page, ops-warn) | $0 fixed | $0 | $0 | AWS SNS pricing | Cited |
| **Subtotal always-on** | | **~$7** | **~$15** | | |

**Combined always-on baseline both envs: ~$14-18/month.** This is the cost whether or not anything runs.

### 4.2 Per-Transaction Marginal Cost

What one additional Razorpay webhook costs end-to-end:

| Component | Per transaction USD |
|---|---|
| Lambda invocations (3 functions × 1 each) | $0.0000006 |
| Lambda compute (3 × 768 MB × 200 ms avg) | $0.0000077 |
| SQS FIFO requests (6 API calls) | $0.000003 |
| API Gateway HTTP API (1 request) | $0.000001 |
| CloudWatch Logs ingestion (~10 KB) | $0.000005 |
| KMS Decrypt API calls (~3-5, mostly cached) | $0.0000015 |
| **Total per transaction** | **~$0.0000185** |

| Volume | Variable monthly cost (both envs) |
|---|---|
| 10k tx/mo | $0.37 |
| 100k tx/mo | $3.70 |
| 1M tx/mo | $37 |

At 1M tx/mo the variable line begins to rival the fixed line. By then Neon's free tier is exhausted and the upgrade trigger fires for Neon Launch plan (~$38/mo combined).

### 4.3 Per-Tenant Marginal Cost (Not Currently Expected to Change)

Tenant count is fixed at 2. This subsection exists as a reference if the model ever changes:

| Component | Per tenant per env per month |
|---|---|
| Secrets Manager (1 tenant Razorpay secret) | $0.40 |
| KMS Decrypt request volume | ~$0.025 |
| Log volume attribution | Negligible |

**Per-tenant marginal both envs: ~$0.90/month.**

| Hypothetical tenant count | Tenant-driven additional cost (both envs) |
|---|---|
| 2 (current) | $0 baseline |
| 10 | +$7.20 |
| 50 | +$43.20 |
| 500 | +$400 |

Not on the roadmap. If it ever becomes one, revisit consolidating multiple tenants into a single Secrets Manager secret (JSON keyed by tenant_id).

### 4.4 Free-Tier Covered Today

Two categories. Always-free does not expire. 12-month free expires 12 months after each account's creation.

| Service | Tier type | What's covered | Per-account expiry |
|---|---|---|---|
| Lambda | Always-free | 1M req/mo + 400k GB-sec/mo | Never |
| SQS | Always-free | 1M req/mo | Never |
| CloudWatch Logs | Always-free partial | 5 GB ingested + 5 GB stored | Never |
| CloudWatch Custom Metrics | Always-free partial | First 10 metrics | Never |
| CloudWatch Dashboards | Always-free | First 3 dashboards | Never |
| CloudWatch Alarms | Always-free partial | First 10 standard alarms | Never |
| CloudFront | 12-month | 1 TB egress + 10M requests | 12 months from account creation |
| API Gateway | 12-month | 1M HTTP API requests/mo | 12 months from account creation |
| S3 | 12-month | 5 GB Standard + 20k GET + 2k PUT | 12 months from account creation |
| ECR | 12-month | 500 MB private repo storage | 12 months from account creation |
| KMS | Always-free partial | 20k requests/mo | Never |
| CloudTrail | Always-free | First management trail per account | Never |
| Neon (external) | Free tier | 0.5 GB storage, 100 compute hrs/mo | Never (terms may change) |

Both AWS accounts are within the 12-month free tier window today. Free-tier expiration impact at month 13: **+$2-10/mo combined** (CloudFront egress, API Gateway requests). Manageable.

### 4.5 Feature-Toggled Services

**Every service in this table is wired in the CDK code from day one.** They are conditionally instantiated based on `infra/cdk.json` `razorpay:features` flags, all defaulting to OFF. Flip a flag, `cdk deploy`, service activates. Application code never changes.

| Toggle | Cost when enabled (combined both envs) | Trigger to enable |
|---|---|---|
| `waf_enabled` | +$22/mo | 1000+ tx/mo OR Producer invocations >1000/min for 5 min |
| `provisioned_concurrency_enabled` (Producer prod only) | +$16.60/mo | Razorpay reports webhook timeout OR webhook p95 >400 ms for 10 min |
| `inspector_enabled` (6 fns × 2 envs) | +$18/mo | 100+ MAU OR security audit requested |
| `config_enabled` | +$4-10/mo | Compliance ask OR >100 resources |
| `guardduty_enabled` | +$6-12/mo | First security incident OR 100+ MAU |
| `security_hub_enabled` | +$6-16/mo | Compliance ask OR audit needed |
| `rds_in_vpc_enabled` (entire Stage 2 stack: VPC, PrivateLink, RDS/Aurora, RDS Proxy) | +$300-400/mo | 100k+ tx/mo OR PCI requires AWS-native DB |
| `aurora_global_db_enabled` (warm DR replica in ap-southeast-1) | +$80-200/mo | RTO < 1 hour requirement |

**Exact enable steps per service** (CDK change, deploy command, verification, rollback) are in `designs/MIGRATION_ARCHITECTURE.md` "Deferred Services Enablement Guide" section. Each service has its own subsection.

### 4.6 Cost ladder

| Configuration | Combined monthly cost |
|---|---|
| Lite (today; default) | ~$14 |
| Lite + WAF | ~$36 |
| Lite + WAF + Provisioned Concurrency | ~$52 |
| Lite + WAF + Inspector + GuardDuty + Security Hub | ~$60 |
| Lite + WAF + all compliance services (Inspector + Config + GuardDuty + Security Hub) | ~$80-100 |
| All of the above + Neon Launch plan | ~$120-140 |
| All of the above + RDS/Aurora migration | ~$400-500 |
| All of the above + Aurora Global DB | ~$500-700 |

Each rung is a feature toggle and a `cdk deploy`. No code change required.

---

## 5. Time-Based Projections

### 5.1 Day Zero

CDK deploy lands. No traffic. ~$0.40-0.80/day prorated rate kicks in (fixed services billed by the hour). Total day-zero cost: $0.

### 5.2 Week One

Smoke tests, integration tests, some manual API calls. Variable cost <$1. Fixed cost prorated ~$3-4. **Total week one: ~$3-5.**

### 5.3 Month One (No Live Payments, Smoke + Dev Use Only)

| Slice | Both envs monthly |
|---|---|
| Fixed (always-on baseline) | $13-18 |
| Per-transaction (dev smoke + integration tests, ~1000 calls) | <$1 |
| **Total month one** | **~$14-19** |

**INR: ₹1,170-1,590/month.**

### 5.4 Month Three to Twelve (Pre-Launch or Low Traffic)

Bill stays at **~$14-19/month combined** as long as the system is running with little real traffic. Free tier covers per-transaction variable cost. Tenant count and infrastructure both unchanged.

### 5.5 When example.com Goes Live with Razorpay Live Mode

Three checkpoints:

| Event | Cost change |
|---|---|
| Razorpay live webhook configured, no real customer traffic yet | $0 change ($14-18/mo) |
| First real payments trickle in (<100/mo) | $0 change ($14-18/mo) |
| Traffic exceeds 1000/mo OR cost-amplification alarm fires | WAF flipped on, +$22/mo (total ~$36-40/mo) |

### 5.6 Year One

Assume 30k tx/mo by month 12 (modest growth on example.com):

| Adjustment vs month one | Delta both envs |
|---|---|
| Tx volume 30k/mo (vs 1k baseline) | +$1.10 |
| S3 storage growth | +$0.50 |
| ECR image storage | +$1.50 |
| 12-month free tier expiry on CloudFront | +$1.70 |
| Other 12-month free tier expiry | +$1.00 |
| **Total year one (still no WAF)** | **$20-25/mo** |
| If 1000+ tx/mo trigger fires → WAF on | **$42-47/mo** |

**INR (with WAF): ₹3,500-3,900/month.**

---

## 6. Hidden Costs and Watch-Items

### 6.1 Inspector for Lambda (if enabled)

| Metric | Value |
|---|---|
| Unit price | $1.50/function/month |
| 6 functions × 2 envs | $18/month |
| Cost driver | Number of Lambda functions, not invocations |
| Watch trigger | Function count doubles (e.g., per-tenant fulfillment hooks) |

Off by default per architecture. Enable only on security audit demand.

### 6.2 WAF Rule Count (if enabled)

| Metric | Value |
|---|---|
| Web ACL price | $5/Web ACL/month |
| Per-rule price | $1/rule/month |
| At 6 rules × 2 envs | $22/month |
| Per-request | $0.60/M requests (negligible at our volume) |

Adding a new rule per env adds $1/mo each. Auditor-driven rule expansion can add 5-10 rules quickly.

### 6.3 CloudWatch Logs Ingestion

| Metric | Value |
|---|---|
| Free tier | First 5 GB/mo per account |
| Beyond | $0.50/GB |
| Expected at low traffic | ~200 MB/mo per env, well within free tier |
| Watch triggers | DEBUG logging in prod; retry storm; verbose request body logging |
| Burst scenario | 10 GB of logs from a sustained retry storm = $5 one-time |

### 6.4 Cost-Amplification Attack on Webhook URL

| Scenario | Cost |
|---|---|
| 1-hour 1M-request flood, no WAF | ~$45-65 (Lambda + SQS + log volume + GuardDuty if enabled) |
| 24-hour sustained | ~$1,100-1,500 |

Producer Reserved Concurrency = 50 caps blast radius. Producer-invocations alarm (>1000/min) fires within minutes. Operator response: flip `waf_enabled: true`. Worst case is 5-10 minutes of detection latency plus 5-10 minutes of CDK deploy.

### 6.5 KMS Request Fees

| Metric | Value |
|---|---|
| Unit price | $0.03 per 10,000 requests (after 20k/mo free) |
| Expected at 10k tx/mo | ~$0.30/mo per env |
| Cost driver | Decrypt + GenerateDataKey calls; reduced by Secrets Manager cache |

Bounded at our scale.

### 6.6 CloudFront 12-Month Free Tier Expiry

| Metric | Value |
|---|---|
| Free tier | 1 TB egress + 10M requests for 12 months |
| Step-up at month 13 (modest widget egress) | +$1-3/mo |
| Cliff potential | Bot abuse of widget URL could push egress to 10 TB/mo = $850/mo |

Mitigation: CloudFront rate limiting via WAF (when enabled) or CloudFront Functions.

### 6.7 NAT Gateway

Not present today. Adding one would cost $33/AZ/month + data processing. Avoid at all costs at this scale.

---

## 7. Architectural Trade-offs

### 7.1 No VPC vs PrivateLink

| Path | Monthly cost both envs | Security |
|---|---|---|
| **Chosen: Public AWS API egress (no VPC)** | **$0** | IAM is the boundary |
| Rejected: PrivateLink endpoints (9 × 2 AZs × $7.30) | $264 fixed | No internet egress from VPC Lambdas |

**Savings: $264/mo.** Re-evaluate if RDS-in-VPC migration becomes necessary (100k+ tx/mo or PCI need).

### 7.2 Neon vs Aurora Serverless v2

| Path | Monthly cost both envs |
|---|---|
| **Chosen: Neon free tier** | **$0** |
| Rejected (deferred): Aurora Serverless v2 0.5 ACU min × 2 envs | ~$86 |

**Savings: $86/mo.** Re-evaluate when Neon free tier exhausts (~100k tx/mo).

### 7.3 API Gateway HTTP API vs CloudFront + Function URL

| Path | Cost both envs | Latency overhead |
|---|---|---|
| **Chosen: API Gateway HTTP API** | $0.02 at 10k tx/mo | ~15 ms |
| Rejected: Lambda Function URL + CloudFront + WAF | $0.50 (CloudFront) | ~55 ms |

Decision was made on latency (Razorpay 500 ms SLA), not cost.

### 7.4 Separate AWS Accounts vs Single Account

| Path | Monthly cost difference |
|---|---|
| **Chosen: 2 standalone accounts (no Org)** | $0 net |
| Rejected: Single account with naming separation | $0 net but loses blast-radius isolation |
| Rejected: 3 accounts under Org | $0 net but Mgmt account overhead for solo |

No cost delta. Multi-account decision made on blast radius and quota isolation.

### 7.5 Lambda Container Image vs Zip

| Path | Monthly cost both envs |
|---|---|
| **Chosen: Container image (x86_64)** | ECR storage ~$0.30/mo |
| Rejected: Zip + Layers | $0 storage |

**Cost premium: ~$0.30/mo.** Decision made on reproducibility (asyncpg compiled wheels), not cost.

### 7.6 x86_64 vs ARM64 (Graviton)

| Path | Cost |
|---|---|
| **Chosen: x86_64** | Baseline Lambda billing |
| Rejected: ARM64 (Graviton) | ~20% cheaper Lambda compute |

**Cost premium: trivial at this scale.** Decision made on build simplicity (no cross-arch wheels).

### 7.7 No DR Standby vs Aurora Global Database

| Path | Monthly cost |
|---|---|
| **Chosen: Neon PITR + weekly S3 dump** | <$1 |
| Rejected: Aurora Global Database warm standby | $80+/mo |

**Savings: $80+/mo.** Re-evaluate when RTO <1h becomes a hard requirement.

### 7.8 WAF Off vs WAF On

| Path | Cost both envs |
|---|---|
| **Chosen at <1000 tx/mo: WAF off; free alternatives (API Gateway throttle, resource policy IP allow-list, HMAC verify in app, Reserved Concurrency cap, Shield Standard)** | $0 |
| When 1000+ tx/mo or attack signal: WAF on | +$22/mo |

**Defer-cost: $22/mo until triggered.**

### 7.9 Inspector + AWS Config + GuardDuty + Security Hub

All off by default. Defer-cost combined: ~$30-50/mo. Enable only when compliance audit demands.

---

## 8. Dashboards and Monitoring

Two dashboards ship with the system.

### 8.1 Application Mission Control

The existing `example/frontend/admin-dashboard.html`, deployed alongside the API. Reachable at `https://api.example.com/admin/dashboard` (gated by `X-Admin-Api-Key`).

**Business view.** Open daily to verify payments are flowing.

| Widget | Source |
|---|---|
| KPI strip (today: total / paid / fulfilled / failed / in-flight / refunded) | `/admin/dashboard-data` aggregated endpoint |
| Payment Journey (17-step state graph, timestamp-driven) | `/admin/orders/{id}` |
| Queue depths (4 queues) | SQS `GetQueueAttributes` via backend |
| Stuck paid orders (>15 min) | DB query in backend |
| Reconciliation mismatches (7d) | `reconciliation_mismatch_count{reason}` |
| Stale data banner | Frontend detects refresh failure |

Cost: $0 incremental (served by existing Lambda + S3).

### 8.2 CloudWatch Dashboard

`razorpay-checkout-{env}-mission-control`, built by CDK. Free tier covers first 3 dashboards per account.

**Infra view.** Glance weekly to verify substrate health.

| Widget | Source |
|---|---|
| Webhook latency p50/p95/p99 | `webhook_latency_ms` metric |
| Lambda invocations + error rate per function | Lambda built-in metrics |
| SQS queue depth + DLQ depth | SQS built-in metrics |
| KMS request count | KMS built-in metric (sanity check) |
| Cost projection (current month) | AWS Budgets actual + forecast |
| Active alarms | Composite alarm widget |

Cost: $0 (within free tier).

### 8.3 Alarm routing

| Severity | Channel |
|---|---|
| Critical (page) | SNS topic `razorpay-{env}-ops-page` → email subscription |
| Warning (warn) | SNS topic `razorpay-{env}-ops-warn` → email subscription |

Both topics created in `foundation.py`. Email subscription confirmed during Phase 8. Cost: SNS is $0 fixed; per-message cost negligible.

### 8.4 Cost monitoring

| Tool | Purpose | Cost |
|---|---|---|
| AWS Budgets | Monthly spend alarm at 50/80/100% | Free (first 2 budgets per account) |
| Cost Anomaly Detection | Service-level anomaly detector | Free |
| Cost Explorer | Manual review with tag filters | Free |

---

## 9. Cost Ownership Map

Which engineering decision drives which cost line:

| Line item | Owner |
|---|---|
| WAF rules and Web ACL count | Security review |
| KMS CMK count | Architecture |
| Secrets Manager secrets | Tenant onboarding |
| Lambda function count | Architecture (each new fn adds Inspector $1.50 too when enabled) |
| Lambda Provisioned Concurrency | SRE (SLA tradeoff) |
| CloudWatch alarm count | SRE (alert hygiene) |
| AWS Config rule count | Compliance (when enabled) |
| GuardDuty findings volume | Operates itself |
| Security Hub standards enabled | Compliance |
| Inspector enrollment | Security |
| ECR image retention | DevOps (lifecycle policy) |
| CloudWatch Logs retention | SRE (debugging vs cost) |
| S3 log archive retention | Compliance (Object Lock 7-year is default) |
| CloudFront cache behaviors | Frontend (widget release cadence) |
| API Gateway throttle limits | SRE (abuse vs legitimate burst) |

Any PR touching these lines must update this file or attest no cost impact.

---

## 10. Appendix: By-Service Quick Reference

| Service | Today monthly (both envs) | Pricing reference |
|---|---|---|
| Lambda | <$1 | <https://aws.amazon.com/lambda/pricing/> |
| SQS | <$1 | <https://aws.amazon.com/sqs/pricing/> |
| API Gateway HTTP API | <$1 | <https://aws.amazon.com/api-gateway/pricing/> |
| CloudFront | $0 (free tier first 12 months) | <https://aws.amazon.com/cloudfront/pricing/> |
| S3 | <$1 | <https://aws.amazon.com/s3/pricing/> |
| ECR | <$1 | <https://aws.amazon.com/ecr/pricing/> |
| KMS | $10 (10 CMKs) | <https://aws.amazon.com/kms/pricing/> |
| Secrets Manager | $3.20 (4 secrets × 2 envs) | <https://aws.amazon.com/secrets-manager/pricing/> |
| CloudWatch Logs | <$1 (free tier) | <https://aws.amazon.com/cloudwatch/pricing/> |
| CloudWatch Metrics + Alarms | $1 | same |
| CloudWatch Dashboards | $0 (free tier) | same |
| CloudTrail | $0 + <$1 S3 | <https://aws.amazon.com/cloudtrail/pricing/> |
| Route 53 | $0 (using Cloudflare for DNS) | <https://aws.amazon.com/route53/pricing/> |
