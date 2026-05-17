#!/usr/bin/env python3
"""One-shot reconcile: compare our payments table vs. Razorpay's record.

Hits /payments/reconcile (admin-gated) and pretty-prints the mismatches.
Designed to run as a daily cron job; non-zero exit if any mismatches are
found.

Usage:

    BASE_URL=https://api.example.com \
    ADMIN_API_KEY=$(aws ssm get-parameter --name /razorpay/admin/key --with-decryption --query Parameter.Value --output text) \
    python scripts/reconcile-diff.py

    # Window override:
    python scripts/reconcile-diff.py --since 2026-05-10 --until 2026-05-13

    # CSV for analysis pipelines:
    python scripts/reconcile-diff.py --csv > mismatches.csv

For mismatch interpretation, see `docs/runbooks/incident-checklist.md`.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx


def _default_window() -> tuple[str, str]:
    until = datetime.now(tz=timezone.utc)
    since = until - timedelta(days=1)
    return since.isoformat(), until.isoformat()


def reconcile(
    base_url: str,
    admin_key: str,
    tenant: str | None,
    since: str | None,
    until: str | None,
    timeout: float,
    fmt: str,
) -> int:
    since_iso, until_iso = since or _default_window()[0], until or _default_window()[1]
    params: dict[str, str] = {"since": since_iso, "until": until_iso}
    if tenant:
        params["tenant_id"] = tenant
    headers = {"X-Admin-Api-Key": admin_key}

    try:
        r = httpx.get(
            f"{base_url.rstrip('/')}/payments/reconcile",
            params=params,
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        print(f"reconcile fetch failed: {exc}", file=sys.stderr)
        return 2

    if r.status_code != 200:
        print(f"reconcile returned {r.status_code}: {r.text}", file=sys.stderr)
        return 2

    body = r.json()
    mismatches = body.get("mismatches", [])
    summary = body.get("summary", {})

    if fmt == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(
            [
                "razorpay_payment_id",
                "razorpay_status",
                "local_status",
                "amount",
                "tenant_id",
                "reason",
            ]
        )
        for m in mismatches:
            w.writerow(
                [
                    m.get("razorpay_payment_id", ""),
                    m.get("razorpay_status", ""),
                    m.get("local_status", ""),
                    m.get("amount", ""),
                    m.get("tenant_id", ""),
                    m.get("reason", ""),
                ]
            )
        return 1 if mismatches else 0

    print(f"reconcile window: {since_iso}  to  {until_iso}")
    print(f"tenant: {tenant or 'all'}")
    print(
        f"summary: razorpay={summary.get('razorpay_total', '?')}  "
        f"local={summary.get('local_total', '?')}  "
        f"mismatches={len(mismatches)}"
    )
    print()
    if not mismatches:
        print("✓ no mismatches")
        return 0

    print(
        f"{'razorpay_payment_id':28} {'rzp_status':12} {'local_status':14} "
        f"{'amount':>10} {'reason':30}"
    )
    print("-" * 100)
    for m in mismatches:
        print(
            f"{m.get('razorpay_payment_id', '')[:28]:28} "
            f"{(m.get('razorpay_status') or '?')[:12]:12} "
            f"{(m.get('local_status') or '?')[:14]:14} "
            f"{int(m.get('amount', 0)):>10} "
            f"{(m.get('reason') or '')[:30]:30}"
        )
    print(
        f"\nFound {len(mismatches)} mismatch(es). See docs/runbooks/incident-checklist.md."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=os.environ.get("BASE_URL"))
    p.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY"))
    p.add_argument("--tenant", default=os.environ.get("TENANT_ID"))
    p.add_argument(
        "--since", default=None, help="ISO8601 window start (default: 24h ago)"
    )
    p.add_argument("--until", default=None, help="ISO8601 window end (default: now)")
    p.add_argument("--csv", action="store_true")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    if not args.base_url:
        print("missing --base-url or BASE_URL", file=sys.stderr)
        return 2
    if not args.admin_key:
        print("missing --admin-key or ADMIN_API_KEY", file=sys.stderr)
        return 2
    return reconcile(
        args.base_url,
        args.admin_key,
        args.tenant,
        args.since,
        args.until,
        args.timeout,
        "csv" if args.csv else "table",
    )


if __name__ == "__main__":
    sys.exit(main())
