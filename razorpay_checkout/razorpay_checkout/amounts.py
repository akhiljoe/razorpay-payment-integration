"""Pluggable amount resolution.

The server is authoritative for `amount`. Clients do not send it. Host apps
register an `AmountResolver` that maps a `CreateOrderRequest` to an integer
price in the smallest currency unit (paise for INR).

`FixedAmountResolver` is the development default, returning a constant amount.
Production hosts should implement their own resolver that does a product
lookup against their catalog.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from razorpay_checkout.models import CreateOrderRequest


@runtime_checkable
class AmountResolver(Protocol):
    """Compute the authoritative amount for a `CreateOrderRequest`."""

    async def resolve(self, request: CreateOrderRequest, tenant_id: str) -> int:
        """Return amount in the smallest currency unit (paise for INR).

        Implementations should raise on any condition that prevents pricing
        (unknown product, catalog outage, etc.). The route handler maps the
        exception to a 4xx response.
        """
        ...


class FixedAmountResolver:
    """Returns a constant amount irrespective of the request.

    Development default. Inject a real resolver in production via host app DI.
    """

    def __init__(self, amount: int) -> None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._amount = amount

    async def resolve(self, request: CreateOrderRequest, tenant_id: str) -> int:
        return self._amount


class AmountResolutionError(Exception):
    """Raised by resolvers when amount cannot be determined.

    The route handler converts to 400 with a structured body.
    """
