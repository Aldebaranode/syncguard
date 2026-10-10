"""Estimate how wide text is in Times New Roman, the manuscript's body font, to size table columns.

Widths are the Times-Roman advance widths in thousandths of an em. The estimate carries a safety
margin, so a column sized from it holds the text on one line in Word. Bold text is wider than
regular text and gets its own factor.
"""

import re

TWIPS_PER_POINT = 20
SAFETY_FACTOR = 1.08
BOLD_FACTOR = 1.06
DEFAULT_ADVANCE = 500

LOWERCASE_ADVANCES = (
    444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500, 500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444,
)
UPPERCASE_ADVANCES = (
    722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722, 556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611,
)
ADVANCE_WIDTHS = {
    **dict(zip("abcdefghijklmnopqrstuvwxyz", LOWERCASE_ADVANCES)),
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", UPPERCASE_ADVANCES)),
    **{digit: 500 for digit in "0123456789"},
    " ": 250, ".": 250, ",": 250, ":": 278, ";": 278, "_": 500, "-": 333, "/": 278, "(": 333, ")": 333, "%": 833,
    "{": 480, "}": 480, "=": 564, "+": 564, "<": 564, ">": 564, "[": 333, "]": 333, "!": 333, "?": 444, '"': 408,
    "'": 180, "\\": 278, "|": 200, "~": 541, "^": 469, "`": 333, "@": 921, "&": 778, "#": 500, "$": 500, "*": 500,
}
BREAK_AFTER_HYPHEN = re.compile(r"(?<=-)(?=\w)")


def estimate_text_width(text: str, size_pt: float, bold: bool = False) -> int:
    """Width of text on one line in twips, with the safety margin."""
    em = sum(ADVANCE_WIDTHS.get(character, DEFAULT_ADVANCE) for character in text) / 1000
    factor = SAFETY_FACTOR * (BOLD_FACTOR if bold else 1)
    return round(em * size_pt * TWIPS_PER_POINT * factor)


def breakable_pieces(text: str) -> list[str]:
    """The pieces Word can put on separate lines: split at spaces and after a hyphen inside a word."""
    pieces = []
    for word in text.split():
        pieces.extend(piece for piece in BREAK_AFTER_HYPHEN.split(word) if piece)
    return pieces
