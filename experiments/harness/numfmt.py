"""Indonesian number formatting for Markdown tables: thousands dot, decimal comma."""

MISSING = "-"


def format_id(value: float | int | None, decimals: int = 1) -> str:
    """Format value with a thousands dot and a decimal comma; None becomes MISSING."""
    if value is None:
        return MISSING
    english = f"{value:,.{decimals}f}"
    return english.replace(",", "_").replace(".", ",").replace("_", ".")


def format_percent(ratio: float | None, decimals: int = 1) -> str:
    """Format a ratio such as 0.991 as an Indonesian percentage such as 99,1%."""
    if ratio is None:
        return MISSING
    return format_id(ratio * 100, decimals) + "%"
