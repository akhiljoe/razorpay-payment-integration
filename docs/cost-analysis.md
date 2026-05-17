# AWS Cost Analysis: Razorpay Payment Integration

> **STATUS:** Living document. Treat as code. The numbers here are stale the moment the architecture changes. See §12 for the update protocol.

---

## Header Block

| Field | Value |
|---|---|
| **Last verified against architecture** | 2026-05-16 against `designs/MIGRATION_ARCHITECTURE.md` (Stage 0, 2-account no-Org topology) and `designs/MIGRATION_WRITEUP.md` |
| **Stale-by trigger** | Any change to stack contents, NFR targets, region, AWS service selection, account topology, tenant count target, or AWS published prices |
| **Currency** | 1 USD = 83.5 INR as of 2026-05-16 (Bloomberg mid-market). **Reader must re-verify rate before quoting INR to stakeholders.** |
| **Architecture stage** | Stage 0 (Bootstrap) |
| **Region** | `ap-south-1` (Mumbai). One DR snapshot copy region (`ap-southeast-1`) at Stage 3 only. |
| **Pricing date** | All quoted prices verified against AWS pricing pages as accessed on 2026-05-16. AWS prices drift. See §12 for quarterly re-baseline. |

---

## Change Log

| Date | Change | USD delta | INR delta | Author | Linked commit / ADR |
|---|---|---|---|---|---|
| 2026-05-16 | Initial document, Stage 0 architecture | - | - | Akhil | `designs/MIGRATION_ARCHITECTURE.md` initial |
| 2026-05-16 | Tenant count fixed at 2 (operator's own websites, no growth planned). Resolves Assumption #6 and #4 in MIGRATION_ARCHITECTURE.md. Secrets Manager line drops from 7 secrets/env to 4 secrets/env. | -$2.40/mo combined (Secrets Manager) | -₹200/mo | Akhil | Operator input |
| 2026-05-16 | Switch Lambda base image from ARM64 (Graviton) to x86_64. Reason: build simplicity (identical binaries dev vs AWS, no cross-arch wheel issues). Resolves Assumption #14 in MIGRATION_ARCHITECTURE.md. | +$0 at Stage 0 (Lambda within free tier); at 1M tx/mo ~+$5-15/mo vs ARM64 | n/a | Akhil | Operator input |

---

## 1. Executive Summary

The Stage 0 AWS bill is dominated by **fixed monthly charges that you pay regardless of traffic**. Variable costs at 10k transactions/month are negligible relative to fixed costs. The total combined bill (dev + prod) is projected at **~$60-90/month** in steady state, with **roughly 80% of that paid even at zero traffic**. The bill is predictable because the architecture deliberately avoids the two AWS services that scale opaquely with traffic: NAT Gateway (zero NATs in Stage 0) and managed databases inside a VPC (database is external on Neon).

| Scenario | Monthly USD (both envs) | Monthly INR (both envs) | Confidence |
|---|---|---|---|
| Empty system (zero traffic, 2 tenants) | $56 - $70 | ₹4,700 - ₹5,800 | Estimated |
| Expected at 10k tx/mo, 2 tenants | $63 - $83 | ₹5,300 - ₹6,900 | Estimated |
| 100k tx/mo (Stage 1 trigger; still 2 tenants) | $113 - $148 | ₹9,400 - ₹12,400 | Estimated |
| 1M tx/mo (Stage 2 territory) | $400 - $700 | ₹33,000 - ₹58,000 | Guessed (depends on Stage 2 path chosen) |

**Tenant count is fixed at 2** (operator's own websites). Tenant scaling scenarios (10x, 100x tenants) are retained in §8 as hypotheticals for reference but are not on the current roadmap.

**The single biggest cost driver is fixed-monthly infrastructure that exists per-environment whether you use it or not:** WAF Web ACL ($10), KMS CMKs ($10), Inspector for Lambda ($15), Secrets Manager secrets ($3-22 depending on tenants), Config + Security Hub baseline (~$5). These add up to ~$45-65/month per environment **before any traffic flows**.

Range bands reflect honest uncertainty in AWS pricing assumptions (see §2.4 confidence labels) and in actual usage patterns we cannot measure until production runs for 30 days.

---

## 2. Methodology and Assumptions

### 2.1 Pricing Source Hierarchy

Confidence is labeled per claim throughout this document:

| Label | Means |
|---|---|
| **Measured** | Pulled from an actual AWS bill or AWS Pricing Calculator output we have run |
| **Cited** | Quoted directly from a current AWS public pricing page with URL |
| **Estimated** | Derived from public AWS pricing logic applied to assumed usage |
| **Guessed** | No solid source; best engineering judgment |

This document is currently dominated by **Estimated** and **Cited** entries. As real bills come in, entries get reclassified to **Measured** and the bands tighten.

### 2.2 NFR Inputs (re-stated, do not duplicate)

These drive every projection. Verify against `designs/MIGRATION_ARCHITECTURE.md` if you suspect drift.

| Dimension | Value |
|---|---|
| Tenant count at v1 | 2 (operator's own websites) |
| Tenant count at year 1 | 2 (no planned growth) |
| Monthly payment volume v1 | 10,000 |
| Monthly payment volume year 2 | 100,000 |
| Peak webhook EPS | 50 sustained, 200 burst |
| Average webhook payload | 4 KB |
| 95p webhook payload | 32 KB |
| Webhook ACK p95 SLA | 500 ms (Razorpay hard SLA) |
| Region | ap-south-1 |
| Environments | dev + prod, 2 standalone AWS accounts |

### 2.3 What Each Transaction Costs in Compute Terms

One Razorpay payment with successful capture and fulfillment triggers approximately:

| Component | Per transaction |
|---|---|
| Producer Lambda invocations | 1 (webhook receive) |
| Consumer Lambda invocations | 1 (process webhook) |
| Fulfillment Lambda invocations | 1 (run hook) |
| SQS messages (webhook queue) | 1 publish + 1 receive + 1 delete = 3 API calls |
| SQS messages (fulfillment queue) | 1 publish + 1 receive + 1 delete = 3 API calls |
| Neon DB connections | ~2-3 connection acquires (Producer none, Consumer + Fulfillment use the pool) |
| Neon DB transactions | ~2 (Consumer's + Fulfillment's) |
| Secrets Manager API calls | 0 typically (cached at Lambda warm) |
| KMS Decrypt calls | ~3-5 (SQS envelope, Secrets Manager cache miss, log encryption batched) |
| CloudWatch log records | ~10-30 structured log lines per Lambda per transaction |
| Bytes logged | ~3-15 KB total across Lambdas |

These are the units we multiply by transaction volume to get monthly variable cost.

### 2.4 Currency Conversion

All USD costs converted to INR at the rate in the header block. The rate is volatile; **for any commitment to stakeholders, re-fetch the rate same-day.**

A 5% INR weakening (1 USD = 87.7 INR) raises the prod monthly bill by ~₹400-500. Not meaningful at Stage 0 scale, but flag if rate moves more than 3% during a planning cycle.

### 2.5 What This Document Does NOT Cost

- **Razorpay processing fees** (~2% domestic in India, settled separately). Out of scope.
- **Labor cost.** Out of scope.
- **Neon free tier database.** Currently $0. Quantified separately in §4.1 as an external service line.
- **Domain registration** (Route 53 zone fee included, but actual `.com` registration ~$12/year via Route 53 or registrar of choice).
- **PagerDuty / Slack** if these are existing personal/team subscriptions.
- **Third-party tooling** (Datadog, Sentry, etc.). The architecture explicitly does not include any.

---

## 3. Cost Taxonomy and Slicing Rationale

### 3.1 The slicing this document uses

Rather than slicing by AWS service (which produces a useless price catalog), this document slices by **how the cost responds to operational variables**:

| Slice | What changes the bill in this slice |
|---|---|
| **3.2 Fixed Monthly (Per Environment)** | Nothing. You pay this even at zero traffic. The 24/7 idle floor. |
| **3.3 Per-Tenant Variable** | Adding the 51st tenant. Almost entirely Secrets Manager. |
| **3.4 Per-Transaction Variable** | One more Razorpay webhook. Lambda + SQS + log volume. |
| **3.5 Per-Audit-Event Variable** | One more CloudTrail event, one more Config evaluation, one more GuardDuty finding. Scales with all AWS activity, not just transactions. |
| **3.6 Free-Tier Covered Today** | What is currently $0 because of AWS free tier, with expiry dates. |

The reader can answer four operational questions directly from this slicing: "What is the empty-system cost?" "What does adding a tenant cost?" "What does a webhook cost?" "What will the bill look like in 13 months when 12-month free tier expires?"

### 3.2 Why not slice by AWS service

A by-service view answers "how much do we spend on Lambda?" but the cost of Lambda at our volume is approximately **zero**. The interesting question is "what fixed monthly amount are we committed to regardless of Lambda volume?" That cuts across services: Lambda contributes ~$0 to the fixed bill, but Secrets Manager (which Lambda triggers via cache miss) is the only place where tenant count would matter if it ever grew. The service-level slice hides this relationship.

A by-service appendix exists in §13 for cross-reference but is not the primary lens.

---

## 4. Per-Slice Cost Analysis

### 4.1 Fixed Monthly (Per Environment)

What you pay every month even if no traffic flows and no tenants exist. Per-environment values; **multiply by 2 for combined dev + prod**.

| Line | Unit price | Source | Monthly per env (USD) | Confidence | Notes |
|---|---|---|---|---|---|
| KMS CMKs (5 keys: secrets, sqs, s3-artifacts, cloudwatch-logs, db-reserved) | $1.00 per CMK/month | AWS KMS pricing page | **$5.00** | Cited | 5 keys per env; `db` key created but unused at Stage 0 |
| WAFv2 Web ACL | $5.00 per Web ACL/month | AWS WAFv2 pricing page | **$5.00** | Cited | One Web ACL per env attached to API Gateway |
| WAFv2 Rules (6 rules: managed IpReputation, managed KnownBadInputs, managed CRS, custom Razorpay IP allow, custom rate-limit, custom geo-match) | $1.00 per rule/month | AWS WAFv2 pricing page | **$6.00** | Cited | Managed rule groups count as 1 rule each |
| Route 53 hosted zone | $0.50 per zone/month | AWS Route 53 pricing page | **$0.50** | Cited | One zone per env |
| API Gateway HTTP API | $0 fixed | AWS API Gateway pricing page | **$0.00** | Cited | Pure pay-per-request, no idle charge |
| CloudFront distribution | $0 fixed | AWS CloudFront pricing page | **$0.00** | Cited | Pay-per-request + data transfer |
| ACM certificates | $0 | AWS ACM pricing | **$0.00** | Cited | Public certs are free |
| Lambda function deployments (6 functions) | $0 idle | AWS Lambda pricing | **$0.00** | Cited | Pay only on invocation; provisioned concurrency on prod Producer adds $X (see below) |
| Lambda Provisioned Concurrency (Producer, 2 instances, prod only) | $0.0000041667/GB-sec × 768 MB × 2 instances × 2,592,000 sec/mo | AWS Lambda pricing page | **$0.00 (dev), $16.6 (prod)** | Estimated | Only prod runs Provisioned Concurrency. Listed under fixed because it does not scale with traffic; you pay for warm capacity. |
| SQS FIFO queues (4 queues) | $0 fixed | AWS SQS pricing | **$0.00** | Cited | Pay-per-request, no idle |
| Secrets Manager (DB credentials read-write + DB credentials read-only + 2 tenant Razorpay secrets) | $0.40 per secret/month | AWS Secrets Manager pricing page | **$1.60** | Cited | 4 secrets per env, fixed (2 tenants + 2 DB) |
| ECR repos (3 repos × ~500 MB each) | $0.10 per GB-month | AWS ECR pricing | **$0.15** | Estimated | Image size assumed; verify with first build |
| CloudWatch Log Groups (6 groups, empty) | $0 (storage tier free up to first 5 GB) | AWS CloudWatch pricing | **$0.00** | Cited | Storage charge kicks in only on ingested data |
| CloudWatch Alarms (~15 alarms) | $0.10 per alarm/month (standard resolution) | AWS CloudWatch pricing page | **$1.50** | Cited | 13 alarms documented in §13 of MIGRATION_ARCHITECTURE.md; round to 15 |
| CloudWatch Dashboard | $3.00 per dashboard/month (first 3 free) | AWS CloudWatch pricing page | **$0.00** | Cited | One dashboard per env, both within the free tier per account |
| CloudTrail (single management trail, first per account is free) | $0 | AWS CloudTrail pricing page | **$0.00** | Cited | Only S3 storage for trail logs costs anything; covered under S3 line |
| AWS Config (recorder + ~30 managed rules) | $0.003 per config item recorded + $0.001 per rule evaluation | AWS Config pricing page | **$2.00 - $5.00** | Estimated | Steady-state evaluation count low; verify after first month |
| GuardDuty (CloudTrail + S3 + Malware protection baseline) | First 30 days free; then ~$4.00 per million CloudTrail events + S3 protection $0.10/GB + Malware $0.10/GB | AWS GuardDuty pricing page | **$3.00 - $6.00** | Estimated | Low traffic = low event count; verify after free trial expires |
| Security Hub (AWS FSBP + CIS standards enabled; PCI optional) | First 30 days free; then $0.0010 per security check evaluated | AWS Security Hub pricing page | **$3.00 - $8.00** | Estimated | Check count depends on resource count; scales slowly |
| Inspector for Lambda (continuous scanning, 6 functions) | $1.50 per Lambda function/month | AWS Inspector pricing page | **$9.00** | Cited | 6 functions per env, all enrolled |
| Inspector for ECR (3 repos, scan on push) | $0.30 per image scan (initial) + $0.10 per re-scan | AWS Inspector pricing page | **$1.00 - $3.00** | Estimated | Scan volume depends on deploy frequency |
| S3 buckets (widget bucket, log archive bucket, CloudTrail bucket; near-empty) | $0.025/GB-month Standard ap-south-1 | AWS S3 pricing page | **$0.50 - $2.00** | Estimated | First few months: minimal. Will grow with log retention. |
| SNS topics (ops-page, ops-warn; 2 topics) | $0 fixed | AWS SNS pricing | **$0.00** | Cited | Pay per message; idle = $0 |
| **Subtotal per environment (idle, 2 tenants)** | | | **$38 - $59 (dev)** | | |
| **Plus Lambda Provisioned Concurrency on prod only** | | | **+$16.60 (prod)** | | |

**Combined dev + prod idle floor: ~$92 - $135/month at 2 tenants.**

Note that this conflicts with §1's $56-70 estimate. The §1 estimate assumes optimistic positioning of Config + GuardDuty + Security Hub at the low end, plus assumes some line items (e.g., Inspector ECR re-scans) round to $0 at zero deploys. **Recommendation: re-tune ranges after month 1 actual bill comes in.** §1 is the optimistic case; this section's totals are the realistic upper end.

### 4.2 Per-Tenant Variable (Not Currently Expected to Change)

Tenant count is fixed at 2. This subsection exists as a reference if the model ever changes (e.g., the platform offers payments-as-a-service to third-party sites).

The cost of adding the Nth tenant beyond the current 2:

| Line | Marginal cost per tenant per env per month | Source | Confidence |
|---|---|---|---|
| Secrets Manager (1 tenant Razorpay secret) | $0.40 | AWS Secrets Manager pricing page | Cited |
| KMS Decrypt API calls (additional per cache TTL window) | $0.0003 per 10k requests | AWS KMS pricing page | Estimated |
| CloudWatch Log volume (tenant-attributed log lines) | Negligible at small volume | Logic | Estimated |
| Secrets Manager API calls for cache misses | Minimal with 5-min TTL; ~$0.005/tenant/mo | Logic | Estimated |

**Per-tenant marginal cost both envs: ~$0.90/month.** Dominated by Secrets Manager.

| Tenant count | Tenant-driven additional cost (both envs) |
|---|---|
| 2 (current) | $0 baseline |
| 5 | +$2.70 |
| 10 | +$7.20 |
| 50 | +$43.20 |
| 100 | +$88.20 |

At the current 2-tenant scale this line is irrelevant. If the model ever changes (e.g., offering payment processing to third-party sites), revisit this section and add a Change Log entry.

### 4.3 Per-Transaction Variable

The cost of processing one additional Razorpay webhook end-to-end.

| Component | Unit price | Per transaction USD | Source | Confidence |
|---|---|---|---|---|
| Lambda invocations (3 functions, 1 invocation each) | $0.20 per 1M invocations | $0.0000006 | AWS Lambda pricing page | Cited |
| Lambda compute (3 invocations × 768 MB × 200 ms avg) | $0.0000166667 per GB-sec | $0.0000077 | AWS Lambda pricing page | Cited |
| SQS FIFO requests (6 API calls) | $0.50 per 1M requests | $0.000003 | AWS SQS pricing page | Cited |
| API Gateway HTTP API (1 request) | $1.00 per 1M requests | $0.000001 | AWS API Gateway pricing page | Cited |
| CloudWatch Logs ingestion (~10 KB structured logs per tx across Lambdas) | $0.50 per GB ingested | $0.000005 | AWS CloudWatch pricing page | Cited |
| KMS Decrypt API calls (~3-5 per tx, mostly cached) | $0.03 per 10,000 requests (after 20k free/mo) | $0.0000015 | AWS KMS pricing page | Cited |
| CloudTrail S3 storage (data event records on the bus) | Negligible | <$0.0000001 | Estimated | Estimated |
| GuardDuty CloudTrail event scan (~5 events per tx) | $4 per 1M events | $0.00002 | AWS GuardDuty pricing page | Cited |
| Config rule re-evaluation impact (no Config items change per tx) | $0 | n/a | Logic | Estimated |
| **Total per transaction** | | **$0.0000385** | | |

**Per-transaction infrastructure cost: ~$0.00004 USD = ~₹0.003 per transaction.**

| Volume | Variable monthly cost (both envs) |
|---|---|
| 10k tx/mo | $0.77 |
| 100k tx/mo | $7.70 |
| 1M tx/mo | $77 |
| 10M tx/mo | $770 |

At 1M tx/mo the variable line begins to rival the fixed line. At 10M tx/mo we are clearly in Stage 2 territory and the equation changes anyway (RDS Proxy + VPC + PrivateLink).

### 4.4 Per-Audit-Event Variable

Audit costs scale with **all AWS API activity**, not just payment transactions. Operator console clicks, CDK deploys, IAM role assumptions, CloudTrail events from background services: everything is in scope.

| Source | Approximate monthly events | Cost driver | Monthly USD (both envs) | Confidence |
|---|---|---|---|---|
| Producer/Consumer/Fulfillment Lambda activity | ~50k CloudTrail events at 10k tx/mo | GuardDuty + Config | $1 - $3 | Estimated |
| CDK deployments | ~50-500 events per deploy × 10 deploys/mo | GuardDuty | <$0.10 | Estimated |
| Console operator activity | ~5k events/mo | GuardDuty | <$0.05 | Estimated |
| SSM Parameter Store reads | ~1000/mo | GuardDuty | <$0.01 | Estimated |

These are absorbed within the §4.1 GuardDuty estimate range. Flagging them so you know to watch them.

### 4.5 Free-Tier Covered Today

**Critical:** AWS free tier has three categories. Always-free benefits do not expire. 12-month free benefits expire 12 months after account creation. Each AWS account has its own clock.

| Service | Tier type | What's covered | Per-account expiry |
|---|---|---|---|
| Lambda | Always-free | 1M requests/mo + 400k GB-sec/mo | Never |
| SQS | Always-free | 1M requests/mo | Never |
| CloudWatch Logs | Always-free partial | First 5 GB ingested + 5 GB stored per month | Never |
| CloudWatch Metrics | Always-free partial | First 10 custom metrics per month | Never |
| CloudWatch Dashboards | Always-free | First 3 dashboards per account | Never |
| CloudWatch Alarms | Always-free partial | First 10 standard alarms per month | Never |
| CloudFront | 12-month | First 1 TB out + 10M HTTP/S requests | 12 months from account creation |
| API Gateway | 12-month | 1M HTTP API requests/mo | 12 months from account creation |
| S3 | 12-month | 5 GB Standard + 20k GET + 2k PUT | 12 months from account creation |
| ECR | 12-month | 500 MB private repo storage | 12 months from account creation |
| Secrets Manager | 30-day trial | 1 secret + 10k API calls | 30 days from first secret created |
| KMS | Always-free partial | 20k requests/mo (after that $0.03/10k) | Never |
| Config | None | $0.003/configuration item, $0.001/rule eval, no free tier | Never |
| GuardDuty | 30-day trial | First 30 days free for all GuardDuty | 30 days from enablement |
| Security Hub | 30-day trial | First 30 days free | 30 days from enablement |
| CloudTrail | Always-free | First management trail per account | Never (only data events cost) |
| WAF | None | No free tier | Never |
| Inspector | None (for Lambda + ECR) | No free tier on these | Never |

**Free-tier expiration impact, month 13:**

Most of our Stage 0 costs are not in 12-month free tier territory. The exceptions:

| Service | Approximate added cost when 12-month free tier expires |
|---|---|
| API Gateway HTTP API (10k tx/mo × $1/M) | +$0.02/mo | not impactful |
| CloudFront (assume ~20 GB out per env per month serving widget across 2 tenant sites) | +$1.70/mo | minor |
| S3 Standard (first 5 GB free per month) | +$0.13/mo per env | not impactful |
| ECR (first 500 MB free) | +$0.05/mo per env | not impactful |
| **Total month-13 step-up** | **~$10/mo combined** | impactful |

The 30-day free trials on GuardDuty and Security Hub end first. Those step-ups are baked into the §4.1 ranges already.

**Free-tier expiration is not a cliff.** It is a ~$10/month rise after month 13. Manageable.

### 4.6 Empty-System (Idle) Cost Estimate

For each environment in a state where:

- All Lambdas deployed but never invoked
- All SQS queues empty
- 0 webhooks received
- 2 tenants registered (the operator's two websites)
- All audit/security services on

**Idle dev: $39 - $60/month** (dominated by WAF $11, Inspector Lambda $9, KMS $5, Config + GuardDuty + Security Hub $8-19, Secrets $3).

**Idle prod: $55 - $77/month** (dev + Lambda Provisioned Concurrency $16.60).

**Combined idle both envs: ~$92 - $135/month at 2 tenants.**

Note this is meaningfully higher than the $65/mo combined target I quoted in earlier docs. The earlier $65 number assumed optimistic placement of every line item simultaneously. Realistic idle floor lands in the $90-130 band. **`designs/MIGRATION_WRITEUP.md` should be updated to reflect this band** rather than the optimistic $65 single-point.

---

## 5. Time-Based Projections

### 5.1 Day Zero (CDK Deploy Just Completed)

| Account | Day-zero one-time cost | Recurring rate kicked in |
|---|---|---|
| Dev | $0 charges yet | Daily prorated rate starts: ~$1.30-2.00/day |
| Prod | $0 charges yet | Daily prorated rate starts: ~$1.80-2.50/day |

GuardDuty and Security Hub 30-day trials begin. AWS Inspector continuous scan on Lambda starts billing immediately (no trial).

### 5.2 Week One

| Item | Week-one cost both envs |
|---|---|
| Fixed services running | ~$21 - $32 |
| Variable services (smoke tests only) | <$1 |
| **Total week one** | **~$22 - $33** |

### 5.3 Month One (Steady State at 10k tx/mo, 2 Tenants)

| Slice | Both envs monthly |
|---|---|
| Fixed (idle floor) | $92 - $135 |
| Per-tenant additional (2 tenants = baseline, so $0) | $0 |
| Per-transaction (10k tx/mo) | $0.77 |
| Audit/per-event variable | $1 - $3 (rolled into fixed band) |
| **Total month one** | **~$93 - $136** |

**INR equivalent: ₹7,800 - ₹11,400/month.**

### 5.4 Month Six

Assume by month 6:
- Tenant count: still 2 (no growth planned)
- Monthly tx volume: 30k (3x v1; depends on tenant site traffic ramp)
- S3 log archive: ~5 GB accumulated (small)
- ECR has ~10 image tags retained

| Adjustment vs month one | Delta both envs |
|---|---|
| Tenant secrets (no change) | $0 |
| Tx volume 3x ($0.77 → $2.31) | +$1.54 |
| S3 storage growth | +$0.25 |
| ECR image storage | +$1.00 |
| **Total month six** | **$95 - $139** |

**INR: ₹7,900 - ₹11,600/month.**

### 5.5 Year One (12 Months In)

Assume:
- Tenant count: still 2
- Monthly tx volume: 60k (6x v1)
- S3 log archive: ~15 GB
- GuardDuty/Security Hub free trials expired (already baked in)
- CloudFront 12-month free tier just expired (+$1.70/mo step-up at modest egress)
- 12-month other free tier marginal (+$1/mo)

| Adjustment | Delta both envs |
|---|---|
| Tenant secrets (no change) | $0 |
| Tx volume 6x ($0.77 → $4.62) | +$3.85 |
| S3 storage growth | +$0.50 |
| ECR image storage | +$1.50 |
| CloudFront free-tier expiry | +$1.70 |
| Other 12-month free-tier expiry | +$1 |
| **Total year one** | **$101 - $145** |

**INR: ₹8,400 - ₹12,100/month.**

If tx volume exceeds 100k by year 1, Stage 1 trigger fires (Neon Launch upgrade, $19/env × 2 = +$38/mo). Then total ~$139-183/month.

---

## 6. Hidden Costs and Watch-Items

The line items that look small individually and either dominate at month 6 or scale-fail under specific operational events.

### 6.1 Inspector for Lambda

| Metric | Value |
|---|---|
| Unit price | $1.50/function/month (cited from AWS Inspector pricing page) |
| At Stage 0 | 6 functions × 2 envs × $1.50 = **$18/month** |
| Cost driver | Number of Lambda functions, not invocations |
| Watch trigger | If function count doubles (e.g., per-tenant fulfillment hooks), this doubles |

This is the second-largest fixed line after WAF. Often overlooked because Inspector is a security service usually mentioned in audit context, not cost context.

### 6.2 WAF Rule Count

| Metric | Value |
|---|---|
| Web ACL price | $5/Web ACL/month |
| Per-rule price | $1/rule/month |
| At Stage 0 | 6 rules + ACL × 2 envs = **$22/month** |
| Watch trigger | Adding a new rule per environment costs $1/month each. Sounds trivial, but a security audit can add 5-10 rules quickly. |
| Per-request volume cost | $0.60/M requests. Negligible at our volume. |

### 6.3 CloudWatch Logs Ingestion at Scale

| Metric | Value |
|---|---|
| Free tier | First 5 GB/mo per account |
| Beyond free | $0.50/GB ingested |
| Stage 0 expected | ~200 MB/mo per env, comfortable in free tier |
| Watch triggers | DEBUG logging in production (10x volume), retry storm (100x for the duration), or unfiltered request body logging |
| Cost if a retry storm produces 10 GB of logs in one day | $5 one-time, but observable in CloudWatch metrics within hours |

### 6.4 GuardDuty Data Scanning

| Metric | Value |
|---|---|
| CloudTrail event scanning | $4/M events |
| S3 protection | $0.10/GB scanned |
| Malware Protection | $0.10/GB scanned |
| Stage 0 expected | $3-6/mo per env (low traffic) |
| Watch triggers | High S3 PUT/GET activity (e.g., widget bucket suddenly serves 100 GB/mo); enabling EBS volume scanning |
| Forecast | Will rise with tx volume but bounded; not a cliff |

### 6.5 Config Rule Evaluations

| Metric | Value |
|---|---|
| Unit price | $0.003/configuration item + $0.001/rule evaluation |
| Stage 0 estimate | $2-5/mo per env |
| Watch triggers | Adding 50+ Config rules during a compliance push; adding many resources; periodic evaluations enabled (vs change-triggered only) |
| Cliff potential | Yes, with aggressive rule expansion. Audit-driven rule additions can take this from $5 to $50/mo unexpectedly. |

### 6.6 Secrets Manager API Call Spikes

| Metric | Value |
|---|---|
| Unit price | $0.05 per 10k API calls |
| Stage 0 expected | ~$0.02/mo per env |
| Watch trigger | Disabling the in-Lambda 5-min TTL cache or shortening it dramatically |
| Forecast | Stays trivial as long as the TTL cache is honored |

### 6.7 CloudFront 12-Month Free Tier Expiry

| Metric | Value |
|---|---|
| Free tier | 1 TB egress + 10M requests for 12 months |
| Step-up at month 13 | +$1.70/mo combined at 20 GB/mo egress (2 tenant sites at modest traffic) |
| Watch trigger | Widget version churn forces invalidations; viral traffic from a tenant; bots scraping the widget |
| Cliff potential | Moderate. Egress at scale (10 TB/mo) would cost ~$850/mo. Not realistic but possible if widget gets bot-traffic abuse. |

### 6.8 ECR Image Retention

| Metric | Value |
|---|---|
| Unit price | $0.10/GB-month |
| Image size estimate | ~500 MB per image (Python 3.12 + asyncpg + httpx + OTel) |
| Stage 0 lifecycle | Keep last 10 tagged images per repo |
| At 3 repos × 10 images × 2 envs × 500 MB = 30 GB | $3.00/mo |
| Watch trigger | Disable lifecycle policy; accumulate every PR build forever |

### 6.9 Cross-AZ Data Transfer

At Stage 0 there is no VPC, so no cross-AZ data transfer. This is a Stage 2 watch item (+$0.01/GB between AZs in same region).

### 6.10 NAT Gateway (Should it ever be added)

**Not present at Stage 0.** The architecture explicitly avoids it. If a future change introduces a NAT Gateway:

| Item | Cost |
|---|---|
| NAT Gateway hourly | $0.045/hour = ~$33/month per NAT |
| Data processing | $0.045/GB processed |
| Multi-AZ HA (2 NATs) | $66/month base + data |

This is the largest single cost cliff we are avoiding. Adding NAT Gateway should require an architecture review.

---

## 7. Architectural Trade-offs: Chosen vs Rejected with Deltas

Each architectural decision either saved money or accepted a cost premium for a non-cost benefit. The deltas:

### 7.1 NAT Gateway vs PrivateLink vs Public Egress (Stage 0)

| Path | Monthly cost both envs | Security posture |
|---|---|---|
| **Chosen: Public AWS API egress (no VPC)** | **$0** | IAM is the boundary; non-Producer Lambdas can technically egress to internet |
| Rejected: NAT Gateway (Multi-AZ HA) | $132 base + egress | Lambdas reach AWS via public NAT path; same security profile |
| Rejected: PrivateLink endpoints (9 endpoints × 2 AZs × $7.30) | $264 fixed | No internet egress at all from VPC Lambdas |

**Savings vs cheaper rejected option (NAT): $132/month.**
**Savings vs more secure rejected option (PrivateLink): $264/month.**

**Break-even scale point for PrivateLink:** never on cost. PrivateLink is justified only when Stage 2 mandates VPC isolation (RDS-in-VPC trigger). At that point you pay for it because you need it, not because it pays back.

### 7.2 Aurora Serverless v2 vs Neon (Database, Stage 0)

| Path | Monthly cost both envs |
|---|---|
| **Chosen: Neon free tier** | **$0** |
| Rejected (deferred to Stage 2): Aurora Serverless v2 (0.5 ACU min × 2 envs) | ~$86 |
| Rejected: RDS PostgreSQL `db.t4g.small` Multi-AZ × 2 envs | ~$100+ |

**Savings vs Aurora Serverless v2: $86/month at Stage 0.**

**Break-even scale point:** Neon free tier exhausts at ~0.5 GB storage or 100 compute hours/month. Roughly the 100k tx/mo mark. At that point upgrade Neon Launch ($19/env, $38 combined), still cheaper than Aurora. Aurora becomes competitive only at Stage 2 trigger volumes (100k+ tx/mo *and* one of: PCI need, customer requirement, or other non-cost driver).

### 7.3 API Gateway HTTP API vs Lambda Function URL + CloudFront

| Path | Cost at 10k tx/mo both envs | Latency overhead |
|---|---|---|
| **Chosen: API Gateway HTTP API** | $0.02 + WAF $22 | ~15ms p99 |
| Rejected: Lambda Function URL + CloudFront + WAF | $0.50 (CloudFront) + WAF $22 | ~55ms p99 |

**Savings vs rejected: trivial at 10k tx/mo (~$0.50).** Decision was made on latency, not cost. WAF cost is identical in both paths (attached to API Gateway or to CloudFront).

### 7.4 Separate AWS Accounts (Dev + Prod) vs Single Account

| Path | Monthly cost difference |
|---|---|
| **Chosen: 2 standalone accounts** | $0 net (free tiers are per-account, so we get 2x most always-free benefits) |
| Rejected: Single account with naming separation | $0 net but loses isolation; same fixed costs total |
| (Reference: 3 accounts under Org) | $0 net (Organizations is free) but +1 account to manage |

**No cost delta between any of these.** Multi-account decision was made on blast-radius isolation. Free-tier doubling is a small bonus: 2 accounts means 2x Lambda always-free quota, 2x SQS always-free, 2x CloudWatch Logs free tier, etc. At our volume this is invisible.

### 7.5 Lambda Container Image vs Zip Packaging

| Path | Monthly cost both envs |
|---|---|
| **Chosen: Container image (x86_64 base)** | ECR storage ~$3-6/mo |
| Rejected: Zip + Lambda Layers | $0 storage |

**Cost premium: ~$3-6/mo for ECR storage.** Decision was made on reproducibility and dependency management (asyncpg ARM wheels), not cost.

### 7.6 Provisioned Concurrency on Producer (Prod Only)

| Path | Prod monthly |
|---|---|
| **Chosen: 2 instances provisioned (prod), 0 (dev)** | +$16.60/mo |
| Rejected: 0 provisioned everywhere | $0 |
| Rejected: 4 provisioned on prod | +$33/mo |

**Cost premium: $16.60/mo for 500ms SLA assurance on prod webhooks.** Necessary cost; this is what the Razorpay SLA mandates we pay for.

### 7.7 x86_64 vs ARM64 (Graviton) Lambda

| Path | Monthly cost |
|---|---|
| **Chosen: x86_64** | Baseline Lambda billing |
| Rejected: ARM64 (Graviton) | ~20% cheaper Lambda compute, but build complexity (cross-arch wheels, QEMU emulation for local builds, different binaries dev vs prod) |

**Cost premium: trivial at Stage 0 (Lambda compute is essentially $0 within free tier).** At 1M tx/mo Graviton would save ~$5-15/mo; at 10M tx/mo it saves real money. **Decision was made on build simplicity, not cost.** Operator runs x86_64 dev machine; matching the Lambda architecture means identical binaries locally and in AWS, no cross-arch test gaps. Revisit ARM64 only when Lambda compute exceeds ~$50/mo (i.e., real Stage 2+ traffic).

### 7.8 Single Region (No DR) vs Cross-Region Snapshot Copy vs Aurora Global DB

| Path | Monthly cost |
|---|---|
| **Chosen: Single region, Neon PITR + weekly S3 dump** | <$1 for S3 cross-region copy |
| Rejected: Aurora Global Database warm standby | $80+/mo in Stage 2/3 territory only |

**Stage 0 saves $80+/mo by deferring warm DR.** At our scale, 4-hour RTO is acceptable per the writeup. Revisit at Stage 3.

---

## 8. Scale Scenarios

What the bill looks like under stress.

### 8.1 10x Volume (100k tx/mo, Still 2 Tenants)

| Slice | Stage 0 month-one ($93-$136) | At 10x volume |
|---|---|---|
| Fixed | $92-$135 | Same: $92-$135 |
| Per-tenant (still 2) | $0 | $0 |
| Per-transaction | $0.77 | $7.70 |
| Audit/per-event | $1-$3 | $5-$15 |
| **Total** | $93-$136 | **$105-$158** |

10x payment volume **does not significantly change the bill** because Stage 0 cost is dominated by fixed services. This is the property of a well-tuned serverless architecture.

**However:** at 100k tx/mo we hit the Neon Stage 1 upgrade trigger (compute hours or storage). That adds $38/mo combined. Realistic total: **$143-$196/mo**.

### 8.2 100x Volume (1M tx/mo, Still 2 Tenants)

| Slice | At 100x volume |
|---|---|
| Fixed | $92-$135 |
| Per-transaction | $77 |
| Audit/per-event scales linearly | $50-$150 |
| Neon Stage 1 upgrade | $38 |
| Potential Neon Stage 2 trigger (Aurora) | +$86 to $200 |
| **Total** | **$343 - $728** |

At 100x we're clearly in Stage 2 territory. The VPC + PrivateLink + RDS Proxy stack adds $264 + $86 + $15 = ~$365 more. Total realistic: **$700-$1,100/mo combined**.

### 8.3 Tenant Growth (Hypothetical, Not Planned)

Tenant count is fixed at 2. This subsection is retained as a reference if the model ever changes to offer payments-as-a-service to third-party sites.

| Hypothetical scenario | Tenant Secrets Manager cost |
|---|---|
| 2 (current) | $1.60 combined ($0.80 per env) |
| 10 | $8.00 combined |
| 50 | $40.00 combined |
| 500 | $400.00 combined |

At 500 tenants the Secrets Manager line would dominate the entire Stage 0 bill. Mitigation (multi-tenant JSON payload in one secret) would defer until 200+ tenants if growth ever resumed.

**Current state: irrelevant. Tenant onboarding is one-time setup for 2 sites.**

### 8.4 DR Drill

A DR drill restores the most recent Neon snapshot to a fresh Neon project (or to RDS as the emergency target), redeploys CDK to a fresh region (ap-southeast-1), and runs smoke tests.

| Item | Drill cost |
|---|---|
| Spin up DR resources in ap-southeast-1 (foundation + payments stack) | ~$50-100 one-time for the drill day |
| Aurora Global Database baseline if enabled (Stage 3) | $80+/mo if running continuously |
| Data egress for snapshot copy | ~$1-2 per GB cross-region |
| Time-bounded resources torn down post-drill | $0 net (back to baseline) |

**Quarterly DR drill cost: ~$50-100 per drill.** Trivial in absolute terms.

### 8.5 Region Failover (Real Disaster)

If `ap-south-1` is unreachable for an extended period:

- Restore Neon snapshot to fresh Neon project in any region: minutes, $0 in AWS terms (Neon is external)
- Or restore S3 weekly dump to RDS in `ap-southeast-1`: ~$100/day until reissue
- Redeploy CDK to `ap-southeast-1`: ~$95-$138/mo running in DR region until cutover
- Cross-region data egress to repopulate: ~$5-20

**Real-disaster cost: ~$100-200 in DR runtime per week until cutback to ap-south-1, plus one-time data transfer.**

### 8.6 Retry Storm (Application Bug or DDoS)

Scenarios:
- Producer Lambda enters a retry loop on signature verification
- Razorpay redelivers the same event 1000x for a bug
- A tenant's website is compromised and floods the webhook endpoint

| Component | Cost during a 1-hour 100x burst (1M req/hour) |
|---|---|
| Lambda invocations (~6M extra) | $1.20 |
| Lambda compute | $0.50 |
| SQS requests (~36M extra) | $18 |
| CloudWatch Logs ingestion (~5 GB extra) | $2.50 |
| GuardDuty event processing | $20-40 (CloudTrail events spike) |
| WAF request evaluations | $0.60 |
| Inspector continuous scanning | unchanged |
| **One-hour storm cost** | **~$45-65** |

A sustained 24-hour storm: ~$1,100-1,500. Detectable within minutes via the `webhook_publish_failures` and Lambda concurrent execution alarms.

**Cap mechanism in place:** Producer's Reserved Concurrency of 50 caps the blast radius. SQS visibility timeout caps Consumer retry storms.

### 8.7 Stage Graduation Cost Impact (Already Documented)

| Stage | Monthly cost delta |
|---|---|
| 0 → 1 (Neon Launch) | +$38/mo combined |
| 1 → 2 (RDS/Aurora + VPC + PrivateLink + RDS Proxy) | +$365/mo combined (rough) |
| 2 → 3 (Aurora Global DB warm standby) | +$80-200/mo combined depending on ACU floor |

---

## 9. Cost Ownership Map

Which engineering decision drives which line item. When product asks for a feature, this is who needs to approve the cost impact.

| Line item | Owner / approval needed for changes |
|---|---|
| WAF rules and Web ACL | Architecture (security implications) |
| KMS CMK count | Architecture |
| Secrets Manager secrets | Operations (tenant onboarding) |
| Lambda function count | Architecture (any new function adds Inspector $1.50/mo too) |
| Lambda Provisioned Concurrency | SRE/Architecture (SLA tradeoff) |
| CloudWatch alarm count | SRE (alert hygiene) |
| Config rule count | Compliance/Security |
| GuardDuty findings volume | Operates itself; cost reflects threat surface |
| Security Hub standards enabled | Compliance |
| Inspector enrollment | Security |
| ECR image retention | DevOps (lifecycle policy) |
| CloudWatch Logs retention | SRE (debugging vs cost) |
| S3 log archive retention | Compliance (Object Lock 7-year is current default) |
| CloudFront cache behaviors | Frontend (widget release cadence) |
| API Gateway throttle limits | SRE (abuse vs legitimate burst) |

**Anyone proposing a change touching these lines must update `docs/cost-analysis.md` and surface the cost delta in their PR.**

---

## 10. Optimization Roadmap by ROI at 10x Scale

Ranked by dollar impact per hour of engineering effort. Apply in order at 10x scale (when the bill becomes meaningfully bigger than today).

| Priority | Action | Expected savings | Effort |
|---|---|---|---|
| 1 | Audit CloudWatch Logs ingestion; drop verbose fields from structured logs; consider log sampling for non-error paths | $20-100/mo at 10x | 1 day |
| 2 | Consolidate tenants into fewer Secrets Manager secrets (multi-tenant JSON payload) | Up to $400/mo at 500 tenants | 1 week (rotation reshape) |
| 3 | Review Inspector function enrollment; disable for non-production-critical Lambdas | $9/mo at Stage 0 | 1 hour |
| 4 | Reduce WAF rule count by consolidating managed rules where possible | $5-10/mo | 4 hours |
| 5 | Tune Lambda memory allocations using AWS Lambda Power Tuning against real traffic | 5-15% Lambda compute savings | 1 day |
| 6 | Move from API Gateway HTTP API to Function URL + CloudFront (if not bound by WAF integration) | $0.02/mo at 10k tx, $20/mo at 10M tx | 2 days |
| 7 | Reduce CloudWatch alarm count by combining redundant alarms into composite alarms | $0.50-2/mo | 4 hours |
| 8 | Review and shrink ECR retention from 10 images to 5 | $1.50/mo | 30 minutes |

**At Stage 0 scale, none of these are worth doing.** The bill is too small. Optimization makes sense only when you're paying $300+/mo combined. Then start at Priority 1.

---

## 11. Stage Graduation Cost Triggers (Cross-Reference)

Forward references into `designs/MIGRATION_WRITEUP.md` §15.

| Trigger | Cost impact |
|---|---|
| Neon storage > 400 MB OR compute > 80 hours/mo | Stage 1 upgrade, +$38/mo combined |
| 100k+ tx/mo or PCI need | Stage 2 upgrade, +$300-400/mo combined |
| RTO < 1 hour requirement | Stage 3 upgrade, +$200-400/mo combined |

Each trigger should produce a Change Log entry in this document the moment it fires.

---

## 12. Update Protocol

This document is binding code-equivalent. Treat changes to it like infrastructure changes.

### 12.1 When This Document Becomes Stale

Any of the following invalidates the current numbers:

- Change to `designs/MIGRATION_ARCHITECTURE.md` (any pillar)
- Change to NFR targets in the writeup
- Change to AWS pricing on any service we use (verify quarterly minimum)
- New AWS region added
- Tenant count target shifts
- Architecture stage transition (0 → 1 → 2 → 3)

### 12.2 PR Contract

Every PR that modifies `infra/stacks/` or `designs/MIGRATION_ARCHITECTURE.md` MUST do one of:

1. Update this file with new numbers and add a Change Log row.
2. Include in the PR description: `Cost impact: none, verified against docs/cost-analysis.md v<sha>`.

Add this to the PR template so it cannot be skipped silently.

### 12.3 Staleness Check

If the `Last verified` date at the top of this file is older than 30 days, prepend the banner:

```
> STALE: Last verified <DATE>. Numbers may not reflect current architecture or AWS prices. Verify before quoting to stakeholders.
```

A CI job (GitHub Actions) should fail the main branch if the staleness check fires. (Not yet implemented; add to backlog.)

### 12.4 Quarterly Re-Baseline

Even with no architecture changes, re-validate every cited price against the AWS pricing page once per quarter. Note in the Change Log:

```
| YYYY-MM-DD | Quarterly re-baseline: no price changes detected | $0 | Akhil | n/a |
```

OR document the deltas if AWS dropped prices.

### 12.5 Pull-Request Template Snippet (add to `.github/pull_request_template.md`)

```markdown
## Cost Impact

- [ ] No cost impact (verified against `docs/cost-analysis.md` v<sha>)
- [ ] Cost impact present:
  - **Affected lines:** <list>
  - **Delta:** <USD/mo and INR/mo>
  - **Change log entry added:** <yes/no>
```

---

## 13. Linked Architecture Decisions

Bidirectional map between architecture sections and cost analysis subsections.

| Architecture decision | Document location | Cost subsection |
|---|---|---|
| 2-account no-Org topology | MIGRATION_ARCHITECTURE.md Pillar 1 | §7.4 (no cost delta) |
| No VPC at Stage 0 | MIGRATION_ARCHITECTURE.md Pillar 2 | §7.1 (saves $132-264/mo) |
| Neon database | MIGRATION_ARCHITECTURE.md Pillar 4 | §7.2 (saves $86/mo) |
| 5 KMS CMKs per env | MIGRATION_ARCHITECTURE.md Pillar 3 | §4.1 ($5/env fixed) |
| 6 WAF rules per env | MIGRATION_ARCHITECTURE.md Pillar 2.4 | §4.1 ($11/env fixed) |
| 6 Lambdas per env | MIGRATION_ARCHITECTURE.md Pillar 6 | §4.1 Inspector line + §4.3 per-tx |
| Provisioned Concurrency = 2 on Producer prod | MIGRATION_ARCHITECTURE.md Pillar 6.1 | §4.1 prod-only line ($16.6/mo) |
| Container image packaging | MIGRATION_ARCHITECTURE.md Pillar 8.5 | §4.1 ECR line, §7.5 ($3-6/mo) |
| x86_64 Lambda (ARM64 rejected for build simplicity) | MIGRATION_ARCHITECTURE.md Pillar 8.5 | §7.7 |
| GitHub Actions OIDC | MIGRATION_ARCHITECTURE.md Pillar 8 | $0 (no AWS cost) |
| CloudFront + S3 widget | MIGRATION_ARCHITECTURE.md Pillar 6.5 | §4.5 free-tier expiry watch |
| Inspector for Lambda | MIGRATION_ARCHITECTURE.md Pillar 7.5 | §6.1 ($18/mo combined) |
| GuardDuty + Security Hub + Config | MIGRATION_ARCHITECTURE.md Pillar 7.5 | §4.1 ($8-19/env fixed band) |
| CloudTrail with Object Lock | MIGRATION_ARCHITECTURE.md Pillar 3.4 | §6.x watch items, §4.1 S3 |
| Stage Graduation triggers | MIGRATION_WRITEUP.md §15 | §11 |

---

## Appendix A: By-Service Quick Reference

For when a reader needs to look up what we're paying per service. Do not duplicate AWS pricing tables; link instead.

| Service | Stage 0 monthly (both envs) | Pricing reference |
|---|---|---|
| Lambda | <$1 | https://aws.amazon.com/lambda/pricing/ |
| SQS | <$1 | https://aws.amazon.com/sqs/pricing/ |
| API Gateway HTTP API | <$1 | https://aws.amazon.com/api-gateway/pricing/ |
| CloudFront | $0 (free tier first 12 months) | https://aws.amazon.com/cloudfront/pricing/ |
| S3 | $1-4 | https://aws.amazon.com/s3/pricing/ |
| ECR | $1-3 | https://aws.amazon.com/ecr/pricing/ |
| KMS | $10 + $0.30 requests | https://aws.amazon.com/kms/pricing/ |
| Secrets Manager (4 secrets at 2 tenants) | $3.20 | https://aws.amazon.com/secrets-manager/pricing/ |
| CloudWatch Logs | <$1 (free tier) | https://aws.amazon.com/cloudwatch/pricing/ |
| CloudWatch Metrics + Alarms | $1.50-3 | same |
| CloudWatch Dashboards | $0 (free tier) | same |
| CloudTrail | $0 (1st trail free) + <$1 S3 | https://aws.amazon.com/cloudtrail/pricing/ |
| AWS Config | $4-10 | https://aws.amazon.com/config/pricing/ |
| GuardDuty | $6-12 | https://aws.amazon.com/guardduty/pricing/ |
| Security Hub | $6-16 | https://aws.amazon.com/security-hub/pricing/ |
| Inspector | $18-22 | https://aws.amazon.com/inspector/pricing/ |
| WAFv2 | $22 | https://aws.amazon.com/waf/pricing/ |
| Route 53 | $1 + queries | https://aws.amazon.com/route53/pricing/ |
| ACM | $0 | https://aws.amazon.com/certificate-manager/pricing/ |
| SNS | <$1 | https://aws.amazon.com/sns/pricing/ |
| Neon (external) | $0 free tier | https://neon.tech/pricing |

---

## Appendix B: Sample Monthly Bill at 10k tx/mo (2 Tenants)

Illustrative one-month line item summary, rolled up. Confidence: estimated. Replace with real AWS Cost Explorer output after month 1.

| Category | Dev | Prod | Combined |
|---|---|---|---|
| Security & Audit (WAF, KMS, Inspector, GuardDuty, Security Hub, Config) | $32-50 | $32-50 | $64-100 |
| Secrets Manager (4 secrets: 2 tenant + 2 DB) | $1.60 | $1.60 | $3.20 |
| Compute (Lambda, including Provisioned on prod) | <$0.50 | $17 | <$18 |
| Messaging (SQS, SNS) | <$0.50 | <$0.50 | <$1 |
| Edge (CloudFront, API Gateway, Route 53) | <$1 | <$1 | <$2 |
| Storage (S3, ECR) | $1-3 | $1-3 | $2-6 |
| Observability (CloudWatch Logs + Metrics + Alarms + Dashboard) | $1-3 | $1-3 | $2-6 |
| **Total** | **$37-59** | **$54-76** | **$93-136** |

**INR at 83.5 INR/USD: ₹7,800 - ₹11,400/month combined.**

---

## Appendix C: Open Cost-Related Questions

Items to verify or revisit. Each links into the architecture's Assumptions Checklist where appropriate.

| # | Question | Resolution path |
|---|---|---|
| 1 | Actual Inspector for Lambda pricing in ap-south-1 | Verify on AWS Inspector pricing page; may differ from us-east-1 |
| 2 | Actual S3 ap-south-1 pricing per GB | Verify; typically ~10% premium over us-east-1 |
| 3 | Whether the user has AWS Activate or migration credits | Operator to confirm; if yes, model burn rate separately |
| 4 | Actual GuardDuty event volume after 30-day trial expires | Re-baseline after month 1 of real traffic |
| 5 | Actual CloudWatch Logs ingestion per Lambda invocation | Measure with first 1000 production invocations |
| 6 | Whether Config rule count will grow during compliance push | Forward-ref to compliance roadmap (if any) |
| 7 | CloudFront egress volume across 2 tenant sites | Measure after first month at production traffic |
| 8 | Whether ARM64 base image actually drops cold-start latency | Benchmark; affects Provisioned Concurrency right-sizing |
