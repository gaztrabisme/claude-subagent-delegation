"""A fixed-capacity least-recently-used cache."""


class LRUCache:
    def __init__(self, capacity: int):
        """Create a cache holding at most `capacity` keys. See TASK.md."""
        raise NotImplementedError

    def get(self, key):
        """Return the value for `key`, or None. A hit marks the key most recent."""
        raise NotImplementedError

    def put(self, key, value) -> None:
        """Store `value` under `key`, evicting the least recently used key if full."""
        raise NotImplementedError
