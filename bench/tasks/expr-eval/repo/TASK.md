# Task: arithmetic expression evaluator

Create `src/expr.js` exporting `evaluate(expression, variables = {})` (ESM named export, no dependencies)
that returns a number.

## Grammar
- Numbers: integers and decimals (`3`, `4.5`, `.5`). Whitespace is ignored.
- Binary operators, lowest to highest precedence:
  1. `+` `-` (left-associative)
  2. `*` `/` `%` (left-associative)
  3. unary `-` and `+` (prefix, may repeat: `--2` is `2`)
  4. `^` power (right-associative, and binds tighter than unary minus: `-2^2` is `-4`, `2^-1` is `0.5`)
- Parentheses for grouping.
- Variables: identifiers matching `[A-Za-z_][A-Za-z0-9_]*`, looked up in `variables`.
- Function calls: `min(a, b, ...)`, `max(a, b, ...)` (one or more args), `abs(x)`, `sqrt(x)` (exactly one arg).
  A function name is only a function when followed by `(`.

## Errors
- Malformed input (unexpected token, missing parenthesis, empty expression, trailing input,
  wrong number of function arguments): throw `SyntaxError`.
- Unknown variable or unknown function name: throw `ReferenceError`.
- Division or modulo by zero: throw `RangeError`.
