import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Sheet } from '../src/index.js';

// Sets many cells at once: sheet(['A1', '1'], ['B1', '=A1'])
const sheet = (...cells) => {
  const s = new Sheet();
  for (const [ref, input] of cells) s.set(ref, input);
  return s;
};
const val = (input, ...cells) => sheet(...cells, ['ZZ1', input]).get('ZZ1');

// ------------------------------------------------------------ cells
test('empty cells', () => {
  const s = new Sheet();
  assert.equal(s.get('A1'), null);
  assert.equal(s.getInput('A1'), '');
});
test('number inputs', () => {
  const s = sheet(['A1', '42'], ['A2', ' 3.5 '], ['A3', '-2'], ['A4', '.5'], ['A5', '+7']);
  assert.deepEqual(['A1', 'A2', 'A3', 'A4', 'A5'].map((r) => s.get(r)), [42, 3.5, -2, 0.5, 7]);
  assert.equal(s.getInput('A2'), ' 3.5 ');
});
test('text inputs', () => {
  const s = sheet(['A1', 'hello'], ['A2', '12abc'], ['A3', ' padded ']);
  assert.equal(s.get('A1'), 'hello');
  assert.equal(s.get('A2'), '12abc');
  assert.equal(s.get('A3'), ' padded ');
});
test('clearing a cell', () => {
  const s = sheet(['A1', '1'], ['A1', '']);
  assert.equal(s.get('A1'), null);
  assert.equal(s.getInput('A1'), '');
});
test('addresses are case-insensitive, multi-letter columns', () => {
  const s = sheet(['a1', '5'], ['AA10', '7'], ['zz300', '1']);
  assert.equal(s.get('A1'), 5);
  assert.equal(s.get('aa10'), 7);
  assert.equal(s.get('ZZ300'), 1);
});
test('invalid addresses throw', () => {
  const s = new Sheet();
  for (const bad of ['1A', 'A0', 'A', 'ABCD1', 'A01', ''])
    assert.throws(() => s.get(bad), Error, `get(${JSON.stringify(bad)})`);
  assert.throws(() => s.set('A0', '1'), Error);
});

// ------------------------------------------------------------ arithmetic and operators
test('arithmetic and precedence', () => {
  assert.equal(val('=1+2*3'), 7);
  assert.equal(val('=(1+2)*3'), 9);
  assert.equal(val('=10/4'), 2.5);
  assert.equal(val('=10-4-3'), 3);
  assert.equal(val(' = 2 * ( 3 + 4 ) '.trimStart()), 14);
});
test('power is left-associative, unary binds tighter', () => {
  assert.equal(val('=2^3^2'), 64);
  assert.equal(val('=-2^2'), 4);
  assert.equal(val('=2*-3'), -6);
  assert.equal(val('=--3'), 3);
  assert.equal(val('=2^-1'), 0.5);
});
test('references', () => {
  assert.equal(val('=A1*2', ['A1', '3']), 6);
  assert.equal(val('=C1'), 0);
  assert.equal(val('=C1+1'), 1);
  assert.equal(val('=A1', ['A1', 'hi']), 'hi');
});
test('booleans in arithmetic', () => {
  assert.equal(val('=TRUE+1'), 2);
  assert.equal(val('=true*5'), 5);
  assert.equal(val('=FALSE'), false);
});
test('concatenation', () => {
  assert.equal(val('="a"&"b"'), 'ab');
  assert.equal(val('=1&2'), '12');
  assert.equal(val('="x"&TRUE'), 'xTRUE');
  assert.equal(val('="["&A1&"]"'), '[]');
  assert.equal(val('="say ""hi"""'), 'say "hi"');
  assert.equal(val('=2.5&""'), '2.5');
});
test('concat and comparison precedence', () => {
  assert.equal(val('="n"&1+2'), 'n3');
  assert.equal(val('=1+1=2'), true);
  assert.equal(val('="a"&"b"="AB"'), true);
});
test('comparisons', () => {
  assert.equal(val('=2<3'), true);
  assert.equal(val('=3<=2'), false);
  assert.equal(val('=2>=2'), true);
  assert.equal(val('=1<>2'), true);
  assert.equal(val('="abc"="ABC"'), true);
  assert.equal(val('="a"<"B"'), true);
  assert.equal(val('=TRUE>FALSE'), true);
});
test('comparisons with mixed types and empty cells', () => {
  assert.equal(val('=1="1"'), false);
  assert.equal(val('=1<>"1"'), true);
  assert.equal(val('=1<"1"'), '#VALUE!');
  assert.equal(val('=Z1=0'), true);
  assert.equal(val('=Z1=""'), true);
});

// ------------------------------------------------------------ errors
test('error values', () => {
  assert.equal(val('=1/0'), '#DIV/0!');
  assert.equal(val('="a"+1'), '#VALUE!');
  assert.equal(val('="3"*1'), '#VALUE!');
  assert.equal(val('=-"a"'), '#VALUE!');
  assert.equal(val('=foo'), '#NAME?');
  assert.equal(val('=FOO(1)'), '#NAME?');
  assert.equal(val('=(-8)^0.5'), '#NUM!');
});
test('syntax errors do not throw', () => {
  for (const f of ['=', '=1+', '=(1', '=1 2', '="abc', '=1)', '=SUM(1,', '=1+*2', '=#'])
    assert.equal(val(f), '#ERROR!', f);
});
test('errors propagate, first error wins', () => {
  const s = sheet(['A1', '=1/0'], ['B1', '=A1+1'], ['C1', '=SUM(A1, 5)'], ['D1', '=IF(TRUE, 1, A1)'],
    ['E1', '=(1/0)+("a"+1)'], ['F1', '="a"&A1']);
  assert.equal(s.get('B1'), '#DIV/0!');
  assert.equal(s.get('C1'), '#DIV/0!');
  assert.equal(s.get('D1'), 1);
  assert.equal(s.get('E1'), '#DIV/0!');
  assert.equal(s.get('F1'), '#DIV/0!');
});
test('ranges outside function arguments', () => {
  assert.equal(val('=A1:A2'), '#VALUE!');
  assert.equal(val('=A1:A2+1'), '#VALUE!');
  assert.equal(val('=ABS(A1:A2)'), '#VALUE!');
});

// ------------------------------------------------------------ functions
test('SUM over ranges ignores text, booleans, empty', () => {
  const cells = [['A1', '1'], ['A2', 'x'], ['A3', '=TRUE'], ['A5', '4']];
  assert.equal(val('=SUM(A1:A5)', ...cells), 5);
  assert.equal(val('=SUM(A5:A1)', ...cells), 5);
  assert.equal(val('=SUM(A1:A5, 10, TRUE)', ...cells), 16);
  assert.equal(val('=SUM(A1:A5, "x")', ...cells), '#VALUE!');
});
test('aggregates over 2D ranges', () => {
  const cells = [['A1', '1'], ['B1', '2'], ['A2', '3'], ['B2', '4']];
  assert.equal(val('=SUM(A1:B2)', ...cells), 10);
  assert.equal(val('=SUM(B2:A1)', ...cells), 10);
  assert.equal(val('=AVERAGE(A1:B2)', ...cells), 2.5);
  assert.equal(val('=MIN(A1:B2)', ...cells), 1);
  assert.equal(val('=MAX(A1:B2, 10)', ...cells), 10);
  assert.equal(val('=COUNT(A1:B2, "x", 5)', ...cells), 5);
});
test('aggregates with no numbers', () => {
  assert.equal(val('=AVERAGE(A1:A3)'), '#DIV/0!');
  assert.equal(val('=MIN(A1:A3)'), 0);
  assert.equal(val('=MAX(A1:A3)', ['A1', 'text']), 0);
  assert.equal(val('=COUNT(A1:A3)'), 0);
});
test('IF', () => {
  assert.equal(val('=IF(1>2, "yes", "no")'), 'no');
  assert.equal(val('=IF(0, 1)'), false);
  assert.equal(val('=IF(2, 1)'), 1);
  assert.equal(val('=IF(A9, 1, 2)'), 2);
  assert.equal(val('=IF("x", 1, 2)'), '#VALUE!');
  assert.equal(val('=IF(1, 2, 3, 4)'), '#VALUE!');
  assert.equal(val('=IF(FALSE, 1/0, 7)'), 7);
});
test('AND, OR, NOT', () => {
  assert.equal(val('=AND(TRUE, 1, 2>1)'), true);
  assert.equal(val('=AND(TRUE, 0)'), false);
  assert.equal(val('=OR(FALSE, 0)'), false);
  assert.equal(val('=OR(FALSE, 3)'), true);
  assert.equal(val('=NOT(0)'), true);
  assert.equal(val('=AND()'), '#VALUE!');
  assert.equal(val('=NOT("a")'), '#VALUE!');
});
test('ABS and ROUND', () => {
  assert.equal(val('=ABS(-3)'), 3);
  assert.equal(val('=ROUND(2.5)'), 3);
  assert.equal(val('=ROUND(-2.5)'), -3);
  assert.equal(val('=ROUND(1.2345, 2)'), 1.23);
  assert.equal(val('=ROUND(1234, -2)'), 1200);
});
test('LEN, UPPER, CONCAT', () => {
  assert.equal(val('=LEN("hello")'), 5);
  assert.equal(val('=LEN(12.5)'), 4);
  assert.equal(val('=LEN(A1)'), 0);
  assert.equal(val('=UPPER("abc"&1)'), 'ABC1');
  assert.equal(val('=CONCAT(A1:B2, "!")', ['A1', 'a'], ['B1', '2'], ['B2', '=TRUE']), 'a2TRUE!');
});
test('function names are case-insensitive', () => {
  assert.equal(val('=sum(1, 2)'), 3);
  assert.equal(val('=Max(1, 2)'), 2);
});
test('wrong number of arguments', () => {
  for (const f of ['=ABS(1, 2)', '=SUM()', '=LEN()', '=NOT(1, 2)', '=ROUND(1, 2, 3)', '=IF(1)'])
    assert.equal(val(f), '#VALUE!', f);
});

// ------------------------------------------------------------ recalculation and cycles
test('changes propagate through dependents', () => {
  const s = sheet(['A1', '1'], ['B1', '=A1+1'], ['C1', '=B1*10']);
  assert.equal(s.get('C1'), 20);
  s.set('A1', '5');
  assert.equal(s.get('C1'), 60);
  s.set('B1', 'text');
  assert.equal(s.get('C1'), '#VALUE!');
});
test('ranges pick up cells set later', () => {
  const s = sheet(['B1', '=SUM(A1:A3)']);
  assert.equal(s.get('B1'), 0);
  s.set('A2', '7');
  assert.equal(s.get('B1'), 7);
  s.set('A2', '');
  assert.equal(s.get('B1'), 0);
});
test('cycles', () => {
  const s = sheet(['A1', '=B1'], ['B1', '=A1'], ['C1', '=A1+1'], ['D1', '=D1'], ['E1', '=SUM(E1:E3)']);
  for (const r of ['A1', 'B1', 'C1', 'D1', 'E1']) assert.equal(s.get(r), '#CYCLE!', r);
  s.set('B1', '3');
  assert.equal(s.get('A1'), 3);
  assert.equal(s.get('C1'), 4);
});
test('cycles count references in untaken IF branches', () => {
  const s = sheet(['F1', '=IF(TRUE, 1, F2)'], ['F2', '=F1']);
  assert.equal(s.get('F1'), '#CYCLE!');
  assert.equal(s.get('F2'), '#CYCLE!');
});
test('long dependency chain, top to bottom', () => {
  const started = Date.now();
  const s = new Sheet();
  s.set('A1', '1');
  for (let i = 2; i <= 1000; i++) s.set(`A${i}`, `=A${i - 1}+1`);
  assert.equal(s.get('A1000'), 1000);
  s.set('A1', '5');
  assert.equal(s.get('A1000'), 1004);
  assert.ok(Date.now() - started < 10000, 'took too long');
});
test('long dependency chain, bottom to top', () => {
  const started = Date.now();
  const s = new Sheet();
  for (let i = 1; i < 1000; i++) s.set(`B${i}`, `=B${i + 1}+1`);
  s.set('B1000', '1');
  assert.equal(s.get('B1'), 1000);
  s.set('B1000', '2');
  assert.equal(s.get('B1'), 1001);
  assert.ok(Date.now() - started < 10000, 'took too long');
});

// ------------------------------------------------------------ row insertion
test('insertRow moves cells down', () => {
  const s = sheet(['A1', '1'], ['A2', '2'], ['A3', '3']);
  s.insertRow(2);
  assert.deepEqual(['A1', 'A2', 'A3', 'A4'].map((r) => s.get(r)), [1, null, 2, 3]);
  assert.equal(s.getInput('A3'), '2');
});
test('insertRow rewrites references and ranges', () => {
  const s = sheet(['A1', '1'], ['A2', '2'], ['A3', '3'], ['B1', '=SUM(A1:A3)'], ['B5', '=A3*2']);
  s.insertRow(2);
  assert.equal(s.getInput('B1'), '=SUM(A1:A4)');
  assert.equal(s.get('B1'), 6);
  assert.equal(s.getInput('B6'), '=A4*2');
  assert.equal(s.get('B6'), 6);
  assert.equal(s.get('B5'), null);
  s.set('A2', '10');
  assert.equal(s.get('B1'), 16);
});
test('insertRow leaves ranges above the row unchanged', () => {
  const s = sheet(['A1', '1'], ['C1', '=SUM(A1:A2)']);
  s.insertRow(3);
  assert.equal(s.getInput('C1'), '=SUM(A1:A2)');
});
test('insertRow at row 1', () => {
  const s = sheet(['A1', '5'], ['B1', '=A1']);
  s.insertRow(1);
  assert.equal(s.getInput('B2'), '=A2');
  assert.equal(s.get('B2'), 5);
  assert.equal(s.get('A1'), null);
});
test('insertRow only rewrites changed references', () => {
  const s = sheet(['A1', '=a2 & "A2" & a1'], ['A2', 'x']);
  s.insertRow(2);
  assert.equal(s.getInput('A1'), '=A3 & "A2" & a1');
  assert.equal(s.get('A1'), '#CYCLE!');
});
test('insertRow validates its argument', () => {
  const s = new Sheet();
  for (const bad of [0, -1, 1.5]) assert.throws(() => s.insertRow(bad), Error);
});

// ------------------------------------------------------------ change events
test('onChange reports changed addresses sorted by row then column', () => {
  const s = sheet(['A1', '1'], ['B1', '=A1+1'], ['A2', '=A1*2'], ['C3', '7']);
  const calls = [];
  s.onChange((refs) => calls.push(refs));
  s.set('A1', '2');
  assert.deepEqual(calls, [['A1', 'B1', 'A2']]);
});
test('onChange is not called without changes; unsubscribe works', () => {
  const s = sheet(['A1', '2']);
  const calls = [];
  const off = s.onChange((refs) => calls.push(refs));
  s.set('A1', '2');
  s.set('A1', ' 2');
  assert.deepEqual(calls, []);
  s.set('C5', 'x');
  s.set('C5', '');
  assert.deepEqual(calls, [['C5'], ['C5']]);
  off();
  s.set('C5', 'y');
  assert.equal(calls.length, 2);
});
test('onChange after insertRow', () => {
  const s = sheet(['A1', '1'], ['A2', '=A1']);
  const calls = [];
  s.onChange((refs) => calls.push(refs));
  s.insertRow(1);
  assert.deepEqual(calls, [['A1', 'A3']]);
});
