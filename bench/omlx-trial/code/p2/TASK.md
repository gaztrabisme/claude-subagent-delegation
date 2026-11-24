# Task: LRUCache

Implement the class `LRUCache` in `lru.py` (this folder). Keep the class name and
method names. Use the standard library only.

## Interface

- `LRUCache(capacity: int)`
- `get(key) -> value | None`
- `put(key, value) -> None`

## Behaviour

- `capacity` must be at least 1. If it is smaller, `__init__` raises `ValueError`.
- `get(key)` returns the stored value if the key is present, otherwise `None`.
  A successful `get` marks that key as the most recently used.
  A `get` that misses does not change the order of anything.
- `put(key, value)` stores the value and marks the key as the most recently used.
  - If the key is already present, update its value and mark it most recent.
    No eviction happens in this case.
  - If the key is new and the cache already holds `capacity` keys, evict the
    least recently used key first, then store the new one.
- Keys are any hashable value.

## Example

With `capacity=2`: `put("a", 1)`, `put("b", 2)`, `get("a")`, `put("c", 3)`.
Now `get("b")` returns `None` (b was least recently used), and `get("a")` returns 1.

## Files

- Write the implementation in `lru.py`, replacing the `NotImplementedError` stubs.
- The tests are in `tests/test_lru.py`. Do not edit them.
