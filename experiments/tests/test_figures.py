"""Figure data preparation (numeric) and 300 dpi PNG output."""

import math

import pytest

from fakes import smoke_records
from harness.constants import TARGET_VALIDATOR
from harness.figures import failover_series, plot_failover, plot_signer_counts, vote_status, with_gap_breaks
from harness.signing import signer_counts

FAULT_MS = 1791168828000


def png_pixels_per_metre(path) -> int:
    """Horizontal resolution stored in the PNG pHYs chunk."""
    data = path.read_bytes()
    position = data.index(b"pHYs")
    return int.from_bytes(data[position + 4:position + 8], "big")


def test_vote_status():
    """Flag 2 signed, flag 3 nil, otherwise absent."""
    records = {record.height: record for record in smoke_records()}
    assert [vote_status(records[height], TARGET_VALIDATOR) for height in (144, 143, 128)] == ["signed", "nil", "absent"]


def test_failover_series_around_the_fault():
    """All 28 recorded heights fall in [t_gagal - 60 s, t_gagal + 180 s]."""
    series = failover_series(smoke_records(), TARGET_VALIDATOR, FAULT_MS)
    assert (series.first_height, series.last_height, len(series.heights)) == (126, 169, 28)
    assert series.seconds[0] == pytest.approx(-6.124)
    assert series.seconds[series.heights.index(144)] == pytest.approx(65.459)
    assert [series.status.count(state) for state in ("signed", "nil", "absent")] == [8, 2, 18]


def test_missing_heights_break_the_line():
    """Heights 1, 2, 5: a NaN point between 2 and 5 keeps the line from bridging missing data."""
    xs, ys = with_gap_breaks([1, 2, 5], [10, 20, 50], [4, 4, 3])
    assert xs[:2] == [10, 20] and xs[3] == 50 and math.isnan(xs[2])
    assert ys[:2] == [4, 4] and ys[3] == 3 and math.isnan(ys[2])


def test_empty_window_is_an_error():
    """A figure without data is refused."""
    with pytest.raises(ValueError):
        failover_series(smoke_records(), TARGET_VALIDATOR, FAULT_MS + 10_000_000)


def test_failover_figure_is_a_300_dpi_png(tmp_path):
    """The file exists, is a PNG, and stores 300 dpi (11811 pixels per metre)."""
    series = failover_series(smoke_records(), TARGET_VALIDATOR, FAULT_MS)
    path = plot_failover(series, 68.91, "Sesi SFIX, iterasi 1", tmp_path / "gambar.png")
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 10_000
    assert png_pixels_per_metre(path) == 11811


def test_signer_count_figure_is_written(tmp_path):
    """Step plot of signatures per height."""
    path = plot_signer_counts(signer_counts(smoke_records()), 4, "Sesi SFIX", tmp_path / "penanda.png")
    assert path.read_bytes()[:4] == b"\x89PNG"
    assert png_pixels_per_metre(path) == 11811
