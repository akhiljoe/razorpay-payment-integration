/**
 * Type contracts for the checkout widget.
 *
 * DESIGN_v5 Section 2.2. Notably:
 * - No `amount` prop. The server returns the authoritative amount in the
 *   CreateOrderResponse and the widget uses that value verbatim.
 * - `csp_blocked` is the dedicated PaymentError code when the Razorpay
 *   checkout script fails to load within 3 seconds.
 */

export interface PaymentResult {
  orderId: string;
  razorpayOrderId: string;
  razorpayPaymentId: string;
  razorpaySignature: string;
}

export interface PaymentError {
  code: string;
  description: string;
  reason: string;
  source: string;
  step: string;
  metadata: {
    orderId: string;
    paymentId: string;
  };
}

export interface CreateOrderResponse {
  order_id: string;
  razorpay_order_id: string;
  amount: number;
  currency: string;
  key_id: string;
}

export interface CheckoutOptions {
  /** URL to POST {receipt, currency} for order creation. */
  orderEndpoint: string;
  /** Server-side idempotency key. */
  receipt: string;
  /** ISO 4217 currency code. Defaults to "INR". */
  currency?: string;
  /** Button label for React/vanilla adapters. Defaults to "Pay". */
  buttonLabel?: string;
  /** Theme passed through to Razorpay's modal. */
  theme?: { color?: string };
  /** Display name shown in the Razorpay modal. */
  modalName?: string;
  /** Description shown in the Razorpay modal. */
  modalDescription?: string;

  onSuccess: (result: PaymentResult) => void;
  onFailure: (error: PaymentError) => void;
  onDismiss: () => void;
}

export interface RazorpayCheckoutProps extends CheckoutOptions {}

/** Minimal shape of the global `window.Razorpay` constructor we rely on. */
export interface RazorpayInstance {
  open: () => void;
  on?: (event: string, handler: (...args: unknown[]) => void) => void;
}

export interface RazorpayConstructor {
  new (options: Record<string, unknown>): RazorpayInstance;
}

declare global {
  interface Window {
    Razorpay?: RazorpayConstructor;
  }
}
