"""Foundation stack: things that exist before the Lambdas do.

Phase 15 scope. Provisions per-environment account scaffolding that
subsequent stacks (Phase 16's payments stack) attach to. Specifically:

- ECR repositories for the three Lambda container images
  (producer, consumer, fulfillment). Tagging mutability=MUTABLE so we
  can roll `:latest` through CI. Lifecycle policy expires untagged
  images after 7 days.
- CloudWatch log groups with 14-day retention (Free Tier covers up to
  5 GB ingest + 5 GB storage; 14 days is the sweet spot for a low-volume
  payment flow).
- SNS topic for operational alerts. Email subscription is created if
  `razorpay:ops_email` context is set.
- AWS Budgets billing alarm with three thresholds (default $5/$25/$100).
  $5 is the "something is wrong" trigger on Free Track; $25 and $100 are
  escalation gates.

Free-track posture (CLAUDE.md): no VPC, no Interface Endpoints. The stack
exposes ECR repo URIs and log group names as CfnOutputs so Phase 16 can
reference them via `Fn.import_value`.
"""

from __future__ import annotations

from typing import Sequence

import aws_cdk as cdk
import aws_cdk.aws_budgets as budgets
import aws_cdk.aws_ecr as ecr
import aws_cdk.aws_logs as logs
import aws_cdk.aws_sns as sns
import aws_cdk.aws_sns_subscriptions as subs
from constructs import Construct

LAMBDA_NAMES = ("webhook-producer", "webhook-consumer", "fulfillment")


class FoundationStack(cdk.Stack):
    """Account-level building blocks the payment Lambdas depend on."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        track: str = "free",
        ops_email: str | None = None,
        budget_thresholds_usd: Sequence[int] = (5, 25, 100),
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.track = track

        # ---------- ECR repos ----------
        self.ecr_repos: dict[str, ecr.Repository] = {}
        for name in LAMBDA_NAMES:
            repo = ecr.Repository(
                self,
                f"Ecr-{name}",
                repository_name=f"razorpay-{name}",
                image_tag_mutability=ecr.TagMutability.MUTABLE,
                image_scan_on_push=True,
                # Avoid surprise costs from old image versions hanging around.
                lifecycle_rules=[
                    ecr.LifecycleRule(
                        description="expire untagged images after 7 days",
                        tag_status=ecr.TagStatus.UNTAGGED,
                        max_image_age=cdk.Duration.days(7),
                    ),
                    ecr.LifecycleRule(
                        description="keep last 10 tagged images",
                        tag_status=ecr.TagStatus.ANY,
                        max_image_count=10,
                    ),
                ],
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )
            self.ecr_repos[name] = repo
            cdk.CfnOutput(
                self,
                f"EcrUri-{name}",
                value=repo.repository_uri,
                export_name=f"{construct_id}-ecr-{name}-uri",
            )

        # ---------- Log groups (14-day retention) ----------
        self.log_groups: dict[str, logs.LogGroup] = {}
        for name in LAMBDA_NAMES:
            lg = logs.LogGroup(
                self,
                f"Logs-{name}",
                log_group_name=f"/aws/lambda/razorpay-{name}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )
            self.log_groups[name] = lg

        # Application log group (for the dev backend, admin routes if hosted
        # as a separate Lambda).
        logs.LogGroup(
            self,
            "Logs-admin",
            log_group_name="/aws/lambda/razorpay-admin",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ---------- Ops alerts SNS topic ----------
        self.ops_topic = sns.Topic(
            self,
            "OpsAlerts",
            display_name="razorpay-checkout ops",
            topic_name=f"razorpay-ops-{track}",
        )
        if ops_email and ops_email != "REPLACE_WITH_OPS_EMAIL@example.com":
            self.ops_topic.add_subscription(subs.EmailSubscription(ops_email))

        cdk.CfnOutput(
            self,
            "OpsTopicArn",
            value=self.ops_topic.topic_arn,
            export_name=f"{construct_id}-ops-topic-arn",
        )

        # ---------- Billing alarm via AWS Budgets ----------
        # AWS Budgets are account-wide; we tag the budget with the project so
        # multi-project accounts can attribute the alarm.
        notify_to = (
            [ops_email] if ops_email and ops_email != "REPLACE_WITH_OPS_EMAIL@example.com"
            else []
        )

        # Subscribers: always include the SNS topic (so paging integrations
        # work), plus the operator email if configured. AWS Budgets accepts up
        # to 10 subscribers per notification.
        subscribers: list[budgets.CfnBudget.SubscriberProperty] = [
            budgets.CfnBudget.SubscriberProperty(
                subscription_type="SNS",
                address=self.ops_topic.topic_arn,
            ),
        ]
        for addr in notify_to:
            subscribers.append(
                budgets.CfnBudget.SubscriberProperty(
                    subscription_type="EMAIL",
                    address=addr,
                )
            )

        for threshold in budget_thresholds_usd:
            budgets.CfnBudget(
                self,
                f"MonthlyBudget-{threshold}",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(
                        amount=threshold,
                        unit="USD",
                    ),
                    budget_name=f"razorpay-{track}-monthly-{threshold}usd",
                    cost_filters={
                        "TagKeyValue": ["user:Project$razorpay-checkout"],
                    },
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type="ACTUAL",
                            comparison_operator="GREATER_THAN",
                            threshold=80,  # alert at 80% of budget
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=subscribers,
                    ),
                ],
            )

        cdk.Tags.of(self).add("Track", track)
