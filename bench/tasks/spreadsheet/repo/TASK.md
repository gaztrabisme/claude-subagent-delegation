# Task: spreadsheet engine

Build an in-memory spreadsheet engine in plain JavaScript (ESM, Node 20, no dependencies).
Export a `Sheet` class from `src/index.js`. Split the code into sensible modules
(for example references, tokenizer, parser, evaluator, sheet).

## Cell addresses
- Column letters (1 to 3 letters, `A`..`Z`, `AA`.., case-insensitive) followed by a row number >= 1
  with no leading zero: `A1`, `b7`, `AA10`, `zz300`.
- API methods throw an `Error` for an invalid address (`1A`, `A0`, `A`, `ABCD1`, `A01`).
- Addresses returned by the API are upper case.

## API
- `sheet.set(address, input)`: `input` is a string.
  - `''` clears the cell.
  - If the trimmed input matches `^[+-]?(\d+\.?\d*|\.\d+)$`, the cell holds that number.
  - If the input starts with `=`, it is a formula.
  - Otherwise it is text (kept exactly as given).
- `sheet.get(address)`: the computed value: a number, a string, a boolean, `null` for an empty cell,
  or an error code string (see Errors). A formula whose result is an empty cell evaluates to `0`.
- `sheet.getInput(address)`: the raw input string, or `''` for an empty cell.
- `sheet.insertRow(n)`: see Row insertion. Throws an `Error` unless `n` is an integer >= 1.
- `sheet.onChange(listener)`: see Change events. Returns an unsubscribe function.

Values must always be up to date: changing a cell updates every formula that depends on it.

## Formulas
Whitespace between tokens is ignored.
- Numbers: `3`, `4.5`, `.5` (no exponents).
- Strings: `"text"`; a doubled quote `""` inside a string is a literal quote.
- Booleans: `TRUE`, `FALSE` (case-insensitive).
- Cell references (`A1`, case-insensitive) and ranges (`A1:B3`; the corners may be given in any order).
  A range is only allowed directly as a function argument; anywhere else it evaluates to `#VALUE!`.
- Function calls: `NAME(arg, ...)`; names are case-insensitive. An identifier followed by `(` is a function
  name. Any other identifier that is not a reference or boolean is an unknown name (`#NAME?`).
- Operators, lowest to highest precedence, all left-associative:
  1. comparison `=` `<>` `<` `>` `<=` `>=` (result is a boolean)
  2. `&` text concatenation
  3. `+` `-`
  4. `*` `/`
  5. `^` power (left-associative: `2^3^2` is `64`)
  6. unary prefix `-` and `+` (bind tighter than `^`: `-2^2` is `4`)
  7. parentheses

### Coercion
- To number (arithmetic, unary minus, scalar arguments of numeric functions): numbers as is,
  `TRUE`/`FALSE` -> `1`/`0`, empty -> `0`, text -> `#VALUE!` (text never converts, even `"3"`).
- To text (`&`, `LEN`, `UPPER`, `CONCAT`): numbers via JavaScript `String(n)`, booleans `"TRUE"`/`"FALSE"`,
  empty -> `""`.
- To boolean (`IF` condition, `AND`, `OR`, `NOT`): booleans as is, numbers -> `n !== 0`, empty -> `FALSE`,
  text -> `#VALUE!`.
- Comparison: an empty operand is `""` when the other operand is text, otherwise `0`. Two numbers compare
  numerically, two booleans with `FALSE < TRUE`, two strings case-insensitively. Operands of different
  types: `=` is `FALSE`, `<>` is `TRUE`, the other comparisons are `#VALUE!`.

### Functions
- `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT` (1+ arguments). Range arguments contribute only their number
  cells (text, booleans and empty cells are ignored). Scalar arguments are coerced to numbers, except
  `COUNT`, which counts scalar arguments that are numbers and ignores the rest.
  `AVERAGE` with no numbers is `#DIV/0!`; `MIN`/`MAX` with no numbers is `0`.
- `IF(condition, then [, else])`: only the chosen branch is evaluated; a missing else gives `FALSE`.
- `AND(...)`, `OR(...)` (1+ arguments), `NOT(x)`.
- `ABS(x)`, `ROUND(x [, digits])`: digits default `0` and may be negative; halves round away from zero
  (`ROUND(-2.5)` is `-3`).
- `LEN(x)`, `UPPER(x)`.
- `CONCAT(...)` (1+ arguments): ranges contribute their cells in row-major order.
- Wrong number of arguments, or a range passed where a single value is expected: `#VALUE!`.

### Errors
Error values are the strings `#DIV/0!` (division by zero), `#VALUE!` (wrong type, see above), `#NAME?`
(unknown function or name), `#NUM!` (non-finite arithmetic result such as `(-8)^0.5`), `#CYCLE!`, and
`#ERROR!` (the formula cannot be parsed; `set` must not throw).
An error in an operand or argument propagates: the result is the first error encountered, evaluating
left to right (ranges in row-major order).

### Cycles
A cell is in a cycle if it can reach itself by following references (every reference in its formula
counts, including ranges and IF branches that are not taken). Cells in a cycle evaluate to `#CYCLE!`;
other cells that use their value get `#CYCLE!` through normal error propagation. Breaking the cycle
restores normal values. Dependency chains of 1000+ cells must work (no stack overflow) in either
direction.

## Row insertion
`insertRow(n)` inserts an empty row before row `n`:
- every cell in row `n` or below moves down one row;
- in every formula on the sheet, each cell reference with row >= `n` has its row increased by one
  (both corners of a range are adjusted independently, so a range spanning row `n` grows);
- only the references that change are rewritten (in upper case); everything else in the formula text,
  including string literals, stays exactly as typed. Formulas that cannot be parsed are left unchanged.

## Change events
After each `set` or `insertRow`, every registered listener is called once with the addresses whose
`get` value changed (compared with `===`), sorted by row and then by column. Listeners are not called
if nothing changed.
