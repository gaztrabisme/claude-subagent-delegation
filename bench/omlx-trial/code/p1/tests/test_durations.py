import pytest

from durations import parse_duration


def test_seconds_only():
    assert parse_duration("45s") == 45


def test_days():
    assert parse_duration("2d") == 172800


def test_hours_and_minutes_without_space():
    assert parse_duration("1h30m") == 5400


def test_hours_minutes_seconds_with_spaces():
    assert parse_duration("1h 5m 3s") == 3903


def test_surrounding_whitespace_is_ignored():
    assert parse_duration("  10m  ") == 600


def test_units_may_appear_in_any_order():
    assert parse_duration("30m1h") == 5400


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_duration("")


def test_whitespace_only_raises():
    with pytest.raises(ValueError):
        parse_duration("   ")


def test_unknown_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("5w")


def test_negative_raises():
    with pytest.raises(ValueError):
        parse_duration("-5m")


def test_repeated_unit_raises():
    with pytest.raises(ValueError):
        parse_duration("1h2h")


def test_decimal_raises():
    with pytest.raises(ValueError):
        parse_duration("1.5h")
