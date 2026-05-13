#!/usr/bin/env python3
"""Production read-only smoke test.

Hits /health, /admin/kpis, and /admin/dashboard-data?recent_limit=0 to
verify the deployed payment integration is alive and responsive WITHOUT
touching any data. Safe to run against prod from CI on a schedule.

Exit codes:
  0  all checks passed
  1  one or more checks failed
  2  configuration error (missing env)

Usage:

    BASE_URL=https://api.example.com \
    ADMIN_API_KEY=$(aws ssm get-parameter --name /razorpay/admin/key --with-decryption --query Parameter.Value --output text) \
    python scripts/prod-smoke.py

    # Or via flags:
    python scripts/prod-smoke.py --base-url https://api.example.com \
                                  --admin-key $ADMIN_API_KEY \
                                  --tenant acme-india

Anything destructive lives in scripts/dlq-replay.py and scripts/local-fixtures.py;
this script is intentionally read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx


def _check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{' · ' + detail if detail else ''}")
    return ok


def smoke(base_url: str, admin_key: str | None, tenant: str | None, timeout: float) -> int:
    base = base_url.rstrip("/")
    print(f"smoke test against {base}\n")
    all_ok = True

    # 1. /health is unauthenticated. Should be fast.
    t0 = time.perf_counter()
    try:
        r = httpx.get(f"{base}/health", timeout=timeout)
        latency_ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        all_ok &= _check("/health", ok, f"{r.status_code} · {latency_ms:.0f}ms")
    except Exception as exc:
        all_ok &= _check("/health", False, str(exc))

    if admin_key is None:
        print("\n  (skipping /admin/* checks — no ADMIN_API_KEY)")
        return 0 if all_ok else 1

    headers = {"X-Admin-Api-Key": admin_key}
    params = {}
    if tenant:
        params["tenant_id"] = tenant

    # 2. /admin/kpis — lightweight, returns today's counts + queue depths.
    t0 = time.perf_counter()
    try:
        r = httpx.get(f"{base}/admin/kpis", headers=headers, params=params, timeout=timeout)
        latency_ms = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 200
        body = r.json() if ok else {}
        kpis = body.get("kpis", {})
        queues = body.get("queues", {})
        all_ok &= _check(
            "/admin/kpis",
            ok,
            f"{r.status_code} · {latency_ms:.0f}ms · paid={kpis.get('today_paid', '?')} stuck={kpis.get('stuck_paid', '?')}",
        )
        # 3. DLQ alarm — depth must be 0.
        for dlq_name in ("webhook-events-dlq.fifo", "fulfillment-dlq.fifo"):
            q = queues.get(dlq_name, {})
            depth = q.get("visible", q.get("error", "?"))
            ok_dlq = isinstance(depth, int) and depth == 0
            all_ok &= _check(
                f"DLQ {dlq_name}",
                ok_dlq,
                f"depth={depth}",
            )
        # 4. Stuck-paid alarm — must be 0.
        stuck = kpis.get("stuck_paid", 0)
        all_ok &= _check("stuck paid < 1", stuck == 0, f"count={stuck}")
    except Exception as exc:
        all_ok &= _check("/admin/kpis", False, str(exc))

    # 5. /admin/dashboard-data with recent_limit=0 — verify DB reachable from API tier.
    t0 = time.perf_counter()
    try:
        r = httpx.get(
            f"{base}/admin/dashboard-data",
            headers=headers,
            params={**params, "recent_limit": 0},
            timeout=timeout,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        all_ok &= _check(
            "/admin/dashboard-data",
            r.status_code == 200,
            f"{r.status_code} · {latency_ms:.0f}ms",
        )
    except Exception as exc:
        all_ok &= _check("/admin/dashboard-data", False, str(exc))

    print()
    print("SMOKE", "OK" if all_ok else "FAILED")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", default=os.environ.get("BASE_URL"))
    p.add_argument("--admin-key", default=os.environ.get("ADMIN_API_KEY"))
    p.add_argument("--tenant", default=os.environ.get("TENANT_ID"))
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args(argv)
    if not args.base_url:
        print("missing --base-url or BASE_URL env", file=sys.stderr)
        return 2
    return smoke(args.base_url, args.admin_key, args.tenant, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
