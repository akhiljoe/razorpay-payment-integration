"""Centralised cdk-nag suppressions with justifications.

Every suppression names the rule, the resource path glob it applies to, and the
reason. If a reason starts with TODO, the suppression is provisional and must be
revisited before the next deploy.
"""

from __future__ import annotations

from typing import TypedDict

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions


class _Suppression(TypedDict, total=False):
    paths: list[str]
    id: str
    reason: str
    applies_to: list[str]


# `paths` documents which constructs the suppression is justified for.
# cdk-nag's resource-path matching is exact (no glob support), so we apply
# suppressions at the stack level and rely on each rule only firing on the
# relevant resource type. `applies_to` narrows by managed policy / wildcard.
SUPPRESSIONS: list[_Suppression] = [
    {
        "paths": ["*/WebhookEventsQueue/Resource", "*/FulfillmentQueue/Resource"],
        "id": "AwsSolutions-SQS3",
        "reason": (
            "DLQs are wired via the DeadLetterQueue prop on each main queue. "
            "cdk-nag does not detect that the redrive policy points at a DLQ. "
            "See DESIGN_v6 Section 6.3 and infra/stacks/payments.py."
        ),
    },
    {
        "paths": ["*/OpsAlerts/Resource"],
        "id": "AwsSolutions-SNS3",
        "reason": (
            "Ops topic publishers are limited to CloudWatch alarms and AWS "
            "Budgets (both AWS-internal, TLS-in-transit by service contract). "
            "No external publisher exists."
        ),
    },
    {
        "paths": ["*/OpsAlerts/Resource"],
        "id": "AwsSolutions-SNS2",
        "reason": (
            "SSE not enabled on the ops topic to keep AWS Budgets able to "
            "publish without granting cross-service KMS permission. Alert "
            "payloads are non-sensitive metric notifications."
        ),
    },
    {
        "paths": ["*/DbSecret/Resource"],
        "id": "AwsSolutions-SMG4",
        "reason": (
            "Phase 4 placeholder secret. Rotation is wired when Neon-to-RDS "
            "migration lands (MIGRATION_ARCHITECTURE.md Enable 7). Neon "
            "manages its own credential lifecycle today."
        ),
    },
    {
        "paths": [
            "*/WebhookProducerRole/Resource",
            "*/WebhookConsumerRole/Resource",
            "*/FulfillmentRole/Resource",
        ],
        "id": "AwsSolutions-IAM4",
        "reason": (
            "AWSLambdaBasicExecutionRole and AWSXRayDaemonWriteAccess are the "
            "AWS-recommended managed policies for Lambda log + X-Ray emission. "
            "Workload-scoped permissions are inline and per-resource."
        ),
        "applies_to": [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "Policy::arn:<AWS::Partition>:iam::aws:policy/AWSXRayDaemonWriteAccess",
        ],
    },
    {
        "paths": [
            "*/WebhookProducerRole/DefaultPolicy/Resource",
            "*/WebhookConsumerRole/DefaultPolicy/Resource",
        ],
        "id": "AwsSolutions-IAM5",
        "reason": (
            "Tenant config is keyed under /razorpay/<tenant_id>/* in SSM. "
            "Producer + Consumer must read parameters for any active tenant; "
            "narrower scoping would require redeploying on every onboard. "
            "Tenant isolation enforced at application layer."
        ),
        "applies_to": [
            "Resource::arn:aws:ssm:ap-south-1:111111111111:parameter/razorpay/*",
            "Resource::arn:aws:ssm:ap-south-1:222222222222:parameter/razorpay/*",
        ],
    },
    {
        "paths": [
            "*/WebhookProducerRole/DefaultPolicy/Resource",
            "*/WebhookConsumerRole/DefaultPolicy/Resource",
            "*/FulfillmentRole/DefaultPolicy/Resource",
        ],
        "id": "AwsSolutions-IAM5",
        "reason": (
            "Resource::* on the Producer role comes from the explicit Deny "
            "on rds:* / rds-db:* (defense-in-depth against the producer "
            "gaining DB access). A wildcard-resource Deny is strictly safer "
            "than a narrow Deny."
        ),
        "applies_to": ["Resource::*"],
    },
]


def apply(app: cdk.App) -> None:
    """Apply all centralised suppressions across every stack in the app.

    Uses `add_stack_suppressions` because resource-path matching in cdk-nag is
    exact (no glob support). Each rule only fires on the relevant resource
    type, so stack-wide suppression (optionally narrowed by `applies_to`) is
    the right granularity.
    """
    stacks = [child for child in app.node.children if isinstance(child, cdk.Stack)]
    for stack in stacks:
        for entry in SUPPRESSIONS:
            kwargs: dict = {"id": entry["id"], "reason": entry["reason"]}
            if "applies_to" in entry:
                kwargs["applies_to"] = entry["applies_to"]
            NagSuppressions.add_stack_suppressions(
                stack,
                suppressions=[NagPackSuppression(**kwargs)],
                apply_to_nested_stacks=True,
            )
