import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Catalog, Cart } from '../src/index.js';

const catalog = () => new Catalog([
  { sku: 'pen', name: 'Pen', price: 150, category: 'office' },
  { sku: 'book', name: 'Book', price: 1999, category: 'books' },
  { sku: 'lamp', name: 'Lamp', price: 3333, category: 'home' },
]);

test('no coupon keeps old behavior', () => {
  const cart = new Cart(catalog()).add('pen', 2).add('book');
  assert.equal(cart.coupon, null);
  assert.equal(cart.discount(), 0);
  assert.equal(cart.total(), 2529);
});
test('SAVE10 with rounding and tax on discounted amount', () => {
  const cart = new Cart(catalog()).add('lamp').applyCoupon('SAVE10');
  assert.equal(cart.discount(), 333);          // round(333.3)
  assert.equal(cart.tax(), 300);               // round(3000 * 0.1)
  assert.equal(cart.total(), 3333 - 333 + 300);
});
test('codes are case-insensitive and trimmed', () => {
  const cart = new Cart(catalog()).add('lamp').applyCoupon('  save10 ');
  assert.equal(cart.coupon, 'SAVE10');
});
test('FIVEOFF threshold is re-evaluated', () => {
  const cart = new Cart(catalog()).add('book').applyCoupon('FIVEOFF');
  assert.equal(cart.discount(), 0);            // 1999 < 2500
  cart.add('pen', 4);                          // 2599
  assert.equal(cart.discount(), 500);
  assert.equal(cart.total(), 2599 - 500 + Math.round(2099 * 0.1));
  cart.remove('pen', 1);                       // 2449
  assert.equal(cart.discount(), 0);
});
test('FIVEOFF exactly at threshold', () => {
  const cat = new Catalog([{ sku: 'x', price: 2500 }]);
  assert.equal(new Cart(cat).add('x').applyCoupon('FIVEOFF').discount(), 500);
});
test('BOOKS20 only discounts books', () => {
  const cart = new Cart(catalog()).add('book', 2).add('pen').applyCoupon('BOOKS20');
  assert.equal(cart.discount(), Math.round(3998 * 0.2));
});
test('replace and remove coupon', () => {
  const cart = new Cart(catalog()).add('lamp').applyCoupon('SAVE10').applyCoupon('BOOKS20');
  assert.equal(cart.coupon, 'BOOKS20');
  assert.equal(cart.discount(), 0);
  assert.equal(cart.removeCoupon().coupon, null);
  assert.equal(cart.total(), 3333 + 333);
});
test('unknown coupon', () => {
  const cart = new Cart(catalog()).add('pen');
  assert.throws(() => cart.applyCoupon('NOPE'), /Unknown coupon/);
  assert.equal(cart.coupon, null);
});
test('discount never exceeds subtotal', () => {
  const cat = new Catalog([{ sku: 'x', price: 2500 }]);
  const cart = new Cart(cat, { taxRate: 0 }).add('x').applyCoupon('FIVEOFF');
  assert.ok(cart.discount() <= cart.subtotal());
  assert.equal(new Cart(cat).applyCoupon('SAVE10').discount(), 0);
});
