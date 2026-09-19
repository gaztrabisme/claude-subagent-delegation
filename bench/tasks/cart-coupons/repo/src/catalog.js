import { assertCents } from './money.js';

export class Catalog {
  constructor(products = []) {
    this.products = new Map();
    for (const product of products) this.add(product);
  }

  add({ sku, name, price, category = 'general' }) {
    if (!sku || typeof sku !== 'string') throw new TypeError('sku is required');
    assertCents(price, 'price');
    this.products.set(sku, { sku, name: name ?? sku, price, category });
    return this;
  }

  get(sku) {
    const product = this.products.get(sku);
    if (!product) throw new Error(`Unknown product: ${sku}`);
    return product;
  }
}
