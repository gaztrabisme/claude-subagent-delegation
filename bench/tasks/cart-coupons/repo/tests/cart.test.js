import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Catalog, Cart, formatCents } from '../src/index.js';

const catalog = () => new Catalog([
  { sku: 'pen', name: 'Pen', price: 150, category: 'office' },
  { sku: 'book', name: 'Book', price: 1999, category: 'books' },
]);

test('subtotal, tax, total', () => {
  const cart = new Cart(catalog()).add('pen', 2).add('book');
  assert.equal(cart.subtotal(), 2299);
  assert.equal(cart.tax(), 230);
  assert.equal(cart.total(), 2529);
});

test('remove', () => {
  const cart = new Cart(catalog()).add('pen', 3).remove('pen', 1);
  assert.equal(cart.items()[0].qty, 2);
  cart.remove('pen');
  assert.equal(cart.items().length, 0);
});

test('validation', () => {
  assert.throws(() => new Cart(catalog()).add('nope'), /Unknown product/);
  assert.throws(() => new Cart(catalog()).add('pen', 0), RangeError);
});

test('formatCents', () => assert.equal(formatCents(2529), '$25.29'));
