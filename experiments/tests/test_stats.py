"""Sample statistics for identical iterations."""

import statistics

import pytest

from harness.stats import T_975, describe, sample_sd, summarize, t_critical_95


def test_hand_computed_summary():
    """Values 10, 12, 14, 16, 18.

    mean = 14; squared deviations 16 + 4 + 0 + 4 + 16 = 40; SD = sqrt(40 / 4) = 3.16227766;
    t(0.975, 4) = 2.776; half width = 2.776 * 3.16227766 / sqrt(5) = 3.92585685;
    interval = 10.07414315 to 17.92585685.
    """
    summary = summarize([10.0, 12.0, 14.0, 16.0, 18.0])
    assert summary.n == 5
    assert summary.mean == 14.0
    assert summary.sd == pytest.approx(3.16227766, abs=1e-8)
    assert (summary.minimum, summary.maximum) == (10.0, 18.0)
    assert summary.t_critical == 2.776
    assert summary.ci_low == pytest.approx(10.07414315, abs=1e-8)
    assert summary.ci_high == pytest.approx(17.92585685, abs=1e-8)


def test_sample_sd_uses_n_minus_one():
    """Agrees with the standard library's sample standard deviation."""
    values = [68.91, 70.2, 66.4, 71.05]
    assert sample_sd(values) == pytest.approx(statistics.stdev(values))


def test_single_value_has_no_spread():
    """n = 1 gives a mean but no SD and no interval."""
    summary = summarize([13.2])
    assert (summary.n, summary.mean, summary.sd, summary.ci_low, summary.ci_high) == (1, 13.2, None, None, None)


def test_table_values_from_nist():
    """Spot checks against NIST/SEMATECH section 1.3.6.7.2, column 0.975."""
    assert len(T_975) == 30
    assert (T_975[1], T_975[9], T_975[10], T_975[30]) == (12.706, 2.262, 2.228, 2.042)


def test_degrees_of_freedom_outside_the_table_have_no_critical_value():
    """df 31 is not in the table, so t_critical_95 refuses it."""
    with pytest.raises(ValueError, match="df=31"):
        t_critical_95(31)


def test_summarize_beyond_the_table_gives_no_interval_and_says_why():
    """n = 32 still has a mean and SD; the interval is None with the reason instead of an exception."""
    summary = summarize([float(value) for value in range(32)])
    assert (summary.n, summary.mean) == (32, 15.5)
    assert summary.sd == pytest.approx(statistics.stdev(range(32)))
    assert (summary.t_critical, summary.ci_low, summary.ci_high) == (None, None, None)
    assert "df=31" in summary.ci_reason


def test_summarize_with_a_table_interval_has_no_reason():
    """Within the table the reason stays empty."""
    assert summarize([10.0, 12.0, 14.0]).ci_reason is None


def test_describe_120_autocorrelated_samples_without_an_interval():
    """n, mean, range, and sample SD only: a t-interval would assume independent samples."""
    values = [0.1 * (index % 7) for index in range(120)]
    description = describe(values)
    assert (description.n, description.minimum, description.maximum) == (120, 0.0, pytest.approx(0.6))
    assert description.mean == pytest.approx(statistics.mean(values))
    assert description.sd == pytest.approx(statistics.stdev(values))
    assert not hasattr(description, "ci_low")


def test_describe_one_value_has_no_sd():
    """A single sample has a mean and no spread."""
    description = describe([4.0])
    assert (description.n, description.mean, description.sd) == (1, 4.0, None)


def test_empty_input_is_refused():
    """A summary of nothing is an error."""
    with pytest.raises(ValueError):
        summarize([])
