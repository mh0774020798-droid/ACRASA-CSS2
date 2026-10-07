"""אימות כל מסלול prepare/pause/resume על שני מחשבי דמה, ללא אתר חיצוני."""

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aggregate
from scripts import distributed_control as control

K6 = shutil.which("k6")
PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(K6, "k6 מותקן ב-GitHub לצורך בדיקות האינטגרציה")
class PauseIntegrationTests(unittest.TestCase):
    def test_two_prepared_workers_resume_together_and_produce_verified_peak(self):
        class Handler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self):
                Handler.requests += 1
                body = "פורום בני ברק".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = str(PROJECT / "scripts/distributed_control.py")
        folders = []
        environments = []
        temporary_directory = tempfile.TemporaryDirectory()
        try:
            temporary = temporary_directory.name
            for index in (1, 2):
                folder = Path(temporary) / str(index)
                folder.mkdir()
                folders.append(folder)
                env = {**os.environ, "TARGET_URL": f"http://127.0.0.1:{server.server_port}", "TOTAL_VUS": "2", "SHARD_COUNT": "2", "SHARD_INDEX": str(index), "DISTRIBUTED": "1", "TEST_DURATION": "10s", "GITHUB_RUN_ID": "local", "GITHUB_RUN_ATTEMPT": "1", "K6_NO_USAGE_REPORT": "true"}
                environments.append(env)
                prepared = subprocess.run([sys.executable, script, "launch"], cwd=folder, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr + (folder / "k6.log").read_text())
                state = control.read_json(folder / "worker-process.json")
                self.assertTrue(control.api(state["port"])["paused"])
            # Setup may issue a single preflight; paused workers cannot produce load.
            self.assertLessEqual(Handler.requests, 2)
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "local", "GITHUB_RUN_ATTEMPT": "1"}):
                plan = control.make_plan("2", "2", "10s")
            start = {**plan, "start_at": time.time() + 1}
            for folder in folders:
                control.write_json(folder / "start.json", start)
            def resume_worker(index):
                return subprocess.run([sys.executable, script, "resume", "--start-file", "start.json"], cwd=folders[index], env=environments[index], capture_output=True, text=True, timeout=35)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(resume_worker, (0, 1)))
            entries = []
            resumed_at = []
            for folder, result in zip(folders, results):
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr + (folder / "monitor.log").read_text())
                metrics = control.read_json(folder / "worker-metrics.json")
                self.assertTrue(metrics["resource_valid"])
                resumed_at.append(control.read_json(folder / "resumed.json")["at"])
                entries.append((control.read_json(folder / "summary.json"), metrics))
            self.assertLess(max(resumed_at) - min(resumed_at), 2)
            report = aggregate.aggregate(plan, entries)
            self.assertTrue(report["passed"], json.dumps(report, ensure_ascii=False))
        finally:
            for folder, env in zip(folders, environments):
                if folder.exists():
                    subprocess.run([sys.executable, script, "cleanup"], cwd=folder, env=env, capture_output=True, timeout=25)
            temporary_directory.cleanup()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
