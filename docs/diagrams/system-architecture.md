# System architecture

Components, queues, and the database in one picture.

```mermaid
flowchart LR
  subgraph Customer["Customer browser"]
    Widget["checkout-widget<br/>(React or vanilla)"]
    Shop["shop.html"]
  end

  subgraph Razorpay["Razorpay (external)"]
    RZP_API["Razorpay API"]
    RZP_WH["Razorpay Webhook<br/>sender"]
  end

  subgraph Producer_box["Webhook Producer (Lambda)"]
    direction TB
    Sig["Signature verify<br/>HMAC + previous-secret fallback"]
    Pub["Publish to SQS<br/>MessageGroupId = razorpay_order_id"]
    Sig --> Pub
  end

  subgraph Queues_box["SQS FIFO queues"]
    direction TB
    WQ[("webhook-events.fifo")]
    WDLQ[("webhook-events-dlq.fifo")]
    FQ[("fulfillment-queue.fifo")]
    FDLQ[("fulfillment-dlq.fifo")]
    WQ -.->|max receives=5| WDLQ
    FQ -.->|max receives=5| FDLQ
  end

  subgraph Consumer_box["Webhook Consumer (Lambda)"]
    direction TB
    Dedup["Fast-path dedup<br/>(tenant_id, event_id) ON CONFLICT"]
    Guard["Event-type-aware primary guard<br/>after SELECT ... FOR UPDATE"]
    Trans["Transition + INSERT payments/refunds"]
    Dedup --> Guard --> Trans
  end

  subgraph Fulfillment_box["Fulfillment Lambda"]
    direction TB
    SC["Short-circuit if already fulfilled"]
    Hook["FulfillmentHook.fulfill<br/>(host code)"]
    Trans2["Transition paid -> fulfilled"]
    SC --> Hook --> Trans2
  end

  DB[("Postgres<br/>orders / payments / refunds<br/>webhook_events / order_events / tenants")]

  Widget -->|POST /payments/orders| OrderRoute["Order route<br/>(FastAPI)"]
  OrderRoute -->|create| RZP_API
  OrderRoute -->|INSERT initializing -> created| DB
  Widget -->|opens modal| RZP_API
  RZP_API -->|payment success| Widget
  Widget -->|verify-callback| OrderRoute
  Widget -.->|in prod, automatic| RZP_WH
  RZP_WH -->|POST /payments/webhook/.tenant_id.| Producer_box
  Producer_box --> WQ
  WQ -->|batch_size=1| Consumer_box
  Consumer_box <-->|FOR UPDATE| DB
  Consumer_box -->|publish on paid| FQ
  FQ -->|batch_size=1| Fulfillment_box
  Fulfillment_box <-->|FOR UPDATE| DB

  classDef external fill:#f0e0d0,stroke:#a37c4b,color:#000;
  class Razorpay external;
  classDef lambda fill:#dde5ff,stroke:#3060c0,color:#000;
  class Producer_box,Consumer_box,Fulfillment_box lambda;
  classDef queue fill:#e6f5e6,stroke:#3a8a3a,color:#000;
  class Queues_box queue;
```

## What this picture says

**Two security boundaries.** The Producer (left of `WQ`) holds zero database credentials. Even if the public endpoint is compromised, the DB is unreachable. The Consumer + Fulfillment (right of `WQ`) hold credentials but only handle messages that have already passed signature verification.

**Two queues, two retry budgets.** Each main queue has its own DLQ with `maxReceiveCount=5`. The DLQs are inspected with `scripts/dlq-replay.py`; alarms fire on `ApproximateNumberOfMessages > 0`.

**One database, five tenant-scoped tables.** Every query against the five tables filters by `tenant_id`. The logical FK to `tenants.id` is unconstrained at the DB level for performance.

## Read next

- [webhook-flow.md](webhook-flow.md) — what happens *inside* this picture during one `payment.captured` event.
- [state-machine.md](state-machine.md) — the order status graph.
- [ARCHITECTURE.md](../../ARCHITECTURE.md) — full system overview in prose.
