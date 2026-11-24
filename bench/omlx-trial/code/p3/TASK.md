# Task: slugify

Implement `slugify(text: str, max_len: int = 60) -> str` in `slug.py` (this folder).
Keep the name and signature. Use the standard library only.

## Rules, applied in this order

1. Fold accented Latin letters to ASCII: decompose with Unicode NFKD and drop the
   combining marks, so `é` becomes `e`, `Å` becomes `A`, `č` becomes `c`.
   Characters that have no ASCII form (for example CJK characters) are dropped.
2. Lowercase the result.
3. Replace each run of characters that are not `a-z` or `0-9` with a single `-`.
4. Strip leading and trailing `-`.
5. Truncate to at most `max_len` characters. If the cut leaves a trailing `-`,
   remove it.
6. If the result is empty, return `"n-a"`.

If `max_len` is less than 1, raise `ValueError`.

## Examples

- `"Hello World"` -> `"hello-world"`
- `"Crème Brûlée"` -> `"creme-brulee"`
- `"  Multiple   spaces & symbols!!  "` -> `"multiple-spaces-symbols"`
- `"one two three four five"` with `max_len=8` -> `"one-two"`
- `"!!!"` -> `"n-a"`
- `"日本語"` -> `"n-a"`

## Files

- Write the implementation in `slug.py`, replacing the `NotImplementedError` stub.
- The tests are in `tests/test_slug.py`. Do not edit them.
