"""Indonesian number formatting for the manuscript tables."""

from harness.numfmt import MISSING, english_number, format_height_range, format_id, format_percent


def test_thousands_use_a_dot():
    """Block heights read 25.463, as in the manuscript."""
    assert format_id(25463, 0) == "25.463"


def test_a_height_range_joins_two_heights_with_sampai():
    """A range of heights reads as the manuscript writes it: 1.200 sampai 140.040."""
    assert format_height_range(1200, 140_040) == "1.200 sampai 140.040"


def test_decimals_use_a_comma():
    """One decimal by default, with a comma."""
    assert format_id(13.2) == "13,2"


def test_both_separators_together():
    """Thousands dot and decimal comma in one number."""
    assert format_id(1234567.891, 3) == "1.234.567,891"


def test_negative_numbers_keep_their_sign():
    """A negative value keeps the minus sign in front."""
    assert format_id(-0.25, 2) == "-0,25"


def test_a_decimal_tie_rounds_half_up():
    """5.85 at one decimal is 5,9, as its three-decimal form 5,850 implies, although the float 5.85 lies just below the
    tie. Rounding works on the shortest decimal form of the value, so every tie rounds away from zero and a value
    off the tie keeps its nearest rounding."""
    assert format_id(5.85, 1) == "5,9"
    assert format_id(35.4755, 3) == "35,476"
    assert format_id(0.125, 2) == "0,13"
    assert format_id(2.5, 0) == "3"
    assert format_id(-0.25, 1) == "-0,3"
    assert format_id(1234.45, 1) == "1.234,5"
    assert format_id(5.8499, 1) == "5,8"


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


def test_english_number_swaps_the_separators():
    """The ABSTRACT writes numbers the English way: thousands comma, decimal point."""
    assert english_number("12,6") == "12.6"
    assert english_number("1.024") == "1,024"
    assert english_number("100,00") == "100.00"
    assert english_number("1.234,5") == "1,234.5"
    assert english_number("1.234.567,891") == "1,234,567.891"
    assert english_number("-0,25") == "-0.25"
    assert english_number("7") == "7"


def test_english_number_leaves_text_that_is_not_a_number():
    """Words, a session id, and a malformed group are returned unchanged."""
    for text in ("tidak tersedia", "S5", "", "1.02", "12,6 detik", "1,2,3"):
        assert english_number(text) == text, text
