#!/usr/bin/env python3
"""CDK app entry for the Razorpay payment integration.

Run synth only (no deploy) with:

    cd infra
    uv sync --extra dev
    uv run cdk synth

To deploy (Phase 17 — requires real AWS credentials):

    aws sso login --profile razorpay-free  # or aws-vault, etc.
    AWS_PROFILE=razorpay-free uv run cdk deploy razorpay-free-foundation

The app is environment-aware via context. Each environment ("free-track",
"paid-track" later) maps to one CDK env (account + region). See cdk.json
context block.
"""

from __future__ import annotations

import os
import sys

import aws_cdk as cdk

from stacks.foundation import FoundationStack
from stacks.payments import PaymentsStack


def main() -> None:
    app = cdk.App()

    envs: dict = app.node.try_get_context("razorpay:env") or {}
    if not envs:
        sys.stderr.write(
            "no razorpay:env context found in cdk.json; nothing to synth\n"
        )
        return

    ops_email = app.node.try_get_context("razorpay:ops_email")
    budget_thresholds = (
        app.node.try_get_context("razorpay:budget_thresholds_usd") or [5, 25, 100]
    )

    for env_name, env_cfg in envs.items():
        account = env_cfg.get("account")
        region = env_cfg.get("region", "ap-south-1")
        # Allow overriding from process env so CI can pin a per-branch account.
        if account == "REPLACE_WITH_ACCOUNT_ID":
            account = os.environ.get("CDK_DEFAULT_ACCOUNT") or account

        env = cdk.Environment(account=account, region=region)
        track = env_cfg.get("track", "free")

        foundation = FoundationStack(
            app,
            f"razorpay-{env_name}-foundation",
            env=env,
            track=track,
            ops_email=ops_email,
            budget_thresholds_usd=budget_thresholds,
        )

        # Optional override: caller can pre-provision the DB secret elsewhere
        # (e.g. via the RDS console or a managed Neon project) and pass the
        # ARN through context. Otherwise PaymentsStack creates an empty one
        # the operator fills.
        db_dsn_secret_arn = env_cfg.get("db_dsn_secret_arn")

        PaymentsStack(
            app,
            f"razorpay-{env_name}-payments",
            env=env,
            foundation=foundation,
            track=track,
            db_dsn_secret_arn=db_dsn_secret_arn,
        )

    cdk.Tags.of(app).add("Project", "razorpay-checkout")
    cdk.Tags.of(app).add("ManagedBy", "cdk")
    app.synth()


if __name__ == "__main__":
    main()
