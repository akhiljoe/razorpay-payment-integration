#!/usr/bin/env python3
"""DLQ inspection and replay CLI.

Operator tool for investigating and resolving messages that landed in either
`webhook-events-dlq.fifo` or `fulfillment-dlq.fifo`. Production posture:

  - Listing is read-only and safe.
  - Replay re-publishes the original body and MessageAttributes back to the
    main queue using the original MessageGroupId/MessageDeduplicationId so
    FIFO ordering is preserved.
  - Delete is irreversible; pair it with `--dry-run` before any destructive run.

Localhost guard. The script refuses to talk to a non-LocalStack endpoint
unless `--allow-production` is set explicitly. Production usage requires the
operator to acknowledge the risk on every invocation.

Usage:

    # list (read-only)
    python scripts/dlq-replay.py list webhook
    python scripts/dlq-replay.py list fulfillment

    # inspect one message (full body + attributes)
    python scripts/dlq-replay.py inspect webhook <receipt_handle_or_index>

    # replay messages back to the main queue
    python scripts/dlq-replay.py replay webhook --all --dry-run
    python scripts/dlq-replay.py replay webhook --message-id <id>

    # delete (irreversible)
    python scripts/dlq-replay.py delete webhook --all --dry-run
    python scripts/dlq-replay.py delete webhook --message-id <id>

Reads `RAZORPAY_DB_DSN`-less env (only AWS_ENDPOINT_URL et al.) from
`example/backend/.env` for local convenience.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "example" / "backend" / ".env"

QUEUE_PAIRS = {
    "webhook": ("webhook-events.fifo", "webhook-events-dlq.fifo"),
    "fulfillment": ("fulfillment-queue.fifo", "fulfillment-dlq.fifo"),
}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def sqs_client(allow_production: bool):
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or None
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    is_local = endpoint and ("localhost" in endpoint or "127.0.0.1" in endpoint)
    if not is_local and not allow_production:
        raise SystemExit(
            f"refusing to talk to non-LocalStack endpoint {endpoint!r}. "
            "Pass --allow-production to confirm a production DLQ run."
        )
    return boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name=region,
        config=BotoConfig(retries={"max_attempts": 2}),
    )


def queue_url(sqs, name: str) -> str:
    try:
        return sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist as exc:
        raise SystemExit(f"queue not found: {name}") from exc


def queue_pair(pair: str) -> tuple[str, str]:
    if pair not in QUEUE_PAIRS:
        raise SystemExit(f"unknown queue pair {pair!r}; choices: {list(QUEUE_PAIRS)}")
    return QUEUE_PAIRS[pair]


def receive_all(
    sqs,
    url: str,
    *,
    peek_visibility: int = 5,
    release_after_peek: bool = False,
) -> list[dict[str, Any]]:
    """Receive all messages from `url` without deleting them.

    `peek_visibility` is the visibility timeout we apply when receiving so the
    operator has a window to act before the message becomes visible again.

    `release_after_peek` flips visibility back to 0 immediately after receive,
    making the message visible to the very next `receive_message` call. Use
    this for read-only operations (`list`, `inspect`) so subsequent commands
    in the same shell don't see an empty queue. Destructive operations
    (`replay`, `delete`) should leave it False so the message stays locked
    long enough to act on it.
    """
    out: list[dict[str, Any]] = []
    while True:
        resp = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            VisibilityTimeout=peek_visibility,
            WaitTimeSeconds=1,
            MessageAttributeNames=["All"],
            AttributeNames=["All"],
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        out.extend(msgs)
        if len(msgs) < 10:
            break
    if release_after_peek:
        for m in out:
            try:
                sqs.change_message_visibility(
                    QueueUrl=url,
                    ReceiptHandle=m["ReceiptHandle"],
                    VisibilityTimeout=0,
                )
            except Exception:  # noqa: BLE001 - already-released is fine
                pass
    return out


def _format_short(msg: dict[str, Any]) -> str:
    mid = msg.get("MessageId", "?")[:18]
    body = msg.get("Body", "")
    body_preview = body[:80].replace("\n", " ")
    attrs = msg.get("Attributes", {})
    receives = attrs.get("ApproximateReceiveCount", "?")
    return f"  {mid}  receives={receives}  body={body_preview!r}"


# ---------- subcommands ----------


def cmd_list(args: argparse.Namespace) -> int:
    sqs = sqs_client(args.allow_production)
    _, dlq_name = queue_pair(args.pair)
    url = queue_url(sqs, dlq_name)
    msgs = receive_all(sqs, url, release_after_peek=True)
    if not msgs:
        print(f"{dlq_name}: empty")
        return 0
    print(f"{dlq_name}: {len(msgs)} message(s)")
    for m in msgs:
        print(_format_short(m))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    sqs = sqs_client(args.allow_production)
    _, dlq_name = queue_pair(args.pair)
    url = queue_url(sqs, dlq_name)
    msgs = receive_all(sqs, url, release_after_peek=True)
    if args.message_id:
        match = next((m for m in msgs if m.get("MessageId") == args.message_id), None)
    else:
        if not msgs:
            print(f"{dlq_name}: empty")
            return 0
        match = msgs[args.index]
    if match is None:
        print("no message matched", file=sys.stderr)
        return 2
    # Pretty-print body if it's JSON.
    body = match.get("Body", "")
    try:
        parsed = json.loads(body)
        body_out = json.dumps(parsed, indent=2)
    except json.JSONDecodeError:
        body_out = body
    print(
        json.dumps(
            {
                "MessageId": match.get("MessageId"),
                "Attributes": match.get("Attributes", {}),
                "MessageAttributes": match.get("MessageAttributes", {}),
                "BodyParsed": body_out if not body_out.startswith("{") else None,
            },
            indent=2,
        )
    )
    print("--- Body ---")
    print(body_out)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    sqs = sqs_client(args.allow_production)
    main_name, dlq_name = queue_pair(args.pair)
    main_url = queue_url(sqs, main_name)
    dlq_url = queue_url(sqs, dlq_name)
    # Dry-runs are read-only; release immediately so subsequent commands see
    # the messages. Real runs hold the lock for 60s to complete the publish.
    msgs = receive_all(
        sqs,
        dlq_url,
        peek_visibility=60 if not args.dry_run else 5,
        release_after_peek=args.dry_run,
    )
    targets = _select_targets(msgs, args)
    if not targets:
        print("no matching messages")
        return 0
    for m in targets:
        body = m.get("Body", "")
        attrs = m.get("MessageAttributes", {}) or {}
        group_id = (
            m.get("Attributes", {}).get("MessageGroupId") or args.group_id or "replayed"
        )
        dedup_id = args.dedup_prefix + ":" + m.get("MessageId", "?")
        print(
            f"replay {m.get('MessageId')} -> {main_name}  group={group_id}  dedup={dedup_id}"
        )
        if args.dry_run:
            continue
        sqs.send_message(
            QueueUrl=main_url,
            MessageBody=body,
            MessageGroupId=group_id,
            MessageDeduplicationId=dedup_id,
            MessageAttributes={
                k: {
                    "DataType": v.get("DataType", "String"),
                    "StringValue": v.get("StringValue", ""),
                }
                for k, v in attrs.items()
                if v.get("StringValue") is not None
            },
        )
        sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    sqs = sqs_client(args.allow_production)
    _, dlq_name = queue_pair(args.pair)
    url = queue_url(sqs, dlq_name)
    msgs = receive_all(
        sqs,
        url,
        peek_visibility=60 if not args.dry_run else 5,
        release_after_peek=args.dry_run,
    )
    targets = _select_targets(msgs, args)
    if not targets:
        print("no matching messages")
        return 0
    for m in targets:
        print(f"delete {m.get('MessageId')} from {dlq_name}")
        if args.dry_run:
            continue
        sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return 0


def _select_targets(
    msgs: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    if args.all:
        return msgs
    if args.message_id:
        return [m for m in msgs if m.get("MessageId") == args.message_id]
    return []


# ---------- argparse ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--allow-production",
        action="store_true",
        help="Allow non-LocalStack endpoints. Required for real AWS.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_pair(parser):
        parser.add_argument("pair", choices=list(QUEUE_PAIRS))

    lp = sub.add_parser("list", help="List DLQ messages (read-only)")
    _add_pair(lp)

    ip = sub.add_parser("inspect", help="Show one message's body and attributes")
    _add_pair(ip)
    ip.add_argument("--message-id", default=None)
    ip.add_argument("--index", type=int, default=0)

    rp = sub.add_parser("replay", help="Re-publish to main queue and delete from DLQ")
    _add_pair(rp)
    group = rp.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--message-id", default=None)
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument(
        "--dedup-prefix",
        default="replay",
        help="Prefix for the replayed MessageDeduplicationId. Default: 'replay'.",
    )
    rp.add_argument(
        "--group-id",
        default=None,
        help="Override MessageGroupId. Default: derive from original attributes.",
    )

    dp = sub.add_parser("delete", help="Drop messages from the DLQ (irreversible)")
    _add_pair(dp)
    group = dp.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--message-id", default=None)
    dp.add_argument("--dry-run", action="store_true")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_env(ENV_FILE)
    args = parse_args(argv)
    fn = {
        "list": cmd_list,
        "inspect": cmd_inspect,
        "replay": cmd_replay,
        "delete": cmd_delete,
    }[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
