"""End-to-end API tests for the measured coefficient calibration endpoint."""

import os
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

# Use an isolated database before importing the app.
_tmpdir = tempfile.mkdtemp(prefix="oven-cal-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmpdir) / "oven.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.calibration import exact_finite_decimal, predict_temperatures  # noqa: E402
from app.main import app, init_db  # noqa: E402

init_db()
client = TestClient(app)


def solve_payload(lock1, lock2, lock3):
    return {
        "duration": 3,
        "max_setpoint_change": 300,
        "locked_minutes": {"1": lock1, "2": lock2, "3": lock3},
        "zones": [
            {
                "name": "A 区",
                "initial_temperature": 20,
                "coefficients": ["0.25", "0.60"],
                "final_low": "0",
                "final_high": "300",
                "max_temperature": "300",
                "exposure_threshold": "300",
                "min_exposure": "0",
            },
            {
                "name": "B 区",
                "initial_temperature": 80,
                "coefficients": ["0.15", "0.45"],
                "final_low": "0",
                "final_high": "300",
                "max_temperature": "300",
                "exposure_threshold": "300",
                "min_exposure": "0",
            },
        ],
    }


def create_curve_snapshot(lock1, lock2, lock3):
    response = client.post("/api/solve", json=solve_payload(lock1, lock2, lock3))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] in ("optimal", "feasible_not_optimal"), data["status"]
    return data["snapshot_id"], data["setpoints"]


def exact_readings(setpoints, initial, alpha_label, minutes):
    alpha = Fraction(alpha_label)
    temperatures = predict_temperatures(Fraction(initial), setpoints, alpha)
    return [
        {"minute": minute, "temperature": exact_finite_decimal(temperatures[minute])}
        for minute in minutes
    ]


def calibrate_payload(entries):
    """entries: list of (snapshot_id, zones) where zones[z] = (series1, series2)."""
    return {
        "snapshots": [
            {
                "snapshot_id": snapshot_id,
                "zones": [{"series": [list(zone[0]), list(zone[1])]} for zone in zones],
            }
            for snapshot_id, zones in entries
        ]
    }


class CalibrationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap1, cls.curve1 = create_curve_snapshot(100, 150, 200)
        cls.snap2, cls.curve2 = create_curve_snapshot(50, 120, 90)
        # A no-curve snapshot (proved unsatisfiable) for the no_curve error.
        unsat = solve_payload(100, 150, 200)
        unsat["zones"][0]["final_low"] = "200"
        unsat["zones"][0]["final_high"] = "250"
        unsat["zones"][0]["max_temperature"] = "150"
        unsat["zones"][1]["final_low"] = "290"
        unsat["zones"][1]["final_high"] = "300"
        unsat["zones"][1]["max_temperature"] = "140"
        response = client.post("/api/solve", json=unsat)
        assert response.status_code == 200
        cls.no_curve_snap = response.json()["snapshot_id"]
        assert response.json()["status"] == "no_solution"

    def good_payload(self):
        # True pairs: zone A (0.25, 0.60), zone B (0.15, 0.45).
        return calibrate_payload(
            [
                (
                    self.snap1,
                    [
                        (
                            exact_readings(self.curve1, 20, "0.25", [1, 3]),
                            exact_readings(self.curve1, 20, "0.60", [2]),
                        ),
                        (
                            exact_readings(self.curve1, 80, "0.15", [1, 2, 3]),
                            exact_readings(self.curve1, 80, "0.45", [3]),
                        ),
                    ],
                ),
                (
                    self.snap2,
                    [
                        (
                            exact_readings(self.curve2, 20, "0.25", [2]),
                            exact_readings(self.curve2, 20, "0.60", [1, 2, 3]),
                        ),
                        (
                            exact_readings(self.curve2, 80, "0.15", [1]),
                            exact_readings(self.curve2, 80, "0.45", [1, 3]),
                        ),
                    ],
                ),
            ]
        )

    def test_successful_joint_calibration(self):
        response = client.post("/api/calibrate", json=self.good_payload())
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["applicable"])
        self.assertEqual([s["snapshot_id"] for s in data["snapshots"]], [self.snap1, self.snap2])
        zone_a, zone_b = data["zones"]
        self.assertEqual(zone_a["zone_name"], "A 区")
        self.assertEqual(zone_b["zone_name"], "B 区")
        self.assertEqual(zone_a["best"]["coefficients"], ["0.25", "0.60"])
        self.assertEqual(zone_b["best"]["coefficients"], ["0.15", "0.45"])
        self.assertEqual(zone_a["min_sse"]["fraction"], "0")
        self.assertEqual(zone_b["min_sse"]["fraction"], "0")
        self.assertFalse(zone_a["best"]["attribution_tied"])
        # Attribution of snapshot 1 zone A: series 1 -> 0.25, series 2 -> 0.60.
        assignment = zone_a["best"]["assignments"][0]
        self.assertEqual(assignment["snapshot_id"], self.snap1)
        self.assertEqual(assignment["series"][0]["coefficient"], "0.25")
        self.assertEqual(assignment["series"][1]["coefficient"], "0.60")
        # Residual table covers exactly the entered minutes.
        self.assertEqual(
            [r["minute"] for r in assignment["series"][0]["residuals"]], [1, 3]
        )
        self.assertTrue(
            all(
                r["residual"]["fraction"] == "0"
                for r in assignment["series"][0]["residuals"]
            )
        )

    def test_calibration_does_not_touch_snapshots_or_diagnoses(self):
        before = client.get("/api/snapshots").json()["items"]
        client.post("/api/calibrate", json=self.good_payload())
        after = client.get("/api/snapshots").json()["items"]
        self.assertEqual(before, after)

    def test_empty_series_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][0]["zones"][1]["series"][0] = []
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "empty_series")
        self.assertIn("至少需要一个读数", detail["message"])

    def test_duplicate_minute_rejected(self):
        payload = self.good_payload()
        series = payload["snapshots"][0]["zones"][0]["series"][0]
        series.append(dict(series[0]))
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "duplicate_minute")

    def test_out_of_range_minute_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][0]["zones"][0]["series"][0].append(
            {"minute": 4, "temperature": "25"}  # snapshot duration is 3
        )
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "minute_out_of_range")
        self.assertIn("第 1 至第 3 分钟", detail["message"])

    def test_invalid_temperature_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][0]["zones"][0]["series"][0][0]["temperature"] = "abc"
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)

        payload = self.good_payload()
        payload["snapshots"][0]["zones"][0]["series"][0][0]["temperature"] = "350"
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn("0 到 300", response.text)

        payload = self.good_payload()
        payload["snapshots"][0]["zones"][0]["series"][0][0]["temperature"] = "-1"
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_no_curve_snapshot_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][1]["snapshot_id"] = self.no_curve_snap
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "no_curve")

    def test_duplicate_snapshot_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][1]["snapshot_id"] = self.snap1
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "duplicate_snapshot")

    def test_missing_snapshot_rejected(self):
        payload = self.good_payload()
        payload["snapshots"][1]["snapshot_id"] = 99999
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "snapshot_not_found")

    def test_too_few_snapshots_rejected(self):
        payload = self.good_payload()
        payload["snapshots"] = payload["snapshots"][:1]
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_unidentifiable_zone_disables_apply(self):
        # Constant curve equal to both initials: every pair ties.
        def flat_snapshot():
            payload = solve_payload(20, 20, 20)
            payload["zones"][1]["initial_temperature"] = 20
            response = client.post("/api/solve", json=payload)
            data = response.json()
            assert data["setpoints"] == [20, 20, 20], data["status"]
            return data["snapshot_id"]

        snap3 = flat_snapshot()
        snap4 = flat_snapshot()

        def flat_series():
            return [
                [{"minute": 1, "temperature": "20"}],
                [{"minute": 2, "temperature": "20"}],
            ]

        payload = calibrate_payload(
            [(snap3, [flat_series(), flat_series()]), (snap4, [flat_series(), flat_series()])]
        )
        response = client.post("/api/calibrate", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["applicable"])
        for zone in data["zones"]:
            self.assertFalse(zone["identifiable"])
            self.assertIsNone(zone["best"])
            self.assertEqual(zone["tied_pairs_count"], 5050)
            self.assertGreater(len(zone["tied_pairs"]), 0)


if __name__ == "__main__":
    unittest.main()
