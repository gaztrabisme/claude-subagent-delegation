import pytest

from lru import LRUCache


def test_get_missing_key_returns_none():
    cache = LRUCache(2)
    assert cache.get("nope") is None


def test_put_then_get_returns_value():
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_evicts_least_recently_used_when_full():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_get_refreshes_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1


def test_put_existing_key_updates_value_and_refreshes_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 10


def test_put_existing_key_does_not_evict():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 5)
    assert cache.get("a") == 5
    assert cache.get("b") == 2


def test_capacity_one_keeps_only_latest():
    cache = LRUCache(1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_miss_does_not_change_order():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("zzz") is None
    cache.put("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_eviction_order_across_three_slots():
    cache = LRUCache(3)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    cache.get("a")
    cache.get("b")
    cache.put("d", 4)
    assert cache.get("c") is None
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("d") == 4


def test_many_puts_keep_only_capacity_keys():
    cache = LRUCache(3)
    for i in range(10):
        cache.put(i, i * i)
    assert [cache.get(i) for i in range(7)] == [None] * 7
    assert [cache.get(i) for i in (7, 8, 9)] == [49, 64, 81]


def test_capacity_zero_raises():
    with pytest.raises(ValueError):
        LRUCache(0)


def test_negative_capacity_raises():
    with pytest.raises(ValueError):
        LRUCache(-3)
