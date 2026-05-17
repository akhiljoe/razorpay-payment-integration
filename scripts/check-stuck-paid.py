#!/usr/bin/env python3
"""Query orders stuck at status='paid' for longer than a threshold.

Read-only against the production DB. Use this when the stale-paid CloudWatch
alarm fires to get the actual list of affected orders + correlation IDs for
deeper investigation in logs.

Usage:

    RAZORPAY_DB_DSN=postgresql://... python scripts/check-stuck-paid.py
    python scripts/check-stuck-paid.py --threshold-minutes 30
    python scripts/check-stuck-paid.py --tenant acme-india --csv > stuck.csv

For each row, runbook is `docs/runbooks/stuck-paid-order.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Best-effort: pull example/backend/.env if RAZORPAY_DB_DSN isn't set."""
    env_file = ROOT / "example" / "backend" / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _normalise_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return "postgresql://" + dsn[len("postgresql+asyncpg://") :]
    return dsn


async def query_stuck(threshold_minutes: int, tenant: str | None) -> list[dict]:
    dsn = _normalise_dsn(os.environ["RAZORPAY_DB_DSN"])
    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT id, tenant_id, razorpay_order_id, receipt, amount,
                   paid_at, NOW() - paid_at AS stuck_for,
                   correlation_id
              FROM orders
             WHERE status = 'paid'
               AND paid_at < NOW() - INTERVAL '{int(threshold_minutes)} minutes'
               AND ($1::text IS NULL OR tenant_id = $1)
             ORDER BY paid_at
            """,
            tenant,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("no stuck-paid orders")
        return
    print(f"\n{len(rows)} stuck-paid order(s):\n")
    print(
        f"{'paid_at':30} {'stuck_for':18} {'tenant':16} {'razorpay_order_id':22} {'correlation_id':40}"
    )
    print("-" * 130)
    for r in rows:
        print(
            f"{str(r['paid_at']):30} "
            f"{str(r['stuck_for']):18} "
            f"{(r['tenant_id'] or '')[:16]:16} "
            f"{(r['razorpay_order_id'] or '')[:22]:22} "
            f"{(r['correlation_id'] or '')[:40]:40}"
        )


def _print_csv(rows: list[dict]) -> None:
    w = csv.writer(sys.stdout)
    w.writerow(
        [
            "paid_at",
            "stuck_for",
            "tenant_id",
            "razorpay_order_id",
            "receipt",
            "amount",
            "correlation_id",
            "order_id",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r["paid_at"],
                r["stuck_for"],
                r["tenant_id"],
                r["razorpay_order_id"],
                r["receipt"],
                r["amount"],
                r["correlation_id"],
                r["id"],
            ]
        )


async def amain(args: argparse.Namespace) -> int:
    rows = await query_stuck(args.threshold_minutes, args.tenant)
    if args.csv:
        _print_csv(rows)
    else:
        _print_table(rows)
    # Non-zero exit if any stuck rows found — useful for cron-driven alerting.
    return 1 if rows and args.exit_nonzero_on_match else 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--threshold-minutes", type=int, default=15)
    p.add_argument("--tenant", default=None)
    p.add_argument("--csv", action="store_true", help="Emit CSV to stdout")
    p.add_argument(
        "--exit-nonzero-on-match",
        action="store_true",
        help="Exit 1 if any stuck rows are found (useful for cron alerts)",
    )
    args = p.parse_args(argv)
    if "RAZORPAY_DB_DSN" not in os.environ:
        print("RAZORPAY_DB_DSN not set", file=sys.stderr)
        return 2
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
