import os
import unittest
from unittest.mock import patch

import aggregate
from scripts import distributed_control as control
from test_reports import summary


def entry(index, local, start=1000, end=2000):
    data = summary()
    data["forum_test"].update({"planned_vus": local, "planned_total_vus": 5, "shard_index": index, "shard_count": 2})
    data["metrics"].update({"sampled_vus": {"values": {"max": local}}, "peak_started_at": {"values": {"value": start}}, "peak_ended_at": {"values": {"value": end}}})
    return data, {"resource_valid": True, "exit_code": 0}


class DistributedTests(unittest.TestCase):
    def test_allocation_is_exact_and_safe_to_parse(self):
        plan = control.make_plan("100000", "19", "3m")
        allocations = [row["vus"] for row in plan["matrix"]["include"]]
        self.assertEqual(sum(allocations), 100000)
        self.assertEqual(allocations[:4], [5264, 5264, 5264, 5263])
        for value in ["100001", "0", "-1", "5abc", "1.1", "$(id)"]:
            with self.assertRaises(ValueError):
                control.make_plan(value, "19", "3m")
        with self.assertRaises(ValueError):
            control.make_plan("100000", "1", "3m")

    def test_shared_peak_and_weighted_average(self):
        plan = control.make_plan("5", "2", "1m")
        first, second = entry(1, 3), entry(2, 2, 1500, 2500)
        report = aggregate.aggregate(plan, [first, second])
        self.assertTrue(report["passed"])
        self.assertIn("5", dict(report["rows"])["👥 שיא מתוכנן שנצפה בכל המחשבים בחלונות חופפים"])
        self.assertIn("p95 הגבוה", " ".join(label for label, _ in report["rows"]))
        self.assertEqual(len(report["workers"]), 2)

    def test_p95s_are_never_averaged(self):
        first, second = entry(1, 3), entry(2, 2)
        second[0]["metrics"]["visit_duration"]["values"]["p(95)"] = 4000
        report = aggregate.aggregate(control.make_plan("5", "2", "1m"), [first, second])
        self.assertFalse(report["passed"])
        self.assertIn("4,000", dict(report["rows"])["🐢 p95 הגבוה ביותר במחשב בדיקה אחד"])

    def test_missing_duplicate_unmatched_and_nonoverlapping_workers_fail(self):
        plan = control.make_plan("5", "2", "1m")
        for entries in [[entry(1, 3)], [entry(1, 3), entry(1, 3)], [entry(1, 3), entry(2, 2, 3000, 4000)], [entry(1, 2), entry(2, 2)]]:
            self.assertFalse(aggregate.aggregate(plan, entries)["passed"])

    def test_generator_failure_does_not_blame_the_forum(self):
        first, second = entry(1, 3), entry(2, 2)
        second[1].update({"resource_valid": False, "resource_error": "CPU", "exit_code": 86})
        report = aggregate.aggregate(control.make_plan("5", "2", "1m"), [first, second])
        self.assertFalse(report["passed"])
        self.assertIn("CPU", " ".join(report["notes"]))

    def test_missing_worker_resource_metrics_is_incomplete(self):
        report = aggregate.aggregate(control.make_plan("5", "2", "1m"), [entry(1, 3), (entry(2, 2)[0], {})])
        self.assertFalse(report["passed"])

    def test_stale_start_and_aborted_coordination_cannot_release_load(self):
        with patch.dict(os.environ, {"TOTAL_VUS": "5", "SHARD_COUNT": "2", "TEST_DURATION": "1m", "GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": "1"}):
            with self.assertRaisesRegex(ValueError, "אחרת"):
                control.resume({"run_id": "9", "attempt": "1"})
            with self.assertRaisesRegex(ValueError, "הכנה"):
                control.resume({"aborted": True})
            plan = control.make_plan("5", "2", "1m")
            with self.assertRaisesRegex(ValueError, "חלף"):
                control.resume({**plan, "start_at": 1})

    def test_coordinator_requires_every_prepared_worker(self):
        env = {"GITHUB_RUN_ID": "10", "GITHUB_RUN_ATTEMPT": "1"}
        with patch.dict(os.environ, env):
            plan = control.make_plan("5", "2", "1m")
            names = [{"id": 1, "name": "bnebrak-ready-10-1-1"}, {"id": 2, "name": "bnebrak-ready-10-1-2"}]
            ready = {1: {"index": 1, "vus": 3, "initialized": 3, "run_id": "10", "attempt": "1"},
                     2: {"index": 2, "vus": 2, "initialized": 2, "run_id": "10", "attempt": "1"}}
            with patch.object(control, "artifacts", return_value=names), patch.object(control, "artifact_json", side_effect=lambda artifact, name: ready[artifact["id"]]), patch.object(control, "write_json") as write:
                result = control.coordinate(plan)
                self.assertEqual(result["total_vus"], 5)
                write.assert_called_once()
            with patch.object(control, "artifacts", return_value=names[:1]), patch.object(control.time, "monotonic", side_effect=[0, 0, 601]), patch.object(control.time, "sleep"), patch.object(control, "write_json") as write:
                with self.assertRaisesRegex(ValueError, "לא כל"):
                    control.coordinate(plan)
                write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
