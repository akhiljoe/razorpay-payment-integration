"""AWS Lambda entry points.

Each module here exposes a synchronous `handler(event, context)` that AWS
Lambda invokes. The handlers drive async coroutines via `asyncio.run`. The
DB pool and SQS client are constructed at module import time so they survive
across warm invocations.
"""
