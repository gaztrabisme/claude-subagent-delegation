# Task: coupon support for the cart

Add coupons to the existing cart library.

## Coupon definitions
Create `src/coupons.js` with a registry of these coupons:
- `SAVE10`: 10% off the subtotal.
- `FIVEOFF`: $5.00 off, only when the subtotal is at least $25.00 (otherwise the discount is 0).
- `BOOKS20`: 20% off the line totals of items in category `books`.

## Cart API
- `cart.applyCoupon(code)`: codes are case-insensitive and surrounding whitespace is ignored.
  Only one coupon can be active; applying a new one replaces the previous one. Unknown codes throw
  an `Error` whose message contains `Unknown coupon`. Returns the cart (chainable).
- `cart.removeCoupon()`: removes the active coupon. Returns the cart.
- `cart.coupon`: the active coupon code in upper case, or `null`.
- `cart.discount()`: discount in integer cents, rounded with `Math.round`, never more than the subtotal.
  It is computed from the cart's current contents every time (coupon conditions are re-evaluated).
- Tax is charged on the discounted amount: `tax() = Math.round((subtotal - discount) * taxRate)`.
- `total() = subtotal - discount + tax`.
- Export anything new that users need from `src/index.js`.

Existing behavior and tests must keep working.
