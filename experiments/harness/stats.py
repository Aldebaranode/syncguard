"""Summary statistics for n identical iterations (R5; findings III-6, IV-3)."""

import math
from dataclasses import dataclass

# Upper critical values t(0.975, df) of Student's t distribution for df 1..30.
# Source: NIST/SEMATECH e-Handbook of Statistical Methods, section 1.3.6.7.2,
# "Critical Values of the Student's t Distribution", column 0.975,
# https://www.itl.nist.gov/div898/handbook/eda/section3/eda3672.htm
T_975 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


@dataclass(frozen=True)
class Summary:
    """n, mean, sample SD, range, and the two-sided 95% t-interval of one metric."""

    n: int
    mean: float
    sd: float | None
    minimum: float
    maximum: float
    t_critical: float | None
    ci_low: float | None
    ci_high: float | None


def mean(values: list) -> float:
    """Arithmetic mean."""
    return sum(values) / len(values)


def sample_sd(values: list) -> float:
    """Standard deviation with the n - 1 denominator; needs at least two values."""
    if len(values) < 2:
        raise ValueError("sample SD needs at least two values")
    average = mean(values)
    squared = sum((value - average) ** 2 for value in values)
    return math.sqrt(squared / (len(values) - 1))


def t_critical_95(df: int) -> float:
    """Two-sided 95% critical value from the NIST table."""
    if df not in T_975:
        raise ValueError(f"no t value for df={df}; the table covers df 1..30")
    return T_975[df]


def summarize(values: list) -> Summary:
    """Summary of one metric over identical iterations; SD and interval need n >= 2."""
    if not values:
        raise ValueError("cannot summarize an empty list")
    count = len(values)
    average = mean(values)
    if count < 2:
        return Summary(count, average, None, min(values), max(values), None, None, None)
    sd = sample_sd(values)
    t_value = t_critical_95(count - 1)
    half_width = t_value * sd / math.sqrt(count)
    return Summary(count, average, sd, min(values), max(values), t_value, average - half_width, average + half_width)
