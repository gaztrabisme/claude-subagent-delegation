export class Cart {
  constructor(catalog, { taxRate = 0.1 } = {}) {
    this.catalog = catalog;
    this.taxRate = taxRate;
    this.lines = new Map(); // sku -> qty
  }

  add(sku, qty = 1) {
    if (!Number.isInteger(qty) || qty <= 0) throw new RangeError('qty must be a positive integer');
    this.catalog.get(sku); // validates sku
    this.lines.set(sku, (this.lines.get(sku) ?? 0) + qty);
    return this;
  }

  remove(sku, qty = Infinity) {
    const current = this.lines.get(sku);
    if (current === undefined) return this;
    if (qty >= current) this.lines.delete(sku);
    else this.lines.set(sku, current - qty);
    return this;
  }

  items() {
    return [...this.lines].map(([sku, qty]) => {
      const { name, price, category } = this.catalog.get(sku);
      return { sku, name, category, unitPrice: price, qty, lineTotal: price * qty };
    });
  }

  subtotal() {
    return this.items().reduce((sum, item) => sum + item.lineTotal, 0);
  }

  tax() {
    return Math.round(this.subtotal() * this.taxRate);
  }

  total() {
    return this.subtotal() + this.tax();
  }
}
