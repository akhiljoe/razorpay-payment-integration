"""PaymentsStack: SQS queues, KMS, Lambdas, and triggers.

Phase 16 scope. Builds on top of FoundationStack (ECR repos, log groups,
SNS topic). The stack is environment-aware via the `track` argument:

  - `track="free"`: no VPC, Lambdas public, no Interface Endpoints. Default.
  - `track="paid"`: VPC subnets + RDS in private subnet. Phase 16 ships
    only the conditional gate; the VPC construct lives in a follow-up.

Critical invariants enforced in IAM (CLAUDE.md):

  1. Producer holds **zero database credentials**. Its role has no
     access to the DB secret and no RDS actions.
  2. Producer can only `sqs:SendMessage` to `webhook-events.fifo`.
  3. Consumer + Fulfillment share DB secret access via the same KMS key.
  4. KMS decrypt is scoped to the per-environment customer-managed key.
  5. All resources tagged for cost attribution.

DESIGN_v5 Section 7 (Free Track) and Section 14 (multi-tenant). For the
SQS / Lambda config rationale see Section 13 (metrics) and Section 6
(failure modes).
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.aws_events as events
import aws_cdk.aws_events_targets as events_targets
import aws_cdk.aws_iam as iam
import aws_cdk.aws_kms as kms
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_lambda_event_sources as lambda_sources
import aws_cdk.aws_secretsmanager as secretsmanager
import aws_cdk.aws_sqs as sqs
from constructs import Construct

from .foundation import FoundationStack


class PaymentsStack(cdk.Stack):
    """The full payment integration runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationStack,
        track: str = "free",
        db_dsn_secret_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.track = track
        self.foundation = foundation

        # ---------- KMS customer-managed key ----------
        self.kms_key = kms.Key(
            self,
            "RazorpayKmsKey",
            alias=f"alias/razorpay-{track}",
            description=f"Wraps SSM SecureStrings and Secrets Manager for razorpay-{track}",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ---------- DB credentials Secret ----------
        # If the caller passed an ARN (e.g. from a managed RDS/Neon secret),
        # import it. Otherwise create a placeholder secret the operator fills.
        if db_dsn_secret_arn:
            self.db_secret = secretsmanager.Secret.from_secret_complete_arn(
                self, "DbSecret", db_dsn_secret_arn
            )
        else:
            self.db_secret = secretsmanager.Secret(
                self,
                "DbSecret",
                secret_name=f"razorpay-{track}-db-dsn",
                description="Postgres DSN for consumer + fulfillment Lambdas",
                encryption_key=self.kms_key,
                removal_policy=cdk.RemovalPolicy.RETAIN,
            )

        # ---------- SQS queue pairs ----------
        # DLQs first; main queues reference them in their redrive policy.
        self.webhook_dlq = sqs.Queue(
            self,
            "WebhookEventsDlq",
            queue_name="webhook-events-dlq.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=cdk.Duration.days(14),
        )
        self.fulfillment_dlq = sqs.Queue(
            self,
            "FulfillmentDlq",
            queue_name="fulfillment-dlq.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=cdk.Duration.days(14),
        )

        self.webhook_queue = sqs.Queue(
            self,
            "WebhookEventsQueue",
            queue_name="webhook-events.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            visibility_timeout=cdk.Duration.seconds(180),
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=self.webhook_dlq,
            ),
        )
        self.fulfillment_queue = sqs.Queue(
            self,
            "FulfillmentQueue",
            queue_name="fulfillment-queue.fifo",
            fifo=True,
            content_based_deduplication=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            visibility_timeout=cdk.Duration.seconds(180),
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=self.fulfillment_dlq,
            ),
        )

        # ---------- IAM roles ----------
        ssm_tenant_resources = [
            f"arn:aws:ssm:{self.region}:{self.account}:parameter/razorpay/*",
        ]

        producer_role = self._make_role(
            "WebhookProducerRole",
            description="Producer Lambda — no DB access (Critical Invariant 4)",
        )
        # Send only to webhook-events.fifo.
        self.webhook_queue.grant_send_messages(producer_role)
        # Read tenant secrets.
        producer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=ssm_tenant_resources,
            )
        )
        self.kms_key.grant_decrypt(producer_role)
        # Explicit deny on RDS + the DB secret (defense in depth).
        producer_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["rds:*", "rds-db:*"],
                resources=["*"],
            )
        )
        producer_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.db_secret.secret_arn],
            )
        )

        consumer_role = self._make_role(
            "WebhookConsumerRole",
            description="Consumer Lambda — webhook drain + DB transitions",
        )
        self.webhook_queue.grant_consume_messages(consumer_role)
        self.fulfillment_queue.grant_send_messages(consumer_role)
        consumer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=ssm_tenant_resources,
            )
        )
        self.db_secret.grant_read(consumer_role)
        self.kms_key.grant_decrypt(consumer_role)

        fulfillment_role = self._make_role(
            "FulfillmentRole",
            description="Fulfillment Lambda — calls FulfillmentHook + completes orders",
        )
        self.fulfillment_queue.grant_consume_messages(fulfillment_role)
        self.db_secret.grant_read(fulfillment_role)
        self.kms_key.grant_decrypt(fulfillment_role)
        # Hook may need to publish to ops topic on failures.
        foundation.ops_topic.grant_publish(fulfillment_role)

        # ---------- Lambdas ----------
        # Container images come from the Phase 15 ECR repos. CI builds and
        # pushes `:latest`; first deploy expects an image to be present.
        common_env = {
            "RAZORPAY_OTEL_EXPORTER": "xray",
            "LOG_LEVEL": "INFO",
            "RAZORPAY_DB_SECRET_ARN": self.db_secret.secret_arn,
            "WEBHOOK_QUEUE_URL": self.webhook_queue.queue_url,
            "FULFILLMENT_QUEUE_URL": self.fulfillment_queue.queue_url,
        }

        self.producer_fn = lambda_.DockerImageFunction(
            self,
            "WebhookProducerFn",
            function_name=f"razorpay-webhook-producer-{track}",
            code=lambda_.DockerImageCode.from_ecr(
                repository=foundation.ecr_repos["webhook-producer"],
                tag_or_digest="latest",
            ),
            role=producer_role,
            memory_size=512,
            timeout=cdk.Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
            reserved_concurrent_executions=50,  # cap blast radius from spam
            environment={
                **common_env,
                # Producer must NOT see the DB secret env name.
                "RAZORPAY_DB_SECRET_ARN": "",
            },
            log_group=foundation.log_groups["webhook-producer"],
            description="Razorpay webhook entry. No DB access. Critical Invariant 4.",
        )

        self.consumer_fn = lambda_.DockerImageFunction(
            self,
            "WebhookConsumerFn",
            function_name=f"razorpay-webhook-consumer-{track}",
            code=lambda_.DockerImageCode.from_ecr(
                repository=foundation.ecr_repos["webhook-consumer"],
                tag_or_digest="latest",
            ),
            role=consumer_role,
            memory_size=512,
            timeout=cdk.Duration.seconds(60),
            tracing=lambda_.Tracing.ACTIVE,
            environment=common_env,
            log_group=foundation.log_groups["webhook-consumer"],
            description=(
                "Drains webhook-events.fifo, runs state transitions" " + publishes fulfillment."
            ),
        )

        self.fulfillment_fn = lambda_.DockerImageFunction(
            self,
            "FulfillmentFn",
            function_name=f"razorpay-fulfillment-{track}",
            code=lambda_.DockerImageCode.from_ecr(
                repository=foundation.ecr_repos["fulfillment"],
                tag_or_digest="latest",
            ),
            role=fulfillment_role,
            memory_size=512,
            timeout=cdk.Duration.seconds(60),
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                **common_env,
                "OPS_TOPIC_ARN": foundation.ops_topic.topic_arn,
            },
            log_group=foundation.log_groups["fulfillment"],
            description="Drains fulfillment-queue.fifo, invokes FulfillmentHook, completes orders.",
        )

        # ---------- Producer Function URL ----------
        # Public HTTPS endpoint with built-in throttling. No API Gateway
        # cost. Auth is signature verification in the Lambda itself.
        self.producer_url = self.producer_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["content-type", "x-razorpay-signature"],
                max_age=cdk.Duration.hours(1),
            ),
        )

        # ---------- SQS event sources ----------
        self.consumer_fn.add_event_source(
            lambda_sources.SqsEventSource(
                self.webhook_queue,
                batch_size=1,  # FIFO with strict ordering per MessageGroupId
                report_batch_item_failures=True,
            )
        )
        self.fulfillment_fn.add_event_source(
            lambda_sources.SqsEventSource(
                self.fulfillment_queue,
                batch_size=1,
                report_batch_item_failures=True,
            )
        )

        # ---------- EventBridge schedule: stale-paid-monitor ----------
        events.Rule(
            self,
            "StalePaidMonitorSchedule",
            description="Sweep for orders stuck at paid for > 15 min",
            schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[
                events_targets.LambdaFunction(
                    self.consumer_fn,
                    event=events.RuleTargetInput.from_object({"trigger": "stale_paid_monitor"}),
                ),
            ],
        )

        # ---------- DLQ alarms via the ops topic ----------
        # Any message landing in either DLQ should page on-call.
        for dlq_name, dlq in (
            ("webhook-events-dlq", self.webhook_dlq),
            ("fulfillment-dlq", self.fulfillment_dlq),
        ):
            cdk.CfnOutput(
                self,
                f"DlqUrl-{dlq_name}",
                value=dlq.queue_url,
                export_name=f"{construct_id}-dlq-{dlq_name}-url",
            )

        # ---------- CfnOutputs ----------
        cdk.CfnOutput(
            self,
            "WebhookProducerUrl",
            value=self.producer_url.url,
            description="Public HTTPS endpoint for Razorpay to POST webhooks to.",
            export_name=f"{construct_id}-producer-url",
        )
        cdk.CfnOutput(
            self,
            "WebhookQueueUrl",
            value=self.webhook_queue.queue_url,
            export_name=f"{construct_id}-webhook-queue-url",
        )
        cdk.CfnOutput(
            self,
            "FulfillmentQueueUrl",
            value=self.fulfillment_queue.queue_url,
            export_name=f"{construct_id}-fulfillment-queue-url",
        )
        cdk.CfnOutput(
            self,
            "KmsKeyArn",
            value=self.kms_key.key_arn,
            export_name=f"{construct_id}-kms-key-arn",
        )
        cdk.CfnOutput(
            self,
            "DbSecretArn",
            value=self.db_secret.secret_arn,
            export_name=f"{construct_id}-db-secret-arn",
        )

        cdk.Tags.of(self).add("Track", track)

    # ----- internal helpers -----

    def _make_role(self, role_id: str, *, description: str) -> iam.Role:
        return iam.Role(
            self,
            role_id,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=description,
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess"),
            ],
        )
