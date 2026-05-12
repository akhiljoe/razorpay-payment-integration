"""Razorpay REST API client.

v1 surface area: order creation. Webhook signature verification is in
`signatures.py`. Reconciliation pagination lands in a later phase as a
dedicated reader.

Auth: HTTP Basic with `key_id:key_secret` (Razorpay standard).
Base URL: `https://api.razorpay.com` (live and test share the same host).
Timeout: configurable via `RazorpayConfig.razorpay_api_timeout`.

Exception contract:

- `RazorpayUnavailableError` for transport failures and 5xx. Caller maps to
  HTTP 503 so Razorpay (or our own client) retries.
- `RazorpayAPIError` for 4xx with the response body attached. These are
  configuration or input errors and should not be retried automatically.
"""

from __future__ import annotations

from typing import Any

import httpx

from razorpay_checkout.config import RazorpayConfig
from razorpay_checkout.logging_config import get_logger

_log = get_logger("razorpay_checkout.client")

_BASE_URL = "https://api.razorpay.com"


class RazorpayUnavailableError(Exception):
    """Razorpay's API is unreachable or returned 5xx. Retryable."""


class RazorpayAPIError(Exception):
    """Razorpay returned a 4xx (typically input or auth error). Not retryable."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Razorpay {status_code}: {body!r}")


class RazorpayClient:
    """Thin async wrapper around Razorpay REST endpoints used by this package."""

    def __init__(self, config: RazorpayConfig, *, base_url: str = _BASE_URL) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(config.key_id, config.key_secret.get_secret_value()),
            timeout=config.razorpay_api_timeout,
            headers={"Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RazorpayClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def create_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/orders.

        Razorpay returns the created order including `id` (the `razorpay_order_id`).
        Raises `RazorpayUnavailableError` on timeout, transport error, or 5xx.
        Raises `RazorpayAPIError` on 4xx with the response body attached.
        """
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "receipt": receipt,
            "notes": notes or {},
        }
        try:
            response = await self._client.post("/v1/orders", json=payload)
        except httpx.TimeoutException as exc:
            _log.warning(
                "razorpay_create_order_timeout",
                receipt=receipt,
                timeout_s=self._config.razorpay_api_timeout,
            )
            raise RazorpayUnavailableError("Razorpay API timeout") from exc
        except httpx.TransportError as exc:
            _log.warning("razorpay_create_order_transport_error", error=str(exc))
            raise RazorpayUnavailableError(f"Razorpay transport error: {exc}") from exc

        if response.status_code >= 500:
            _log.warning(
                "razorpay_create_order_5xx",
                status_code=response.status_code,
                receipt=receipt,
            )
            raise RazorpayUnavailableError(
                f"Razorpay returned {response.status_code}"
            )

        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            raise RazorpayAPIError(response.status_code, body)

        data: dict[str, Any] = response.json()
        return data
