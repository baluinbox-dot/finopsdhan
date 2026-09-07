from __future__ import annotations

from datetime import date

from app.backtest.engine import _advance_expiry_if_stale
from app.backtest.expiry_calendar import next_expiry_on_or_after, weekly_expiries


def test_weekly_expiries_are_all_the_requested_weekday():
    expiries = weekly_expiries(date(2024, 9, 1), date(2024, 9, 30), weekday=3)  # Thursday
    assert expiries  # non-empty
    assert all(date.fromisoformat(e).weekday() == 3 for e in expiries)


def test_weekly_expiries_covers_the_full_range_plus_buffer():
    expiries = weekly_expiries(date(2024, 9, 1), date(2024, 9, 8), weekday=3)
    assert date.fromisoformat(expiries[0]) >= date(2024, 9, 1)
    assert date.fromisoformat(expiries[-1]) > date(2024, 9, 8)  # buffer past `end`


def test_next_expiry_on_or_after_picks_earliest_valid():
    expiries = ["2024-09-05", "2024-09-12", "2024-09-19"]
    assert next_expiry_on_or_after(expiries, date(2024, 9, 6)) == "2024-09-12"
    assert next_expiry_on_or_after(expiries, date(2024, 9, 5)) == "2024-09-05"


def test_next_expiry_on_or_after_none_when_all_passed():
    expiries = ["2024-09-05"]
    assert next_expiry_on_or_after(expiries, date(2024, 9, 6)) is None


def test_advance_expiry_leaves_still_valid_expiry_untouched():
    params = {"expiry": "2024-09-19"}
    _advance_expiry_if_stale(params, ["2024-09-19", "2024-09-26"], date(2024, 9, 10))
    assert params["expiry"] == "2024-09-19"  # unchanged -- still in the future


def test_advance_expiry_moves_to_next_once_stale():
    params = {"expiry": "2024-09-05"}
    _advance_expiry_if_stale(params, ["2024-09-05", "2024-09-12", "2024-09-19"], date(2024, 9, 6))
    assert params["expiry"] == "2024-09-12"


def test_advance_expiry_handles_missing_or_malformed_expiry():
    params = {"expiry": ""}
    _advance_expiry_if_stale(params, ["2024-09-05", "2024-09-12"], date(2024, 9, 1))
    assert params["expiry"] == "2024-09-05"

    params2 = {"expiry": "not-a-date"}
    _advance_expiry_if_stale(params2, ["2024-09-05"], date(2024, 9, 1))
    assert params2["expiry"] == "2024-09-05"
