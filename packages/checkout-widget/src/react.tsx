/**
 * React adapter. DESIGN_v5 Section 2.2.
 *
 * No `amount` prop. The widget triggers `runCheckout` on click and lets the
 * server return the authoritative amount in the `CreateOrderResponse`.
 */

import { useCallback, useState } from "react";

import { runCheckout } from "./core";
import type { RazorpayCheckoutProps } from "./types";

export function RazorpayCheckout(props: RazorpayCheckoutProps): JSX.Element {
  const [busy, setBusy] = useState(false);
  const label = props.buttonLabel ?? "Pay";

  const onClick = useCallback(async () => {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      await runCheckout(props);
    } finally {
      setBusy(false);
    }
  }, [busy, props]);

  return (
    <button
      type="button"
      disabled={busy}
      onClick={onClick}
      data-testid="razorpay-checkout-button"
    >
      {busy ? "Loading…" : label}
    </button>
  );
}

export type { RazorpayCheckoutProps, PaymentResult, PaymentError } from "./types";
