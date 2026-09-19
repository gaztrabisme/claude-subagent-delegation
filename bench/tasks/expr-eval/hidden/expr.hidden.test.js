import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluate } from '../src/expr.js';

test('numbers', () => { assert.equal(evaluate('42'), 42); assert.equal(evaluate('4.5'), 4.5); assert.equal(evaluate('.5'), 0.5); });
test('whitespace', () => assert.equal(evaluate('  1 +\t2 '), 3));
test('precedence', () => { assert.equal(evaluate('2+3*4'), 14); assert.equal(evaluate('(2+3)*4'), 20); assert.equal(evaluate('10-4-3'), 3); assert.equal(evaluate('100/10/5'), 2); });
test('modulo', () => assert.equal(evaluate('7 % 4 * 2'), 6));
test('power right-assoc', () => assert.equal(evaluate('2^3^2'), 512));
test('unary minus vs power', () => { assert.equal(evaluate('-2^2'), -4); assert.equal(evaluate('(-2)^2'), 4); assert.equal(evaluate('2^-1'), 0.5); });
test('repeated unary', () => { assert.equal(evaluate('--2'), 2); assert.equal(evaluate('-+-3'), 3); assert.equal(evaluate('3 - -2'), 5); });
test('variables', () => { assert.equal(evaluate('x * y + _z1', { x: 2, y: 3, _z1: 1 }), 7); });
test('functions', () => {
  assert.equal(evaluate('min(3, 1, 2)'), 1);
  assert.equal(evaluate('max(4)'), 4);
  assert.equal(evaluate('abs(-5) + sqrt(16)'), 9);
  assert.equal(evaluate('max(1, min(5, x) * 2)', { x: 3 }), 6);
});
test('function name as variable', () => assert.equal(evaluate('min + 1', { min: 1 }), 2));
test('syntax errors', () => {
  for (const e of ['', '   ', '1 +', '(1 + 2', '1 + 2)', '1 2', '*3', 'min()', 'abs(1, 2)', 'sqrt()', '3 $ 4', 'max(1,)'])
    assert.throws(() => evaluate(e), SyntaxError, `expected SyntaxError for ${JSON.stringify(e)}`);
});
test('reference errors', () => {
  assert.throws(() => evaluate('a + 1'), ReferenceError);
  assert.throws(() => evaluate('foo(1)'), ReferenceError);
});
test('range errors', () => {
  assert.throws(() => evaluate('1 / 0'), RangeError);
  assert.throws(() => evaluate('5 % (2 - 2)'), RangeError);
});
