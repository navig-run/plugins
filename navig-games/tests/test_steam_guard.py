"""Steam Guard TOTP — verified against the canonical steampy/ValvePython algorithm."""

import base64

from navig_games.engine.steam import guard


def test_canonical_vector():
    # Cross-verified against the steampy generate_one_time_code algorithm:
    # secret=b"superdupersecret", t=3000030 -> "YRGQJ".
    secret = base64.b64encode(b"superdupersecret").decode()
    assert guard.generate_code(secret, 3000030) == "YRGQJ"


def test_second_golden_vector():
    secret = base64.b64encode(bytes(range(20))).decode()
    assert guard.generate_code(secret, 1000000000) == "YXT7G"


def test_format_and_alphabet():
    code = guard.generate_code(base64.b64encode(bytes(range(20))).decode(), 1700000000)
    assert len(code) == 5
    assert all(c in "23456789BCDFGHJKMNPQRTVWXY" for c in code)


def test_determinism():
    s = base64.b64encode(bytes(range(20))).decode()
    assert guard.generate_code(s, 1e9) == guard.generate_code(s, 1e9)


def test_valid_secret():
    assert guard.is_valid_secret(base64.b64encode(bytes(20)).decode())
    assert not guard.is_valid_secret("not-valid-base64!!!")
    assert not guard.is_valid_secret(base64.b64encode(bytes(10)).decode())  # wrong length


def test_seconds_remaining():
    assert guard.seconds_remaining(0) == 30
    assert guard.seconds_remaining(15) == 15
    assert guard.seconds_remaining(29) == 1
