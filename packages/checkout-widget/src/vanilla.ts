/**
 * Vanilla DOM adapter. DESIGN_v5 Section 2.2.
 *
 * `mountCheckout(element, options)` appends a button to `element`, wires it
 * to `runCheckout`, and returns an `unmount()` function the host can call
 * to remove the button on teardown (e.g., SPA route change).
 */

import { runCheckout } from "./core";
import type { CheckoutOptions } from "./types";

export function mountCheckout(
  element: HTMLElement,
  options: CheckoutOptions,
): () => void {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = options.buttonLabel ?? "Pay";
  button.dataset["testid"] = "razorpay-checkout-button";

  let busy = false;
  const onClick = async (): Promise<void> => {
    if (busy) {
      return;
    }
    busy = true;
    const original = button.textContent;
    button.textContent = "Loading…";
    button.disabled = true;
    try {
      await runCheckout(options);
    } finally {
      busy = false;
      button.textContent = original;
      button.disabled = false;
    }
  };

  button.addEventListener("click", onClick);
  element.appendChild(button);

  return function unmount(): void {
    button.removeEventListener("click", onClick);
    if (button.parentNode === element) {
      element.removeChild(button);
    }
  };
}
