"""Indonesian number formatting for Markdown tables: thousands dot, decimal comma."""

import re
from decimal import ROUND_HALF_UP, Decimal

MISSING = "-"
# An Indonesian-formatted number: optional minus, whole part in dot-separated groups of three (or no dots),
# optional decimal comma with digits.
INDONESIAN_NUMBER = re.compile(r"-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?")


def format_id(value: float | int | None, decimals: int = 1) -> str:
    """Format value with a thousands dot and a decimal comma, a tie rounded half up; None becomes MISSING.

    The rounding works on the shortest decimal form of the value (its repr), so 5.85 at one decimal gives
    5,9, as its table form 5,850 implies, although the float 5.85 lies just below the tie.
    """
    if value is None:
        return MISSING
    step = Decimal(10) ** -decimals
    rounded = Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP)
    english = f"{rounded:,.{decimals}f}"
    return english.replace(",", "_").replace(".", ",").replace("_", ".")


def format_height_range(first: int, last: int) -> str:
    """Two block heights as the manuscript writes a range: 1.200 sampai 140.040."""
    return f"{format_id(first, 0)} sampai {format_id(last, 0)}"


def english_number(text: str) -> str:
    """An Indonesian-formatted number in English format ("1.234,5" to "1,234.5"); any other text is returned unchanged."""
    if INDONESIAN_NUMBER.fullmatch(text) is None:
        return text
    whole, _, decimals = text.partition(",")
    english_whole = whole.replace(".", ",")
    if not decimals:
        return english_whole
    return f"{english_whole}.{decimals}"


def format_percent(ratio: float | None, decimals: int = 1) -> str:
    """Format a ratio such as 0.991 as an Indonesian percentage such as 99,1%."""
    if ratio is None:
        return MISSING
    return format_id(ratio * 100, decimals) + "%"
