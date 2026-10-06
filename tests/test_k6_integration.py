"""הרצת k6 האמיתי מול localhost; בדיקות אלה לעולם אינן פונות לפורום."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import summarize

K6 = shutil.which("k6")
if os.environ.get("K6_REQUIRED") == "1" and not K6:
    raise RuntimeError("k6 is required for the integration checks")
PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(K6, "k6 אינו מותקן מקומית; בדיקות האינטגרציה רצות ב-GitHub")
class K6IntegrationTests(unittest.TestCase):
    def run_scenario(self, mode):
        class Handler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self):
                Handler.requests += 1
                status = 403 if mode == "blocked" else 429 if mode == "limited" and Handler.requests > 1 else 200
                body = ("פורום בני ברק" if status == 200 else "blocked").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, "TARGET_URL": f"http://127.0.0.1:{server.server_port}", "TOTAL_VUS": "1", "TEST_DURATION": "10s", "K6_NO_USAGE_REPORT": "true"}
                result = subprocess.run([K6, "run", "--quiet", str(PROJECT / "load-test.js")], cwd=directory, env=env, capture_output=True, text=True, timeout=45)
                summary_file = Path(directory) / "summary.json"
                self.assertTrue(summary_file.is_file(), result.stdout + result.stderr)
                data = json.loads(summary_file.read_text(encoding="utf-8"))
                return result, data, Handler.requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_successful_run(self):
        result, data, requests = self.run_scenario("healthy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(summarize.make_report(data, exit_code=0)["passed"])
        self.assertGreater(requests, 1)
        self.assertEqual(summarize.metric_number(data, "failed_visits"), 0)
        self.assertEqual(summarize.metric_number(data, "visits"), requests - 1)

    def test_preflight_blocked(self):
        result, data, requests = self.run_scenario("blocked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(requests, 1)
        self.assertFalse(summarize.make_report(data, exit_code=result.returncode)["passed"])
        self.assertIn("403", result.stdout + result.stderr)

    def test_rate_limit_under_load_is_not_reported_as_success(self):
        result, data, requests = self.run_scenario("limited")
        self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
        self.assertGreater(summarize.metric_number(data, "limited_429"), 0)
        self.assertEqual(summarize.metric_number(data, "failed_visits"), requests - 1)
        self.assertFalse(summarize.make_report(data, exit_code=99)["passed"])


if __name__ == "__main__":
    unittest.main()
