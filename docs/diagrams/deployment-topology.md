# Deployment topology

AWS resources provisioned by `FoundationStack` (Phase 15) and `PaymentsStack` (Phase 16). What gets deployed where, and who can talk to whom.

## Free Track (default)

No VPC, all Lambdas public, ~$0/month idle.

```mermaid
flowchart TB
  subgraph AWS["AWS account · ap-south-1"]
    direction TB

    subgraph Found["FoundationStack"]
      ECR_P[("ECR<br/>razorpay-webhook-producer")]
      ECR_C[("ECR<br/>razorpay-webhook-consumer")]
      ECR_F[("ECR<br/>razorpay-fulfillment")]
      LG_P["LogGroup<br/>/aws/lambda/razorpay-webhook-producer<br/>14-day"]
      LG_C["LogGroup<br/>/aws/lambda/razorpay-webhook-consumer<br/>14-day"]
      LG_F["LogGroup<br/>/aws/lambda/razorpay-fulfillment<br/>14-day"]
      SNS["SNS Topic<br/>razorpay-ops-free"]
      Bud["AWS Budgets<br/>$5 / $25 / $100"]
    end

    subgraph Pay["PaymentsStack"]
      direction TB
      KMS["KMS key<br/>alias/razorpay-free"]
      SM["Secrets Manager<br/>razorpay-free-db-dsn"]

      subgraph SQS_grp["SQS FIFO"]
        WQ_aws[("webhook-events.fifo")]
        WDLQ_aws[("webhook-events-dlq.fifo")]
        FQ_aws[("fulfillment-queue.fifo")]
        FDLQ_aws[("fulfillment-dlq.fifo")]
      end

      ProdL["Lambda<br/>razorpay-webhook-producer<br/>memory=512MB<br/>timeout=30s"]
      ConsL["Lambda<br/>razorpay-webhook-consumer<br/>memory=512MB<br/>timeout=60s"]
      FulL["Lambda<br/>razorpay-fulfillment<br/>memory=512MB<br/>timeout=60s"]

      FURL["Function URL<br/>(auth=NONE, CORS)"]

      EB["EventBridge rule<br/>rate(5 minutes)<br/>stale-paid-monitor"]
    end
  end

  Razorpay["Razorpay (external)"]
  Neon[("Neon Postgres<br/>public TLS endpoint")]
  SSM[("SSM Parameter Store<br/>/razorpay/.tenant_id./*")]

  Razorpay -->|HTTPS POST| FURL
  FURL --> ProdL
  ProdL --> WQ_aws
  WQ_aws -.->|max receives=5| WDLQ_aws
  WQ_aws -->|event-source| ConsL
  ConsL --> Neon
  ConsL --> FQ_aws
  FQ_aws -.->|max receives=5| FDLQ_aws
  FQ_aws -->|event-source| FulL
  FulL --> Neon
  EB -->|invoke| ConsL

  ProdL -.->|GetParameter<br/>per tenant| SSM
  ConsL -.->|GetParameter<br/>per tenant| SSM
  ConsL -.->|GetSecretValue| SM
  FulL -.->|GetSecretValue| SM
  KMS -.->|wraps| SM
  KMS -.->|wraps| SSM

  ECR_P -.->|:latest| ProdL
  ECR_C -.->|:latest| ConsL
  ECR_F -.->|:latest| FulL

  ProdL -->|logs| LG_P
  ConsL -->|logs| LG_C
  FulL -->|logs| LG_F

  classDef stack fill:#dde5ff,stroke:#3060c0,color:#000;
  class Found,Pay stack;
  classDef external fill:#f0e0d0,stroke:#a37c4b,color:#000;
  class Razorpay,Neon external;
```

## Network posture (Free Track)

- **Producer Function URL** is public HTTPS. Razorpay's webhook senders POST to it directly. No API Gateway.
- **Consumer and Fulfillment Lambdas** have AWS public egress (no VPC). They talk to Neon over its public TLS endpoint. Cost = $0 ingress + standard egress.
- **Inside AWS:** Lambda → SQS / SSM / Secrets Manager / KMS / CloudWatch all stays inside AWS's network. No public hops.
- **No NAT Gateway.** This is the single biggest cost saver. Adding one would add ~$32/month minimum even with zero traffic.

## Network posture (Paid Track)

Conditional on `track="paid"` context. Same Lambdas + queues, but Consumer and Fulfillment move into private subnets with Interface Endpoints for SQS, Secrets Manager, KMS, Logs, Monitoring. RDS in the same VPC's private subnet.

```mermaid
flowchart LR
  subgraph VPC["VPC (Paid Track only)"]
    direction TB
    subgraph Private["Private subnets (multi-AZ)"]
      ConsL2["Consumer Lambda"]
      FulL2["Fulfillment Lambda"]
      RDS[("RDS Postgres<br/>private endpoint")]
    end
    IEsqs["VPC Interface Endpoint<br/>SQS"]
    IEsm["VPC Interface Endpoint<br/>Secrets Manager"]
    IEkms["VPC Interface Endpoint<br/>KMS"]
    IElogs["VPC Interface Endpoint<br/>CloudWatch Logs"]
  end

  ConsL2 --> IEsqs
  ConsL2 --> IEsm
  ConsL2 --> IEkms
  ConsL2 --> IElogs
  ConsL2 <--> RDS
  FulL2 --> IEsqs
  FulL2 --> IEsm
  FulL2 --> IEkms
  FulL2 --> IElogs
  FulL2 <--> RDS
```

**Paid Track delta:** ~$7/month per Interface Endpoint × 4 endpoints × 2 AZs = ~$56/month minimum, plus RDS reserved instance ~$15/month at the smallest size. Compare to Free Track's ~$2/month at idle.

## IAM model

Three Lambda execution roles, each scoped to the minimum:

| Role | Allowed | Explicit Deny |
|---|---|---|
| `WebhookProducerRole` | `sqs:SendMessage` on webhook-events.fifo; `ssm:GetParameter` on `/razorpay/*`; `kms:Decrypt` | `rds:*`, `rds-db:*`; `secretsmanager:GetSecretValue` on DB secret ARN |
| `WebhookConsumerRole` | `sqs:Receive/Delete` on webhook-events.fifo; `sqs:SendMessage` on fulfillment-queue.fifo; `ssm:GetParameter`; `kms:Decrypt`; `secretsmanager:GetSecretValue` on DB secret | — |
| `FulfillmentRole` | `sqs:Receive/Delete` on fulfillment-queue.fifo; `secretsmanager:GetSecretValue` on DB secret; `kms:Decrypt`; `sns:Publish` on ops topic | — |

**Critical Invariant 4 enforcement:** the producer's explicit Deny on `rds:*` and the DB secret ARN means a leaked producer credential cannot reach the DB even if the application code has a bug.

## Read next

- [system-architecture.md](system-architecture.md) — what each Lambda *does*.
- [webhook-flow.md](webhook-flow.md) — sequence of one event through this topology.
- [docs/cloud/iam-bootstrap.md](../cloud/iam-bootstrap.md) — IAM role + policy patterns.
- `infra/stacks/foundation.py` and `infra/stacks/payments.py` — the actual CDK code.
