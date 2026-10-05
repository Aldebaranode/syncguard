"""Indonesian number formatting for the manuscript tables."""

from harness.numfmt import MISSING, format_id, format_percent


def test_thousands_use_a_dot():
    """Block heights read 25.463, as in the manuscript."""
    assert format_id(25463, 0) == "25.463"


def test_decimals_use_a_comma():
    """One decimal by default, with a comma."""
    assert format_id(13.2) == "13,2"


def test_both_separators_together():
    """Thousands dot and decimal comma in one number."""
    assert format_id(1234567.891, 3) == "1.234.567,891"


def test_negative_numbers_keep_their_sign():
    """A negative value keeps the minus sign in front."""
    assert format_id(-0.25, 2) == "-0,25"


def test_trailing_zeros_are_kept():
    """Fixed decimals keep trailing zeros, so columns line up."""
    assert format_id(68.91, 3) == "68,910"


def test_missing_value_renders_a_hyphen():
    """None becomes the table placeholder."""
    assert format_id(None) == MISSING == "-"


def test_percent_from_ratio():
    """A ratio renders as an Indonesian percentage."""
    assert format_percent(0.991) == "99,1%"
    assert format_percent(1.0, 0) == "100%"
    assert format_percent(None) == "-"
