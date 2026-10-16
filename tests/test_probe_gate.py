import pytest


def test_probe_two_raising_calls():
    with pytest.raises(ValueError):
        int("x")
        float("y")
