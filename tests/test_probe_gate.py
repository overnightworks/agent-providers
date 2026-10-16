import pytest


def _first(value):
    raise ValueError(value)


def _second(value):
    raise ValueError(value)


def test_probe_two_raising_calls():
    unused_probe = 1
    with pytest.raises(ValueError):
        _first(_second("x"))
    assert True
