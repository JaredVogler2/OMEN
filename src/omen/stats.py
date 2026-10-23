"""Small, dependency-free statistics: Student-t CDF (fat tails), bootstrap helpers."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1 - x) / b


def student_t_cdf(t: float, nu: float) -> float:
    if nu > 1e6:
        return normal_cdf(t)
    x = nu / (nu + t * t)
    tail = 0.5 * betainc(nu / 2, 0.5, x)
    return 1 - tail if t > 0 else tail


def scaled_t_cdf(x: float, scale_sd: float, nu: float) -> float:
    """CDF of a Student-t with ``nu`` dof rescaled to have standard deviation ``scale_sd``."""
    if scale_sd <= 0:
        return 1.0 if x >= 0 else 0.0
    s = scale_sd * math.sqrt((nu - 2) / nu) if nu > 2 else scale_sd
    return student_t_cdf(x / s, nu)


def cluster_bootstrap_lower(
    values_by_cluster: dict[str, list[tuple[float, float]]], confidence: float, samples: int, seed: int = 7
) -> float:
    """Lower confidence bound of ROI = sum(pnl) / sum(cost), resampling whole clusters (events), which keeps
    correlated strikes of one event together."""
    clusters = [v for v in values_by_cluster.values() if v]
    if not clusters:
        return float("-inf")
    rng = random.Random(seed)
    rois = []
    for _ in range(samples):
        pnl = cost = 0.0
        for _ in range(len(clusters)):
            for p, c in rng.choice(clusters):
                pnl += p
                cost += c
        rois.append(pnl / cost if cost else 0.0)
    rois.sort()
    return rois[max(0, int((1 - confidence) * samples) - 1)]


def expected_calibration_error(pairs: Sequence[tuple[float, float]], bins: int = 10) -> float:
    """Weighted mean |mean prediction - observed frequency| over equal-width probability bins."""
    if not pairs:
        return float("nan")
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, y in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    n = len(pairs)
    return sum(len(b) / n * abs(sum(p for p, _ in b) / len(b) - sum(y for _, y in b) / len(b)) for b in buckets if b)
