import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import summarize


def summary():
    def metric(**values):
        return {"values": values}
    return {
        "forum_test": {"target_url": "https://bnebrak.com/", "planned_vus": 5, "planned_duration_seconds": 60, "p95_target_ms": 3000, "error_target_rate": 0.02},
        "state": {"testRunDurationMs": 62000},
        "metrics": {
            "visits": metric(count=100), "successful_visits": metric(count=99), "failed_visits": metric(count=1),
            "visit_duration": {"values": {"avg": 300, "med": 250, "max": 1200, "p(95)": 500, "p(99)": 1000}, "thresholds": {"p(95)<3000": {"ok": True}}},
            "errors": {"values": {"rate": 0.01}, "thresholds": {"rate<0.02": {"ok": True}}},
            "blocked_403": metric(count=0), "limited_429": metric(count=1), "server_errors": metric(count=0), "network_errors": metric(count=0), "unexpected_responses": metric(count=0),
            "waiting_duration": metric(avg=230),
        },
    }


class ReportTests(unittest.TestCase):
    def test_nested_values_and_percentages(self):
        report = summarize.make_report(summary(), exit_code=0)
        markdown = summarize.render_markdown(report)
        self.assertTrue(report["passed"])
        self.assertIn("1.00%", markdown)
        self.assertIn("500 מילישניות (0.50 שניות)", markdown)
        self.assertNotIn("N/A", markdown)

    def test_legacy_flat_summary_export(self):
        old = {"metrics": {"http_reqs": {"count": 200}, "http_req_failed": {"rate": 0.05}, "http_req_duration": {"avg": 600, "p(95)": 1500}}}
        report = summarize.make_report(old)
        self.assertFalse(report["passed"])
        self.assertIn("5.00%", summarize.render_markdown(report))
        self.assertIn("לא נמדד", summarize.render_markdown(report))

    def test_slow_run_and_threshold_failure_are_visible(self):
        data = summary()
        data["metrics"]["visit_duration"]["values"]["p(95)"] = 3100
        self.assertFalse(summarize.make_report(data)["passed"])
        data = summary()
        data["metrics"]["errors"]["thresholds"]["rate<0.02"]["ok"] = False
        self.assertFalse(summarize.make_report(data)["passed"])

    def test_two_percent_is_a_failure(self):
        data = summary()
        data["metrics"]["failed_visits"]["values"]["count"] = 2
        data["metrics"]["successful_visits"]["values"]["count"] = 98
        self.assertFalse(summarize.make_report(data)["passed"])

    def test_empty_short_cancelled_and_inconsistent_runs_are_never_green(self):
        self.assertFalse(summarize.make_report({"metrics": {}})["passed"])
        data = summary()
        data["state"]["testRunDurationMs"] = 30000
        self.assertIn("לא הושלמה", summarize.make_report(data)["title"])
        self.assertFalse(summarize.make_report(summary(), exit_code=108)["passed"])
        self.assertFalse(summarize.make_report(summary(), exit_code=99)["passed"])
        data = summary()
        data["metrics"]["successful_visits"]["values"]["count"] = 100
        self.assertIn("אינם עקביים", summarize.make_report(data)["title"])

    def test_html_escapes_external_content(self):
        data = summary()
        data["forum_test"]["target_url"] = '<script>alert("x")</script>'
        result = summarize.render_html(summarize.make_report(data))
        self.assertIn("&lt;script&gt;", result)
        self.assertNotIn("<script>", result)
        self.assertIn('lang="he" dir="rtl"', result)

    def test_missing_json_still_writes_an_explanatory_report(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            completed = subprocess.run([sys.executable, "summarize.py", str(folder / "missing.json"), "--markdown", str(folder / "report.md"), "--html", str(folder / "report.html")], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("לא ניתן לקרוא", (folder / "report.md").read_text(encoding="utf-8"))
            self.assertTrue((folder / "report.html").is_file())


if __name__ == "__main__":
    unittest.main()
