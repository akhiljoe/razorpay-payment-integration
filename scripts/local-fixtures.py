#!/usr/bin/env python3
"""Local dev test data manager.

Seeds, inspects, and wipes the dummy data this project creates while
manual-testing the dev backend. Two stores get touched:

  - Postgres (tenants, orders, payments, refunds, webhook_events, order_events)
  - LocalStack SQS (webhook-events.fifo + DLQ, fulfillment-queue.fifo + DLQ)

The script is intentionally dev-only. It reads `example/backend/.env` for
connection details so it stays aligned with the running uvicorn process.

Usage:

    python scripts/local-fixtures.py status
    python scripts/local-fixtures.py seed           # insert default tenant
    python scripts/local-fixtures.py seed --tenant t_dev --tenant acme
    python scripts/local-fixtures.py purge          # wipe tenant-scoped rows + drain queues
    python scripts/local-fixtures.py purge --hard   # also delete tenant rows
    python scripts/local-fixtures.py reset          # purge then seed (most common)

Safety: refuses to run if the DB host is anything other than localhost
or 127.0.0.1. This avoids accidental drops against a remote dev DB.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import asyncpg
import boto3
from botocore.config import Config as BotoConfig

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "example" / "backend" / ".env"

TENANT_SCOPED_TABLES = (
    "order_events",
    "webhook_events",
    "refunds",
    "payments",
    "orders",
)

QUEUE_NAMES = (
    "webhook-events.fifo",
    "webhook-events-dlq.fifo",
    "fulfillment-queue.fifo",
    "fulfillment-dlq.fifo",
)

DEFAULT_TENANTS = (("local-dev", "Local Dev Tenant"),)


def load_env(path: Path) -> None:
    """Lift .env into os.environ. Mirrors example/backend/main.py."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _normalise_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://") :]
    return dsn


def _assert_local(dsn: str) -> None:
    host = urlparse(_normalise_dsn(dsn)).hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit(
            f"refusing to run against non-local DB host {host!r}. "
            "Set RAZORPAY_DB_DSN to a localhost dsn or unset and re-source .env."
        )


def sqs_client():
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or "http://localhost:4566"
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    if "localhost" not in endpoint and "127.0.0.1" not in endpoint:
        raise SystemExit(
            f"refusing to talk to non-local SQS endpoint {endpoint!r}."
        )
    return boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        config=BotoConfig(retries={"max_attempts": 2}),
    )


def queue_url(sqs, name: str) -> str | None:
    try:
        return sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist:
        return None


# ---------- subcommands ----------


async def cmd_status(_args: argparse.Namespace) -> int:
    dsn = _normalise_dsn(os.environ["RAZORPAY_DB_DSN"])
    _assert_local(dsn)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        print("Postgres row counts:")
        for table in ("tenants", *TENANT_SCOPED_TABLES):
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                print(f"  {table:20s} {n}")
            except asyncpg.UndefinedTableError:
                print(f"  {table:20s} (table missing — migrations not applied?)")
    finally:
        await conn.close()

    print("\nSQS queue depths:")
    sqs = sqs_client()
    for name in QUEUE_NAMES:
        url = queue_url(sqs, name)
        if url is None:
            print(f"  {name:30s} (not created)")
            continue
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        visible = attrs.get("ApproximateNumberOfMessages", "?")
        inflight = attrs.get("ApproximateNumberOfMessagesNotVisible", "?")
        print(f"  {name:30s} visible={visible} inflight={inflight}")
    return 0


async def cmd_seed(args: argparse.Namespace) -> int:
    dsn = _normalise_dsn(os.environ["RAZORPAY_DB_DSN"])
    _assert_local(dsn)
    tenants: Iterable[tuple[str, str]]
    if args.tenant:
        tenants = [(tid, f"Dev tenant {tid}") for tid in args.tenant]
    else:
        tenants = DEFAULT_TENANTS
    conn = await asyncpg.connect(dsn=dsn)
    try:
        for tid, name in tenants:
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) "
                "ON CONFLICT (id) DO NOTHING",
                tid,
                name,
            )
            print(f"  upserted tenant {tid!r} ({name})")
    finally:
        await conn.close()
    return 0


async def cmd_purge(args: argparse.Namespace) -> int:
    dsn = _normalise_dsn(os.environ["RAZORPAY_DB_DSN"])
    _assert_local(dsn)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Order matters for FK constraints; TRUNCATE CASCADE handles cycles too.
        tables_sql = ", ".join(TENANT_SCOPED_TABLES)
        await conn.execute(f"TRUNCATE {tables_sql} RESTART IDENTITY CASCADE")
        print(f"  truncated: {tables_sql}")
        if args.hard:
            n = await conn.fetchval("SELECT COUNT(*) FROM tenants")
            await conn.execute("DELETE FROM tenants")
            print(f"  deleted {n} tenant rows (--hard)")
    finally:
        await conn.close()

    sqs = sqs_client()
    for name in QUEUE_NAMES:
        url = queue_url(sqs, name)
        if url is None:
            print(f"  skipped queue {name} (not created)")
            continue
        try:
            sqs.purge_queue(QueueUrl=url)
            print(f"  purged queue {name}")
        except sqs.exceptions.PurgeQueueInProgress:
            print(f"  queue {name} purge already in progress (60s cooldown)")
    return 0


async def cmd_reset(args: argparse.Namespace) -> int:
    rc = await cmd_purge(args)
    if rc != 0:
        return rc
    return await cmd_seed(args)


# ---------- entry ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print row counts and queue depths")

    seed = sub.add_parser("seed", help="Insert tenant rows")
    seed.add_argument(
        "--tenant",
        action="append",
        help="Tenant id to upsert. Repeatable. Default: local-dev",
    )

    purge = sub.add_parser("purge", help="Truncate tenant-scoped tables and purge queues")
    purge.add_argument(
        "--hard",
        action="store_true",
        help="Also delete rows from tenants table",
    )

    reset = sub.add_parser("reset", help="purge then seed")
    reset.add_argument("--hard", action="store_true")
    reset.add_argument("--tenant", action="append")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_env(ENV_FILE)
    if "RAZORPAY_DB_DSN" not in os.environ:
        print(
            "RAZORPAY_DB_DSN not set. Populate example/backend/.env or export it.",
            file=sys.stderr,
        )
        return 2

    args = parse_args(argv)
    fn = {
        "status": cmd_status,
        "seed": cmd_seed,
        "purge": cmd_purge,
        "reset": cmd_reset,
    }[args.cmd]
    return asyncio.run(fn(args))


if __name__ == "__main__":
    sys.exit(main())
