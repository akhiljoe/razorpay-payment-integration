/**
 * Public entry. Host apps may import from `@akhiljoe/checkout-widget`
 * (this file), `/react` (React component), or `/vanilla` (DOM adapter).
 */

export { runCheckout, ensureRazorpayLoaded } from "./core";
export { RazorpayCheckout } from "./react";
export { mountCheckout } from "./vanilla";
export type {
  CheckoutOptions,
  CreateOrderResponse,
  PaymentError,
  PaymentResult,
  RazorpayCheckoutProps,
} from "./types";
