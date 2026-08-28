"""format_dhan_error() turns the SDK's `remarks` field into a message shown
in logs/flashes. These tests pin down the three shapes it now has to tell
apart in the ambiguous "no errorCode/errorType/errorMessage" case, since a
real "Invalid Expiry Date" 400 was previously misreported as "you're
probably being rate-limited" — see app/dhan/diagnostics.py, which is what
populates the `status_code`/`raw_message` keys this reads."""

from __future__ import annotations

from app.dhan.helpers import format_dhan_error


def test_true_429_with_no_recoverable_body_still_reads_as_rate_limited():
    remarks = {"error_code": None, "error_type": None, "error_message": None, "status_code": 429, "raw_message": None}
    assert "rate-limited" in format_dhan_error(remarks)


def test_recovered_raw_message_is_used_verbatim_instead_of_guessing():
    remarks = {
        "error_code": None, "error_type": None, "error_message": None,
        "status_code": 400, "raw_message": "Invalid Expiry Date",
    }
    result = format_dhan_error(remarks)
    assert "Invalid Expiry Date" in result
    assert "HTTP 400" in result
    assert "rate-limited" not in result


def test_401_with_recovered_message_is_not_reported_as_rate_limited():
    remarks = {
        "error_code": None, "error_type": None, "error_message": None,
        "status_code": 401, "raw_message": "Authentication Failed - Client ID or Token invalid",
    }
    result = format_dhan_error(remarks)
    assert "Authentication Failed" in result
    assert "rate-limited" not in result


def test_non_429_status_with_no_recovered_message_names_the_status_instead_of_guessing():
    remarks = {"error_code": None, "error_type": None, "error_message": None, "status_code": 500, "raw_message": None}
    result = format_dhan_error(remarks)
    assert "500" in result
    assert "rate-limited" not in result


def test_ambiguous_dict_with_no_status_code_at_all_falls_back_to_rate_limit_guess():
    """Older/plain remarks (no diagnostics enrichment applied, e.g. if that
    patch ever fails to install) must still degrade to the previous
    behavior rather than crashing on a missing key."""
    remarks = {"error_code": None, "error_type": None, "error_message": None}
    assert "rate-limited" in format_dhan_error(remarks)


def test_real_error_code_type_message_still_formatted_as_before():
    remarks = {"error_code": "DH-901", "error_type": "Invalid_Authentication", "error_message": "Client ID invalid"}
    result = format_dhan_error(remarks)
    assert "Invalid_Authentication" in result
    assert "Client ID invalid" in result
    assert "DH-901" in result
