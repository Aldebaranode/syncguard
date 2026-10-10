"""Tests for the text width estimate used to size table columns."""

from redline.textwidth import breakable_pieces, estimate_text_width


def test_text_width_estimate_uses_times_new_roman_metrics():
    """'l' is 278/1000 em and 'm' is 778/1000 em; widths scale with the size in points and bold is wider."""
    assert estimate_text_width("l", 12) < estimate_text_width("m", 12)
    assert abs(estimate_text_width("mm", 24) - 2 * estimate_text_width("mm", 12)) <= 1
    assert estimate_text_width("mm", 12, bold=True) > estimate_text_width("mm", 12)


def test_known_value_for_ten_wide_letters():
    """Ten 'm' at 12 pt: 10 * 0.778 em * 12 pt * 20 twips, times the safety margin."""
    assert estimate_text_width("m" * 10, 12) == round(10 * 0.778 * 12 * 20 * 1.08)


def test_pieces_break_at_spaces_and_after_hyphens_but_not_at_dots_or_underscores():
    """Word wraps at spaces and after hyphens; a dotted or underscored identifier is one piece."""
    assert breakable_pieces("failover.state_sync_interval dan node-pasif") == ["failover.state_sync_interval", "dan", "node-", "pasif"]


def test_punctuation_has_its_own_width_instead_of_the_default():
    """Braces, colon, equals and plus are not the 500 default advance."""
    for character in "{}:=+":
        assert estimate_text_width(character * 100, 12) != estimate_text_width("0" * 100, 12), character


def _font_advances(path) -> tuple[dict[int, int], int]:
    """ASCII code point to advance width and units per em, read from a TrueType file without extra packages."""
    import struct

    data = path.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * index : 28 + 16 * index])
        tables[tag.decode()] = offset
    units = struct.unpack(">H", data[tables["head"] + 18 : tables["head"] + 20])[0]
    metric_count = struct.unpack(">H", data[tables["hhea"] + 34 : tables["hhea"] + 36])[0]
    cmap = tables["cmap"]
    subtables = struct.unpack(">H", data[cmap + 2 : cmap + 4])[0]
    mapping = {}
    for index in range(subtables):
        platform, encoding, offset = struct.unpack(">HHI", data[cmap + 4 + 8 * index : cmap + 12 + 8 * index])
        start = cmap + offset
        if (platform, encoding) == (3, 1) and struct.unpack(">H", data[start : start + 2])[0] == 4:
            segments = struct.unpack(">H", data[start + 6 : start + 8])[0] // 2
            ends = struct.unpack(f">{segments}H", data[start + 14 : start + 14 + 2 * segments])
            begins = struct.unpack(f">{segments}H", data[start + 16 + 2 * segments : start + 16 + 4 * segments])
            deltas = struct.unpack(f">{segments}h", data[start + 16 + 4 * segments : start + 16 + 6 * segments])
            range_base = start + 16 + 6 * segments
            ranges = struct.unpack(f">{segments}H", data[range_base : range_base + 2 * segments])
            for code in range(32, 127):
                for segment in range(segments):
                    if begins[segment] <= code <= ends[segment]:
                        if ranges[segment] == 0:
                            mapping[code] = (code + deltas[segment]) % 65536
                        else:
                            position = range_base + 2 * segment + ranges[segment] + 2 * (code - begins[segment])
                            glyph = struct.unpack(">H", data[position : position + 2])[0]
                            mapping[code] = (glyph + deltas[segment]) % 65536 if glyph else 0
                        break
    advances = {}
    for code, glyph in mapping.items():
        position = tables["hmtx"] + 4 * min(glyph, metric_count - 1)
        advances[code] = struct.unpack(">H", data[position : position + 2])[0]
    return advances, units


def test_estimate_covers_the_real_times_new_roman_advances_for_identifiers():
    """For sample identifiers the estimate is at least the real width from the font file and within 15 percent above it."""
    import pytest
    from pathlib import Path

    path = Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf")
    if not path.is_file():
        pytest.skip("Times New Roman is not installed")
    advances, units = _font_advances(path)
    for sample in ("failover.state_sync_interval", "cometbft.validator_address", "validator.stop_timeout", "{{DATA:a=b+c}}", "SignWithTimestamp/VerifyTimedSignature"):
        real = sum(advances[ord(character)] for character in sample) / units * 12 * 20
        assert real <= estimate_text_width(sample, 12) <= real * 1.15, (sample, real, estimate_text_width(sample, 12))
