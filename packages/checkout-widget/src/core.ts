/**
 * Internal core flow used by both the React and vanilla adapters.
 *
 * DESIGN_v5 Section 2.2 internal core flow:
 *  1. POST `{ receipt, currency }` to `orderEndpoint`.
 *  2. Inject `https://checkout.razorpay.com/v1/checkout.js` if `window.Razorpay`
 *     is not defined. Wait up to 3 seconds. If still undefined, call
 *     `onFailure` with `code: "csp_blocked"`.
 *  3. Construct `Razorpay(options)` with the server-returned `amount`. The
 *     client never sets an amount.
 *  4. Open modal. Map Razorpay callbacks to PaymentResult / PaymentError.
 */

import type {
  CheckoutOptions,
  CreateOrderResponse,
  PaymentError,
  PaymentResult,
} from "./types";

const RAZORPAY_SCRIPT_SRC = "https://checkout.razorpay.com/v1/checkout.js";
const SCRIPT_LOAD_TIMEOUT_MS = 3000;

export async function runCheckout(opts: CheckoutOptions): Promise<void> {
  let order: CreateOrderResponse;
  try {
    order = await fetchOrder(opts);
  } catch (err) {
    opts.onFailure(makeError({
      code: "order_create_failed",
      description: "Failed to create order on the server",
      reason: String(err),
      source: "client",
      step: "create_order",
    }));
    return;
  }

  const ready = await ensureRazorpayLoaded();
  if (!ready) {
    opts.onFailure(makeError({
      code: "csp_blocked",
      description:
        "Razorpay checkout script blocked or failed to load within 3 seconds",
      reason: "csp",
      source: "client",
      step: "load_script",
      metadata: {
        orderId: order.order_id,
        paymentId: "",
      },
    }));
    return;
  }

  openModal(opts, order);
}

// ---------- 1. POST /payments/orders ----------

async function fetchOrder(opts: CheckoutOptions): Promise<CreateOrderResponse> {
  const response = await fetch(opts.orderEndpoint, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      receipt: opts.receipt,
      currency: opts.currency ?? "INR",
    }),
  });
  if (!response.ok) {
    throw new Error(`order endpoint returned ${response.status}`);
  }
  const data = (await response.json()) as CreateOrderResponse;
  if (!data.razorpay_order_id || !data.key_id || typeof data.amount !== "number") {
    throw new Error("order response missing required fields");
  }
  return data;
}

// ---------- 2. Razorpay script injection with 3s timeout ----------

export async function ensureRazorpayLoaded(
  timeoutMs: number = SCRIPT_LOAD_TIMEOUT_MS,
): Promise<boolean> {
  if (typeof window === "undefined") {
    return false;
  }
  if (window.Razorpay) {
    return true;
  }
  injectScriptOnce();
  return await waitForRazorpay(timeoutMs);
}

function injectScriptOnce(): void {
  const id = "razorpay-checkout-script";
  if (document.getElementById(id)) {
    return;
  }
  const s = document.createElement("script");
  s.id = id;
  s.src = RAZORPAY_SCRIPT_SRC;
  s.async = true;
  document.head.appendChild(s);
}

function waitForRazorpay(timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = (): void => {
      if (window.Razorpay) {
        resolve(true);
        return;
      }
      if (Date.now() - start >= timeoutMs) {
        resolve(false);
        return;
      }
      window.setTimeout(tick, 50);
    };
    tick();
  });
}

// ---------- 3 + 4. Construct + open modal ----------

function openModal(opts: CheckoutOptions, order: CreateOrderResponse): void {
  const Razorpay = window.Razorpay;
  if (!Razorpay) {
    // Defensive — `ensureRazorpayLoaded` already returned true.
    opts.onFailure(makeError({
      code: "csp_blocked",
      description: "window.Razorpay disappeared between load check and open",
      reason: "race",
      source: "client",
      step: "open_modal",
      metadata: { orderId: order.order_id, paymentId: "" },
    }));
    return;
  }

  const rzp = new Razorpay({
    key: order.key_id,
    amount: order.amount, // server-authoritative
    currency: order.currency,
    order_id: order.razorpay_order_id,
    name: opts.modalName ?? "Razorpay Checkout",
    description: opts.modalDescription ?? "Secure payment",
    theme: opts.theme ?? {},
    handler: (response: Record<string, unknown>) => {
      opts.onSuccess({
        orderId: order.order_id,
        razorpayOrderId: order.razorpay_order_id,
        razorpayPaymentId: String(response["razorpay_payment_id"] ?? ""),
        razorpaySignature: String(response["razorpay_signature"] ?? ""),
      });
    },
    modal: {
      ondismiss: () => opts.onDismiss(),
    },
  });

  if (typeof rzp.on === "function") {
    rzp.on("payment.failed", (...args: unknown[]) => {
      const payload = (args[0] ?? {}) as { error?: Record<string, unknown> };
      const e = payload.error ?? {};
      opts.onFailure({
        code: String(e["code"] ?? "payment_failed"),
        description: String(e["description"] ?? "Payment failed"),
        reason: String(e["reason"] ?? "unknown"),
        source: String(e["source"] ?? "razorpay"),
        step: String(e["step"] ?? "payment"),
        metadata: {
          orderId: order.order_id,
          paymentId: String((e["metadata"] as Record<string, unknown> | undefined)?.["payment_id"] ?? ""),
        },
      });
    });
  }

  rzp.open();
}

// ---------- helpers ----------

function makeError(
  partial: Partial<PaymentError> & { code: string; description: string },
): PaymentError {
  return {
    code: partial.code,
    description: partial.description,
    reason: partial.reason ?? "unknown",
    source: partial.source ?? "client",
    step: partial.step ?? "unknown",
    metadata: partial.metadata ?? { orderId: "", paymentId: "" },
  };
}
