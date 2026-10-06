"""סנכרון מחשבי k6 דרך Artifacts, תוך שימוש ב-REST API המקומי של k6."""

import argparse
import io
import json
import math
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def write_json(path, data):
    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def positive_integer(value, maximum, label):
    if not str(value).isascii() or not str(value).isdigit() or not 1 <= int(value) <= maximum:
        raise ValueError(f"{label}: יש לבחור מספר שלם בין 1 ל־{maximum}")
    return int(value)


def make_plan(total, shards, duration):
    total = positive_integer(total, 100000, "👥 משתמשים")
    shards = positive_integer(shards, 19, "🧩 מחשבי בדיקה")
    if shards > total:
        raise ValueError("🧩 מספר מחשבי הבדיקה גדול ממספר המשתמשים")
    # 10s is used only by the localhost integration suite; the UI exposes minutes.
    if duration not in ("10s", "1m", "3m", "5m"):
        raise ValueError("⏱️ יש לבחור 1m, 3m או 5m")
    allocations = [total // shards + (index < total % shards) for index in range(shards)]
    if max(allocations) > 10000:
        raise ValueError("🧩 נדרשים יותר מחשבי בדיקה: לכל מחשב מותר יעד של עד 10,000 משתמשים")
    return {"total_vus": total, "shard_count": shards, "duration": duration,
            "matrix": {"include": [{"index": index + 1, "vus": users} for index, users in enumerate(allocations)]},
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")}


def api(port, attributes=None):
    payload = None if attributes is None else json.dumps({"data": {"type": "status", "id": "default", "attributes": attributes}}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/v1/status", data=payload,
                                     headers={"Content-Type": "application/json"}, method="GET" if payload is None else "PATCH")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)["data"]["attributes"]


def github(path, binary=False):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("🧭 חסרה הרשאת GitHub לקריאת Artifacts")
    request = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    # Strip repository credentials before following a signed storage redirect.
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if urllib.parse.urlparse(newurl).netloc != "api.github.com":
                redirected.remove_header("Authorization")
            return redirected
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=20) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("🧭 קובץ תיאום גדול מהצפוי")
        return raw if binary else json.loads(raw)


def artifacts():
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    return github(f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100").get("artifacts", [])


def artifact_json(artifact, filename):
    raw = github(f"/repos/{os.environ['GITHUB_REPOSITORY']}/actions/artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(filename)
        if info.file_size > 100000:
            raise ValueError("🧭 נתוני תיאום גדולים מהצפוי")
        return json.loads(archive.read(info))


def artifact_prefix(kind):
    return f"bnebrak-{kind}-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"


def memory_capacity():
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.strip().split()[0]) * 1024
    total = fields["MemTotal"]
    available = fields["MemAvailable"]
    try:
        maximum = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if maximum != "max":
            total = min(total, int(maximum))
            used = int(Path("/sys/fs/cgroup/memory.current").read_text())
            available = min(available, max(0, int(maximum) - used))
    except OSError:
        pass
    return total, available


def effective_cpus():
    cores = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cores = min(cores, int(quota) / int(period))
    except OSError:
        pass
    return max(0.1, cores)


def process_usage(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    rss = next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:"))
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return rss, (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")


def monitor(port):
    child_env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN")}
    child_env["K6_NO_USAGE_REPORT"] = "true"
    with Path("k6.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(["k6", "run", "--quiet", "--paused", "--address", f"127.0.0.1:{port}", str(PROJECT / "load-test.js")], env=child_env, stdout=log, stderr=subprocess.STDOUT)
        write_json("worker-process.json", {"port": port, "pid": process.pid})
        peak_rss = peak_cpu = 0
        previous = None
        hot_samples = 0
        resource_error = None
        deadline = time.monotonic() + 1200
        while process.poll() is None:
            try:
                rss, cpu_seconds = process_usage(process.pid)
                now = time.monotonic()
                total, available = memory_capacity()
                peak_rss = max(peak_rss, rss)
                running = Path("resumed.json").exists()
                cpu = (cpu_seconds - previous[1]) / (now - previous[0]) / effective_cpus() if previous and running else 0
                previous = (now, cpu_seconds)
                if running:
                    peak_cpu = max(peak_cpu, cpu)
                    hot_samples = hot_samples + 1 if cpu > 0.80 else 0
                if rss > total * 0.85 or available < total * 0.05:
                    resource_error = "🧠 למחשב הבדיקה אין מספיק זיכרון; המדידה נעצרה כדי לא לייחס תקלה זו לפורום"
                elif hot_samples >= 3:
                    resource_error = "⚙️ מחשב הבדיקה הגיע לעומס CPU גבוה במספר דגימות; תוצאת המהירות אינה מאומתת"
                elif now > deadline:
                    resource_error = "⏱️ מחשב הבדיקה חרג מזמן ההכנה והריצה המותר"
                if resource_error:
                    try:
                        api(port, {"stopped": True})
                    except (OSError, ValueError, KeyError):
                        process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
            except (OSError, StopIteration):
                if process.poll() is not None:
                    break
            time.sleep(2)
        code = process.wait()
        if resource_error:
            code = 86
        metrics = {"exit_code": code, "resource_error": resource_error, "resource_valid": resource_error is None,
                   "peak_rss_mb": round(peak_rss / 1024 ** 2, 1), "peak_cpu_percent": round(peak_cpu * 100, 1),
                   "shard_index": int(os.environ.get("SHARD_INDEX", "1"))}
        write_json("worker-metrics.json", metrics)
        Path("k6-exit-code.txt").write_text(str(code), encoding="utf-8")


def launch():
    for name in ("worker-process.json", "worker-metrics.json", "resumed.json", "ready.json", "summary.json", "k6-exit-code.txt", "k6.log"):
        Path(name).unlink(missing_ok=True)
    with socket.socket() as selection:
        selection.bind(("127.0.0.1", 0))
        port = selection.getsockname()[1]
    with Path("monitor.log").open("w", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "monitor", "--port", str(port)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    total = int(os.environ["TOTAL_VUS"])
    count = int(os.environ["SHARD_COUNT"])
    index = int(os.environ["SHARD_INDEX"])
    local = total // count + (index <= total % count)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if Path("worker-metrics.json").exists():
            raise ValueError("🛑 מחשב הבדיקה נעצר בזמן ההכנה; פרטים ב-k6.log וב-worker-metrics.json")
        try:
            status = api(port)
            if status.get("paused") and status.get("vus-max", 0) >= local:
                process = read_json("worker-process.json")
                rss, _ = process_usage(process["pid"])
                capacity, available = memory_capacity()
                if rss > capacity * 0.85 or available < capacity * 0.05:
                    raise ValueError("🧠 אין די זיכרון להכנת המשתמשים במחשב הזה")
                write_json("ready.json", {"index": index, "vus": local, "initialized": status["vus-max"], "rss_mb": round(rss / 1024 ** 2, 1),
                                          "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1")})
                print(f"✅ מחשב {index}/{count} הכין {local:,} משתמשים ועוצר בהמתנה ליתר המחשבים", flush=True)
                return
        except (urllib.error.URLError, KeyError, FileNotFoundError):
            pass
        time.sleep(2)
    raise ValueError("⏱️ מחשב הבדיקה לא סיים להכין את המשתמשים בזמן")


def coordinate(plan, timeout=600):
    prefix = artifact_prefix("ready")
    deadline = time.monotonic() + timeout
    expected = {f"{prefix}-{row['index']}": row for row in plan["matrix"]["include"]}
    previous_count = -1
    while time.monotonic() < deadline:
        found = {item["name"]: item for item in artifacts() if not item.get("expired") and item["name"] in expected}
        if len(found) != previous_count:
            previous_count = len(found)
            print(f"🧭 מוכנים: {len(found)}/{len(expected)} מחשבי בדיקה", flush=True)
        if set(found) == set(expected):
            for name, row in expected.items():
                ready = artifact_json(found[name], "ready.json")
                if ready.get("index") != row["index"] or ready.get("vus") != row["vus"] or ready.get("initialized", 0) < row["vus"] or ready.get("run_id") != plan["run_id"] or ready.get("attempt") != plan["attempt"]:
                    raise ValueError("🛑 אחד ממחשבי הבדיקה הכין כמות שגויה או שייך לריצה אחרת")
            start = {**plan, "start_at": time.time() + 45}
            write_json("start.json", start)
            print(f"🟢 כל {len(expected)} המחשבים מוכנים. מתחילים יחד בעוד 45 שניות: יעד {plan['total_vus']:,} משתמשים", flush=True)
            return start
        time.sleep(10)
    raise ValueError("⏱️ לא כל מחשבי הבדיקה נעשו זמינים בזמן. הריצה לא שוחררה לעומס; ייתכן תור או מגבלת מקביליות ב-GitHub")


def wait_start(timeout=600):
    name = artifact_prefix("start")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in artifacts():
            if item["name"] == name and not item.get("expired"):
                return artifact_json(item, "start.json")
        # 19 workers polling every 20 seconds stay below GitHub's shared token
        # budget during a normal ten-minute preparation window.
        time.sleep(20)
    raise ValueError("⏱️ לא התקבל אישור התחלה משותפת בזמן")


def resume(start):
    if start.get("aborted"):
        raise ValueError(start.get("reason", "🛑 ההכנה של הריצה המבוזרת נכשלה"))
    if start.get("run_id") != os.environ.get("GITHUB_RUN_ID", "local") or start.get("attempt") != os.environ.get("GITHUB_RUN_ATTEMPT", "1"):
        raise ValueError("🛑 נתוני ההתחלה שייכים לריצה אחרת")
    plan = make_plan(os.environ["TOTAL_VUS"], os.environ["SHARD_COUNT"], os.environ["TEST_DURATION"])
    if start.get("total_vus") != plan["total_vus"] or start.get("matrix") != plan["matrix"] or start.get("duration") != plan["duration"]:
        raise ValueError("🛑 נתוני העומס אינם תואמים לתוכנית המשותפת")
    at = start.get("start_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at > time.time() + 120 or at < time.time() - 2:
        raise ValueError("⏱️ מועד ההתחלה חלף או אינו תקין; אין להציג ריצה לא מסונכרנת כ־100,000 במקביל")
    process = read_json("worker-process.json")
    while time.time() < at:
        if Path("worker-metrics.json").exists():
            raise ValueError("🛑 מחשב המדידה נעצר לפני ההתחלה")
        time.sleep(min(0.2, max(0, at - time.time())))
    if time.time() - at > 2:
        raise ValueError("⏱️ מחשב הבדיקה איחר למועד ההתחלה")
    api(process["port"], {"paused": False})
    write_json("resumed.json", {"at": time.time()})
    print("🚀 כל המשתמשים הוכנו; הבדיקה ההדרגתית מתחילה במועד המשותף", flush=True)
    with Path("k6.log").open(encoding="utf-8", errors="replace") as log:
        while not Path("worker-metrics.json").exists():
            chunk = log.read()
            if chunk:
                print(chunk, end="", flush=True)
            time.sleep(1)
        print(log.read(), end="", flush=True)
    metrics = read_json("worker-metrics.json")
    if metrics.get("resource_error"):
        print(metrics["resource_error"], flush=True)
    return metrics["exit_code"]


def cleanup():
    if Path("worker-process.json").exists() and not Path("worker-metrics.json").exists():
        try:
            api(read_json("worker-process.json")["port"], {"stopped": True})
        except (OSError, ValueError, KeyError):
            pass
        deadline = time.monotonic() + 20
        while not Path("worker-metrics.json").exists() and time.monotonic() < deadline:
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "launch", "monitor", "coordinate", "resume", "cleanup"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--plan", type=Path, default=Path("plan.json"))
    parser.add_argument("--start-file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            plan = make_plan(os.environ["TOTAL_VUS"], os.environ["SHARD_COUNT"], os.environ["TEST_DURATION"])
            write_json(args.plan, plan)
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                    output.write("matrix=" + json.dumps(plan["matrix"]) + "\n")
            print(f"🎯 יעד כולל: {plan['total_vus']:,} משתמשים; מחשבי בדיקה: {plan['shard_count']}")
        elif args.command == "monitor":
            monitor(args.port)
        elif args.command == "launch":
            launch()
        elif args.command == "coordinate":
            coordinate(read_json(args.plan))
        elif args.command == "resume":
            return resume(read_json(args.start_file) if args.start_file else wait_start())
        else:
            cleanup()
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        if args.command == "coordinate":
            write_json("start.json", {"aborted": True, "reason": str(exc)})
        print(f"🛑 {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
