"""Tests for the exact joint calibration core (app/calibration.py)."""

import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.calibration import (
    COEFFICIENT_GRID,
    calibrate_joint,
    calibration_payload,
    coefficient_label,
    exact_display_decimal,
    exact_finite_decimal,
    predict_temperatures,
    series_sse,
)


def make_context(snapshot_id, setpoints, initials, readings):
    """readings: readings[zone][series] = list of (minute, Fraction)."""
    return {
        "snapshot_id": snapshot_id,
        "setpoints": setpoints,
        "initials": [Fraction(v) for v in initials],
        "readings": readings,
    }


def gen_readings(setpoints, initial, alpha_label, minutes=None):
    """Exact predicted readings for a coefficient label like "0.25"."""
    alpha = Fraction(alpha_label)
    temperatures = predict_temperatures(Fraction(initial), setpoints, alpha)
    minutes = minutes or range(1, len(setpoints) + 1)
    return [(minute, temperatures[minute]) for minute in minutes]


def pair_of(zone_payload):
    return tuple(zone_payload["best"]["coefficients"])


class ExactDecimalTests(unittest.TestCase):
    def test_exact_finite_decimal(self):
        self.assertEqual(exact_finite_decimal(Fraction(0)), "0")
        self.assertEqual(exact_finite_decimal(Fraction(1, 4)), "0.25")
        self.assertEqual(exact_finite_decimal(Fraction(-5, 2)), "-2.5")
        self.assertEqual(exact_finite_decimal(Fraction(300)), "300")
        self.assertEqual(exact_finite_decimal(Fraction(1, 10 ** 30)), "0." + "0" * 29 + "1")
        self.assertEqual(exact_finite_decimal(Fraction(-1, 10 ** 30)), "-0." + "0" * 29 + "1")
        self.assertEqual(exact_finite_decimal(Fraction(123456789, 10 ** 6)), "123.456789")
        # Denominator 100**24 (deepest prediction denominator) stays exact.
        value = Fraction(12345, 100 ** 24)
        self.assertEqual(exact_finite_decimal(value), "0." + "0" * 43 + "12345")

    def test_exact_display_decimal(self):
        self.assertEqual(exact_display_decimal(Fraction(1, 4)), "0.2500")
        self.assertEqual(exact_display_decimal(Fraction(-5, 2)), "-2.5000")
        self.assertEqual(exact_display_decimal(Fraction(1, 10 ** 30)), "0.0000")
        self.assertEqual(exact_display_decimal(Fraction(25, 10), places=0), "3")
        self.assertEqual(exact_display_decimal(Fraction(-25, 10), places=0), "-3")
        self.assertEqual(exact_display_decimal(Fraction(24, 10), places=0), "2")
        self.assertEqual(exact_display_decimal(Fraction(-24, 10), places=0), "-2")

    def test_calibration_payload(self):
        payload = calibration_payload(Fraction(1, 8))
        self.assertEqual(payload["fraction"], "1/8")
        self.assertEqual(payload["decimal"], "0.125")
        self.assertEqual(payload["display"], "0.1250")

    def test_coefficient_label(self):
        self.assertEqual(coefficient_label(0), "0.01")
        self.assertEqual(coefficient_label(24), "0.25")
        self.assertEqual(coefficient_label(99), "1.00")


class JointCalibrationTests(unittest.TestCase):
    def test_exact_recovery_across_snapshots(self):
        # Two snapshots with different curves/initials; readings generated
        # exactly from known coefficient pairs must be recovered with SSE 0.
        ctx1 = make_context(
            1,
            [100, 200],
            [0, 50],
            [
                [gen_readings([100, 200], 0, "0.25"), gen_readings([100, 200], 0, "0.60")],
                [gen_readings([100, 200], 50, "0.10"), gen_readings([100, 200], 50, "0.90")],
            ],
        )
        ctx2 = make_context(
            2,
            [150, 50, 100],
            [10, 200],
            [
                # Skip some minutes: only minutes 1 and 3 are measured.
                [gen_readings([150, 50, 100], 10, "0.25", [1, 3]),
                 gen_readings([150, 50, 100], 10, "0.60", [1, 3])],
                [gen_readings([150, 50, 100], 200, "0.10", [1, 3]),
                 gen_readings([150, 50, 100], 200, "0.90", [1, 3])],
            ],
        )
        result = calibrate_joint([ctx1, ctx2])
        self.assertTrue(result["applicable"])
        zone1, zone2 = result["zones"]
        self.assertTrue(zone1["identifiable"])
        self.assertTrue(zone2["identifiable"])
        self.assertEqual(pair_of(zone1), ("0.25", "0.60"))
        self.assertEqual(pair_of(zone2), ("0.10", "0.90"))
        self.assertEqual(zone1["min_sse"]["fraction"], "0")
        self.assertEqual(zone2["min_sse"]["fraction"], "0")
        # Attribution: snapshot 1 zone 1 series 1 -> 0.25, series 2 -> 0.60.
        assignment = zone1["best"]["assignments"][0]
        self.assertEqual(assignment["snapshot_id"], 1)
        self.assertFalse(assignment["tied"])
        self.assertEqual(assignment["series"][0]["coefficient"], "0.25")
        self.assertEqual(assignment["series"][1]["coefficient"], "0.60")
        self.assertFalse(zone1["best"]["attribution_tied"])
        # Residuals are all exactly zero and cover exactly the given minutes.
        residuals = assignment["series"][0]["residuals"]
        self.assertEqual([r["minute"] for r in residuals], [1, 2])
        self.assertTrue(all(r["residual"]["fraction"] == "0" for r in residuals))

    def test_swapped_series_order_is_equivalent(self):
        # Feeding the two series in the opposite order must give the same pair.
        setpoints = [120, 40, 90]
        base = [
            [gen_readings(setpoints, 30, "0.30"), gen_readings(setpoints, 30, "0.55")],
            [gen_readings(setpoints, 80, "0.12"), gen_readings(setpoints, 80, "0.48")],
        ]
        ctx_a = make_context(1, setpoints, [30, 80], base)
        ctx_b = make_context(2, [70, 70], [10, 10],
                             [[gen_readings([70, 70], 10, "0.30"),
                               gen_readings([70, 70], 10, "0.55")],
                              [gen_readings([70, 70], 10, "0.12"),
                               gen_readings([70, 70], 10, "0.48")]])
        swapped = [
            [base[0][1], base[0][0]],
            [base[1][1], base[1][0]],
        ]
        ctx_a_swapped = make_context(1, setpoints, [30, 80], swapped)
        ctx_b_swapped = make_context(2, [70, 70], [10, 10],
                                     [[gen_readings([70, 70], 10, "0.55"),
                                       gen_readings([70, 70], 10, "0.30")],
                                      [gen_readings([70, 70], 10, "0.48"),
                                       gen_readings([70, 70], 10, "0.12")]])
        result_a = calibrate_joint([ctx_a, ctx_b])
        result_b = calibrate_joint([ctx_a_swapped, ctx_b_swapped])
        self.assertEqual(pair_of(result_a["zones"][0]), ("0.30", "0.55"))
        self.assertEqual(pair_of(result_b["zones"][0]), ("0.30", "0.55"))
        self.assertEqual(pair_of(result_a["zones"][1]), ("0.12", "0.48"))
        self.assertEqual(pair_of(result_b["zones"][1]), ("0.12", "0.48"))
        # The swapped input assigns series 1 to the larger coefficient now.
        self.assertEqual(
            result_b["zones"][0]["best"]["assignments"][0]["series"][0]["coefficient"],
            "0.55",
        )

    def test_unidentifiable_when_predictions_do_not_depend_on_alpha(self):
        # U(t) == initial temperature everywhere -> every trajectory is
        # constant, so all 5050 unordered pairs tie and the zone is
        # unidentifiable; the whole application is disabled.
        ctx1 = make_context(1, [20, 20], [20, 20], [[[(1, Fraction(20))], [(2, Fraction(20))]],
                                                    [[(1, Fraction(20))], [(1, Fraction(20))]]])
        ctx2 = make_context(2, [20, 20, 20], [20, 20], [[[(3, Fraction(20))], [(1, Fraction(20))]],
                                                        [[(2, Fraction(20))], [(3, Fraction(20))]]])
        result = calibrate_joint([ctx1, ctx2])
        self.assertFalse(result["applicable"])
        for zone in result["zones"]:
            self.assertFalse(zone["identifiable"])
            self.assertIsNone(zone["best"])
            self.assertEqual(zone["tied_pairs_count"], 5050)  # C(100,2)+100, swap-invariant
            self.assertLessEqual(len(zone["tied_pairs"]), 30)

    def test_unique_pair_with_equal_coefficients(self):
        # Both series fit 0.25 exactly -> unique best pair (0.25, 0.25);
        # attribution is trivially tied and must be marked.
        ctx1 = make_context(1, [100], [0, 0], [[[(1, Fraction(25))], [(1, Fraction(25))]],
                                               [[(1, Fraction(10))], [(1, Fraction(10))]]])
        ctx2 = make_context(2, [100, 100], [0, 0], [[[(1, Fraction(25))], [(1, Fraction(25))]],
                                                    [[(1, Fraction(10))], [(1, Fraction(10))]]])
        result = calibrate_joint([ctx1, ctx2])
        self.assertTrue(result["applicable"])
        zone1 = result["zones"][0]
        self.assertEqual(pair_of(zone1), ("0.25", "0.25"))
        self.assertTrue(zone1["best"]["attribution_tied"])
        self.assertTrue(all(a["tied"] for a in zone1["best"]["assignments"]))

    def test_attribution_tie_with_distinct_coefficients(self):
        # Snapshot 1 is uninformative (U == initial temperature everywhere):
        # every pair costs 0 there and its series attribution is always tied.
        # Snapshots 2/3 pin the unique best pairs with exact readings.
        ctx1 = make_context(1, [20, 20], [20, 20], [[[(1, Fraction(20))], [(1, Fraction(20))]],
                                                    [[(2, Fraction(20))], [(2, Fraction(20))]]])
        ctx2 = make_context(
            2,
            [100, 200],
            [0, 50],
            [
                [gen_readings([100, 200], 0, "0.25"), gen_readings([100, 200], 0, "0.60")],
                [gen_readings([100, 200], 50, "0.10"), gen_readings([100, 200], 50, "0.90")],
            ],
        )
        ctx3 = make_context(
            3,
            [150, 50, 100],
            [10, 200],
            [
                [gen_readings([150, 50, 100], 10, "0.25"), gen_readings([150, 50, 100], 10, "0.60")],
                [gen_readings([150, 50, 100], 200, "0.10"), gen_readings([150, 50, 100], 200, "0.90")],
            ],
        )
        result = calibrate_joint([ctx1, ctx2, ctx3])
        self.assertTrue(result["applicable"])
        zone1 = result["zones"][0]
        self.assertEqual(pair_of(zone1), ("0.25", "0.60"))
        assignments = {a["snapshot_id"]: a for a in zone1["best"]["assignments"]}
        self.assertTrue(assignments[1]["tied"])
        self.assertFalse(assignments[2]["tied"])
        self.assertFalse(assignments[3]["tied"])
        self.assertTrue(zone1["best"]["attribution_tied"])

    def test_joint_search_not_per_series_stitching(self):
        # The shared pair must minimise the *joint* error: snapshot 1 fits
        # (0.25, 0.60) exactly, snapshot 2 fits (0.25, 0.64) exactly; the
        # joint optimum is the shared compromise (0.25, 0.62), which neither
        # per-snapshot fit returns.
        ctx1 = make_context(1, [100], [0, 0], [[[(1, Fraction(25))], [(1, Fraction(60))]],
                                               [[(1, Fraction(25))], [(1, Fraction(60))]]])
        ctx2 = make_context(2, [100], [0, 0], [[[(1, Fraction(25))], [(1, Fraction(64))]],
                                               [[(1, Fraction(25))], [(1, Fraction(64))]]])
        result = calibrate_joint([ctx1, ctx2])
        zone1 = result["zones"][0]
        self.assertTrue(zone1["identifiable"])
        self.assertEqual(pair_of(zone1), ("0.25", "0.62"))
        # Total SSE: per snapshot only the 60/64-reading misses the shared
        # 0.62 compromise: (60-62)^2 + (64-62)^2 = 8 exactly.
        self.assertEqual(Fraction(zone1["min_sse"]["fraction"]), Fraction(8))

    def test_bruteforce_crosscheck_random(self):
        # Independent naive re-implementation must agree on random instances.
        rng = random.Random(20261005)
        for trial in range(6):
            n_snapshots = rng.randint(2, 4)
            contexts = []
            for snap_id in range(1, n_snapshots + 1):
                duration = rng.randint(1, 4)
                setpoints = [rng.randint(0, 300) for _ in range(duration)]
                initials = [rng.randint(0, 300) for _ in range(2)]
                readings = []
                for zone in range(2):
                    zone_series = []
                    for _ in range(2):
                        n_readings = rng.randint(1, duration)
                        minutes = sorted(rng.sample(range(1, duration + 1), n_readings))
                        zone_series.append(
                            [(m, Fraction(rng.randint(0, 30000), 100)) for m in minutes]
                        )
                    readings.append(zone_series)
                contexts.append(make_context(snap_id, setpoints, initials, readings))

            result = calibrate_joint(contexts)
            for zone_index, zone in enumerate(result["zones"]):
                # Naive joint search.
                sse = {}
                for s_idx, ctx in enumerate(contexts):
                    for series_idx in range(2):
                        for k, alpha in enumerate(COEFFICIENT_GRID):
                            temps = predict_temperatures(
                                ctx["initials"][zone_index], ctx["setpoints"], alpha
                            )
                            sse[(s_idx, series_idx, k)] = series_sse(
                                temps, ctx["readings"][zone_index][series_idx]
                            )
                best = None
                best_pairs = []
                for i in range(100):
                    for j in range(i, 100):
                        total = Fraction(0)
                        for s_idx in range(len(contexts)):
                            a = sse[(s_idx, 0, i)] + sse[(s_idx, 1, j)]
                            b = sse[(s_idx, 0, j)] + sse[(s_idx, 1, i)]
                            total += min(a, b)
                        if best is None or total < best:
                            best = total
                            best_pairs = [(i, j)]
                        elif total == best:
                            best_pairs.append((i, j))
                self.assertEqual(
                    zone["tied_pairs_count"], len(best_pairs), f"trial {trial} zone {zone_index}"
                )
                self.assertEqual(
                    Fraction(zone["min_sse"]["fraction"]), best, f"trial {trial} zone {zone_index}"
                )
                self.assertEqual(zone["identifiable"], len(best_pairs) == 1)
                if zone["identifiable"]:
                    expect = tuple(sorted(coefficient_label(k) for k in best_pairs[0]))
                    self.assertEqual(pair_of(zone), expect)


if __name__ == "__main__":
    unittest.main()
