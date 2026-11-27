import calc


def test_add_small():
    assert calc.add(2, 3) == 5


def test_add_negative():
    assert calc.add(-4, 1) == -3


def test_mul_small():
    assert calc.mul(2, 3) == 6


def test_mul_zero():
    assert calc.mul(0, 9) == 0
