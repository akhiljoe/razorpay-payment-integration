"""Centralised cdk-nag suppressions with justifications.

Every suppression names the rule, the resource path glob it applies to, and the
reason. If a reason starts with TODO, the suppression is provisional and must be
revisited before the next deploy.

To audit suppressions:

    cd infra
    uv run python -c "from stacks.nag_suppressions import SUPPRESSIONS; \
        import json; print(json.dumps(SUPPRESSIONS, indent=2))"
"""

from __future__ import annotations

from typing import TypedDict

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions


class _Suppression(TypedDict):
    paths: list[str]
    id: str
    reason: str


# Resource path globs use the CDK construct tree path
# (e.g. "razorpay-free-payments/WebhookQueue/Resource").
SUPPRESSIONS: list[_Suppression] = [
    {
        "paths": ["*/WebhookQueue/Resource", "*/FulfillmentQueue/Resource"],
        "id": "AwsSolutions-SQS3",
        "reason": (
            "DLQs are wired via DeadLetterQueue prop in PaymentsStack; cdk-nag "
            "does not detect this on the parent queue. See DESIGN_v6 Section 6.3."
        ),
    },
    {
        "paths": ["*/ProducerFn/ServiceRole/Resource"],
        "id": "AwsSolutions-IAM4",
        "reason": (
            "Producer uses AWSLambdaBasicExecutionRole only (no VPC). All other "
            "permissions are inline and scoped per the IAM matrix in "
            "designs/MIGRATION_ARCHITECTURE.md Pillar 5."
        ),
    },
    {
        "paths": ["*/ConsumerFn/ServiceRole/Resource", "*/FulfillmentFn/ServiceRole/Resource"],
        "id": "AwsSolutions-IAM4",
        "reason": (
            "VPC-resident Lambdas attach AWSLambdaVPCAccessExecutionRole for ENI "
            "lifecycle. All app permissions are inline and scoped."
        ),
    },
    # Suppressions for resources that intentionally do not exist yet
    # (Phase 6 of the migration plan) are added here as the stacks are built out.
]


def apply(app: cdk.App) -> None:
    """Apply all centralised suppressions across the CDK app.

    Suppressions are applied by resource-path glob. If no resource matches a
    glob, cdk-nag silently ignores the suppression; this is acceptable for the
    early build-out phase where some constructs do not yet exist.
    """
    for entry in SUPPRESSIONS:
        nag_suppressions = [NagPackSuppression(id=entry["id"], reason=entry["reason"])]
        for path in entry["paths"]:
            NagSuppressions.add_resource_suppressions_by_path(
                app,
                path=path,
                suppressions=nag_suppressions,
                apply_to_children=True,
            )
