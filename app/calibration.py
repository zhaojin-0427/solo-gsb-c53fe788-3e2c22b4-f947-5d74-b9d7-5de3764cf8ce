"""Exact joint calibration of measured response coefficients (stdlib only).

The service reads each selected snapshot's *saved* setpoint curve and initial
temperatures, re-derives predictions with the original exact recurrence

    T(t+1) = T(t) + alpha * (U(t) - T(t))

for every candidate coefficient on the 0.01..1.00 grid (step 0.01), and then
jointly searches — per zone — the unordered coefficient pair shared across
all selected snapshots together with the per-snapshot assignment of the two
unlabeled measured series, minimising the sum of squared errors over every
reading.  All arithmetic and all comparisons use exact ``Fraction`` values;
nothing is ever rounded to the page display precision before comparing, and
fitting each series separately and stitching the results would give a
different (wrong) answer.  This module never reads the current form and never
writes to the snapshot or diagnosis tables.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Candidate coefficients: 0.01, 0.02, ..., 1.00 (step 0.01).
COEFFICIENT_GRID: List[Fraction] = [Fraction(k, 100) for k in range(1, 101)]
# Cap on the number of tied pairs reported for an unidentifiable zone.
TIED_PAIRS_LIMIT = 30

# A measured reading: (minute, measured minute-end temperature).
Reading = Tuple[int, Fraction]


def coefficient_label(index: int) -> str:
    """Display label for a 0-based grid index (index 0 -> "0.01")."""
    return f"{(index + 1) / 100:.2f}"


def _round_half_up(value: Fraction) -> int:
    """Round to the nearest integer, ties away from zero (ROUND_HALF_UP)."""
    if value < 0:
        return -_round_half_up(-value)
    quotient, remainder = divmod(value.numerator, value.denominator)
    if 2 * remainder >= value.denominator:
        quotient += 1
    return quotient


def exact_finite_decimal(value: Fraction) -> str:
    """Exact finite decimal string of a calibration fraction.

    Every calibration value (prediction, residual, squared error) has a
    denominator whose only prime factors are 2 and 5, so the decimal
    expansion terminates; render it exactly with integer arithmetic instead
    of a fixed-precision division.
    """
    numerator = value.numerator
    denominator = value.denominator
    twos = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    fives = 0
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:  # pragma: no cover - not reachable for calibration values
        raise ValueError(f"分母含 2/5 以外的因子，无法精确展开为有限小数：{value}")
    scale = max(twos, fives)
    numerator *= (2 ** (scale - twos)) * (5 ** (scale - fives))
    sign = "-" if numerator < 0 else ""
    digits = str(abs(numerator))
    if scale == 0:
        return sign + digits
    digits = digits.rjust(scale + 1, "0")
    integer_part, fractional_part = digits[:-scale], digits[-scale:]
    fractional_part = fractional_part.rstrip("0")
    if fractional_part:
        return f"{sign}{integer_part}.{fractional_part}"
    return f"{sign}{integer_part}"


def exact_display_decimal(value: Fraction, places: int = 4) -> str:
    """Exact ROUND_HALF_UP formatting of a fraction at the given places."""
    rounded = _round_half_up(value * (10 ** places))
    if places == 0:
        return str(rounded)
    sign = "-" if rounded < 0 else ""
    digits = str(abs(rounded)).rjust(places + 1, "0")
    return f"{sign}{digits[:-places]}.{digits[-places:]}"


def calibration_payload(value: Fraction) -> Dict[str, str]:
    return {
        "fraction": str(value),
        "decimal": exact_finite_decimal(value),
        "display": exact_display_decimal(value),
    }


def predict_temperatures(
    initial: Fraction, setpoints: Sequence[int], alpha: Fraction
) -> List[Fraction]:
    """Exact minute-end temperatures from the saved curve and initial temp."""
    temperatures = [initial]
    for setpoint in setpoints:
        previous = temperatures[-1]
        temperatures.append(previous + alpha * (Fraction(setpoint) - previous))
    return temperatures


def series_sse(temperatures: Sequence[Fraction], readings: Sequence[Reading]) -> Fraction:
    """Sum of squared errors of the readings against one predicted trajectory."""
    total = Fraction(0)
    for minute, measured in readings:
        residual = measured - temperatures[minute]
        total += residual * residual
    return total


def calibrate_joint(contexts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Jointly calibrate both zones over the selected snapshots.

    ``contexts`` holds one dict per selected snapshot with keys
    ``snapshot_id``, ``setpoints`` (the saved curve) and ``readings`` —
    ``readings[zone][series]`` being the sorted list of ``(minute, measured)``
    pairs of that zone's two unlabeled series.

    The search enumerates every unordered coefficient pair (swapping the two
    coefficients yields the same pair, so only ``first <= second`` is tried)
    and, for each snapshot, the assignment of its two series that gives the
    smaller exact error; the pair minimising the exact total over all
    readings wins.  Tied best pairs make the zone unidentifiable.
    """
    # Exact predictions and per-series SSE for every candidate coefficient.
    # sse[snapshot][zone][series][grid_index]
    sse: List[List[List[List[Fraction]]]] = []
    predictions: List[List[List[List[Fraction]]]] = []
    for ctx in contexts:
        per_zone_sse: List[List[List[Fraction]]] = []
        per_zone_pred: List[List[List[Fraction]]] = []
        for zone_index in range(2):
            per_series_sse: List[List[Fraction]] = [[], []]
            per_alpha_pred: List[List[Fraction]] = []
            for alpha in COEFFICIENT_GRID:
                temperatures = predict_temperatures(
                    ctx["initials"][zone_index], ctx["setpoints"], alpha
                )
                per_alpha_pred.append(temperatures)
                for series_index in range(2):
                    per_series_sse[series_index].append(
                        series_sse(temperatures, ctx["readings"][zone_index][series_index])
                    )
            per_zone_sse.append(per_series_sse)
            per_zone_pred.append(per_alpha_pred)
        sse.append(per_zone_sse)
        predictions.append(per_zone_pred)

    grid_size = len(COEFFICIENT_GRID)
    zones_payload: List[Dict[str, Any]] = []
    for zone_index in range(2):
        best_total: Optional[Fraction] = None
        best_pairs: List[Tuple[int, int]] = []
        for first in range(grid_size):
            for second in range(first, grid_size):
                total = Fraction(0)
                for snap_index in range(len(contexts)):
                    direct = (
                        sse[snap_index][zone_index][0][first]
                        + sse[snap_index][zone_index][1][second]
                    )
                    swapped = (
                        sse[snap_index][zone_index][0][second]
                        + sse[snap_index][zone_index][1][first]
                    )
                    total += direct if direct <= swapped else swapped
                if best_total is None or total < best_total:
                    best_total = total
                    best_pairs = [(first, second)]
                elif total == best_total:
                    best_pairs.append((first, second))

        assert best_total is not None  # the grid is never empty
        identifiable = len(best_pairs) == 1
        zone_payload: Dict[str, Any] = {
            "zone_index": zone_index + 1,
            "identifiable": identifiable,
            "min_sse": calibration_payload(best_total),
            "tied_pairs_count": len(best_pairs),
        }

        if identifiable:
            first, second = best_pairs[0]
            assignments: List[Dict[str, Any]] = []
            attribution_tied = False
            for snap_index, ctx in enumerate(contexts):
                direct = (
                    sse[snap_index][zone_index][0][first]
                    + sse[snap_index][zone_index][1][second]
                )
                swapped = (
                    sse[snap_index][zone_index][0][second]
                    + sse[snap_index][zone_index][1][first]
                )
                if direct <= swapped:
                    mapping = (first, second)
                    tied = direct == swapped
                else:
                    mapping = (second, first)
                    tied = False
                attribution_tied = attribution_tied or tied
                series_payloads: List[Dict[str, Any]] = []
                for series_index in range(2):
                    grid_index = mapping[series_index]
                    temperatures = predictions[snap_index][zone_index][grid_index]
                    residuals = [
                        {
                            "minute": minute,
                            "observed": calibration_payload(measured),
                            "predicted": calibration_payload(temperatures[minute]),
                            "residual": calibration_payload(
                                measured - temperatures[minute]
                            ),
                        }
                        for minute, measured in ctx["readings"][zone_index][series_index]
                    ]
                    series_payloads.append(
                        {
                            "series_index": series_index + 1,
                            "coefficient": coefficient_label(grid_index),
                            "sse": calibration_payload(
                                sse[snap_index][zone_index][series_index][grid_index]
                            ),
                            "residuals": residuals,
                        }
                    )
                assignments.append(
                    {
                        "snapshot_id": ctx["snapshot_id"],
                        "tied": tied,
                        "series": series_payloads,
                    }
                )
            zone_payload["best"] = {
                "coefficients": [coefficient_label(first), coefficient_label(second)],
                "attribution_tied": attribution_tied,
                "assignments": assignments,
            }
            zone_payload["tied_pairs"] = []
        else:
            zone_payload["best"] = None
            zone_payload["tied_pairs"] = [
                {"coefficients": [coefficient_label(first), coefficient_label(second)]}
                for first, second in best_pairs[:TIED_PAIRS_LIMIT]
            ]
        zones_payload.append(zone_payload)

    return {
        "applicable": all(zone["identifiable"] for zone in zones_payload),
        "zones": zones_payload,
    }
