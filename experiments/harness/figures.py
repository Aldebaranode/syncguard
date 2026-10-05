"""Thesis figures as 300 dpi PNG (R10): one failover around t_gagal, and the signer count per height."""

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (the backend is chosen before pyplot loads)

DPI = 300
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e4e3df"
SERIES = "#2a78d6"
ACCENT = "#eb6834"
VOTE_STYLES = (
    ("signed", "ditandatangani", SERIES, "o"),
    ("nil", "suara nil", ACCENT, "s"),
    ("absent", "tidak hadir", INK_MUTED, "x"),
)


def vote_status(record, validator: str) -> str:
    """signed (block_id_flag 2), nil (flag 3), or absent for the validator at one height."""
    if validator in record.signers:
        return "signed"
    if validator in record.nil_voters:
        return "nil"
    return "absent"


@dataclass(frozen=True)
class FailoverSeries:
    """Heights around one failover: header time relative to t_gagal, height, and the validator's vote."""

    seconds: list
    heights: list
    status: list
    first_height: int
    last_height: int


def failover_series(records: list, validator: str, t_fault_ms: int, before_s: float = 60.0,
                    after_s: float = 180.0) -> FailoverSeries:
    """Heights whose host-clock header time lies in [t_fault - before_s, t_fault + after_s]."""
    start = t_fault_ms - before_s * 1000
    end = t_fault_ms + after_s * 1000
    window = [record for record in sorted(records, key=lambda record: record.height)
              if start <= record.header_time_ms <= end]
    if not window:
        raise ValueError("no heights inside the figure window")
    return FailoverSeries(
        seconds=[(record.header_time_ms - t_fault_ms) / 1000 for record in window],
        heights=[record.height for record in window],
        status=[vote_status(record, validator) for record in window],
        first_height=window[0].height,
        last_height=window[-1].height,
    )


def with_gap_breaks(heights: list, xs: list, ys: list) -> tuple:
    """Insert a NaN point wherever consecutive heights are not adjacent, so missing heights show as a break."""
    out_x = []
    out_y = []
    for index, height in enumerate(heights):
        if index > 0 and height != heights[index - 1] + 1:
            out_x.append(float("nan"))
            out_y.append(float("nan"))
        out_x.append(xs[index])
        out_y.append(ys[index])
    return out_x, out_y


def _style(axis) -> None:
    """Light grid, no top or right spine, muted ticks."""
    axis.grid(color=GRID, linewidth=0.6)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_color(INK_MUTED)
    axis.spines["bottom"].set_color(INK_MUTED)
    axis.tick_params(colors=INK_MUTED, labelsize=8)


def _save(figure, path: Path) -> Path:
    """Write the figure as a 300 dpi PNG on white and release it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    return path


def plot_failover(series: FailoverSeries, t_recovery_s: float | None, title: str, path: Path) -> Path:
    """Block height (top panel) and the validator's vote per height (bottom panel) on one time axis."""
    figure, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(7.0, 4.2), height_ratios=[3, 1])
    line_x, line_y = with_gap_breaks(series.heights, series.seconds, series.heights)
    top.plot(line_x, line_y, color=SERIES, linewidth=1.5, marker="o", markersize=2.5)
    top.set_ylabel("Tinggi blok", color=INK, fontsize=9)
    for state, label, colour, shape in VOTE_STYLES:
        times = [second for second, status in zip(series.seconds, series.status) if status == state]
        bottom.scatter(times, [0] * len(times), color=colour, marker=shape, s=16, label=label)
    bottom.set_yticks([])
    bottom.set_xlabel("Waktu relatif terhadap t_gagal (s)", color=INK, fontsize=9)
    for axis in (top, bottom):
        _style(axis)
        axis.axvline(0, color=INK, linewidth=1.0, linestyle="--")
        if t_recovery_s is not None:
            axis.axvline(t_recovery_s, color=INK_MUTED, linewidth=1.0, linestyle=":")
    top.annotate("t_gagal", xy=(0, 1), xycoords=("data", "axes fraction"), xytext=(3, -10),
                 textcoords="offset points", fontsize=8, color=INK)
    if t_recovery_s is not None:
        top.annotate("t_aktif_cadangan", xy=(t_recovery_s, 1), xycoords=("data", "axes fraction"), xytext=(3, -10),
                     textcoords="offset points", fontsize=8, color=INK_MUTED)
    bottom.legend(loc="upper center", bbox_to_anchor=(0.5, -0.9), ncol=3, frameon=False, fontsize=8)
    top.set_title(title, fontsize=9, color=INK)
    return _save(figure, path)


def plot_signer_counts(counts: list, validator_total: int, title: str, path: Path) -> Path:
    """block_id_flag 2 signatures per height as a step line, with the validator-set size as a labelled reference."""
    heights = [height for height, _ in counts]
    values = [count for _, count in counts]
    figure, axis = plt.subplots(figsize=(7.0, 2.8))
    line_x, line_y = with_gap_breaks(heights, heights, values)
    axis.step(line_x, line_y, where="mid", color=SERIES, linewidth=1.5)
    axis.axhline(validator_total, color=INK_MUTED, linewidth=1.0, linestyle="--")
    axis.annotate(f"{validator_total} validator", xy=(0, validator_total), xycoords=("axes fraction", "data"),
                  xytext=(3, 3), textcoords="offset points", fontsize=8, color=INK_MUTED)
    axis.set_ylim(0, validator_total + 0.6)
    axis.set_xlabel("Tinggi blok", color=INK, fontsize=9)
    axis.set_ylabel("Jumlah penanda tangan", color=INK, fontsize=9)
    _style(axis)
    axis.set_title(title, fontsize=9, color=INK)
    return _save(figure, path)
