"""סיכום ריצה מבוזרת, בלי לחשב p95 גלובלי מממוצע של אחוזונים."""

import argparse
import json
from pathlib import Path

import summarize


def aggregate(plan, entries):
    expected = {row["index"]: row["vus"] for row in plan["matrix"]["include"]}
    seen = set()
    total = succeeded = failed = weighted_time = 0
    maximum = 0
    p95s = []
    p99s = []
    peak_starts = []
    peak_ends = []
    observed_peak = 0
    valid = True
    all_passed = True
    rows_by_worker = []
    reasons = []
    categories = {name: 0 for name in ("blocked_403", "limited_429", "server_errors", "network_errors", "unexpected_responses")}
    for data, metrics in entries:
        metadata = data.get("forum_test", {})
        index = metadata.get("shard_index")
        if index not in expected or index in seen:
            valid = False
            reasons.append("אחד הדוחות שייך למחשב לא צפוי או כפול")
            continue
        seen.add(index)
        count = summarize.metric_number(data, "visits")
        good = summarize.metric_number(data, "successful_visits")
        bad = summarize.metric_number(data, "failed_visits")
        avg = summarize.metric_number(data, "visit_duration", "avg")
        p95 = summarize.metric_number(data, "visit_duration", "p(95)")
        p99 = summarize.metric_number(data, "visit_duration", "p(99)")
        peak = summarize.metric_number(data, "sampled_vus", "max")
        start = summarize.metric_number(data, "peak_started_at", "value")
        end = summarize.metric_number(data, "peak_ended_at", "value")
        local_ok = metrics.get("resource_valid") is True and metrics.get("exit_code") == 0
        settings_ok = metadata.get("planned_total_vus") == plan["total_vus"] and metadata.get("planned_vus") == expected[index] and metadata.get("shard_count") == len(expected)
        if not settings_ok or count is None or good is None or bad is None or good + bad != count or not count or avg is None or p95 is None:
            valid = local_ok = False
            reasons.append(f"מחשב {index}: נתונים חסרים, לא עקביים או תוכנית שגויה")
        if not metrics.get("resource_valid"):
            valid = False
            reasons.append(f"מחשב {index}: {metrics.get('resource_error') or 'חסרים נתוני תקינות של מחשב המדידה'}")
        if metrics.get("exit_code") not in (0, 99):
            valid = False
            reasons.append(f"מחשב {index}: הריצה לא הושלמה; קוד סיום {metrics.get('exit_code', 'לא נשמר')}")
        if peak is None or peak != expected[index] or start is None or end is None or end < start:
            valid = False
            reasons.append(f"מחשב {index}: אין דגימות שמאשרות הגעה לשיא המתוכנן")
        else:
            observed_peak += peak
            peak_starts.append(start)
            peak_ends.append(end)
        if count is not None and count > 0 and good is not None and bad is not None and avg is not None:
            total += count
            succeeded += good
            failed += bad
            weighted_time += avg * count
        maximum = max(maximum, summarize.metric_number(data, "visit_duration", "max") or 0)
        if p95 is not None:
            p95s.append(p95)
        if p99 is not None:
            p99s.append(p99)
        for name in categories:
            value = summarize.metric_number(data, name)
            if value is None:
                valid = False
            else:
                categories[name] += value
        local_report = summarize.make_report(data, exit_code=metrics.get("exit_code"))
        all_passed = all_passed and local_report["passed"] and local_ok
        rows_by_worker.append([
            str(index), summarize.integer(expected[index]), summarize.integer(peak), summarize.millis(p95),
            summarize.percentage(bad / count if count and bad is not None else None),
            "✅ עבר" if local_report["passed"] and local_ok else "⚠️ לבדוק",
        ])
    missing = sorted(set(expected) - seen)
    if missing:
        valid = False
        reasons.append("חסרים דוחות ממחשבים: " + ", ".join(map(str, missing)))
    overlap_ms = min(peak_ends) - max(peak_starts) if len(peak_starts) == len(expected) and peak_starts else None
    peak_confirmed = overlap_ms is not None and overlap_ms > 0 and observed_peak == plan["total_vus"]
    if not peak_confirmed:
        valid = False
        reasons.append("אין חפיפה מאומתת של חלונות השיא בכל מחשבי הבדיקה")
    passed = valid and all_passed
    title = "✅ היעד המקבילי אומת וכל מחשבי הבדיקה עמדו ביעדים" if passed else "⚠️ הבדיקה לא עמדה בכל היעדים" if valid else "🛑 אין די נתונים לאימות הבדיקה המלאה"
    explanation = "הכמות המבוקשת נצפתה בחלונות שיא חופפים. לכל מחשב חושבו יעדי המהירות והשגיאות בנפרד." if valid else "מספר המשתמשים המבוקש הוא יעד בלבד. דוח חלקי או ריצה שאינה מסונכרנת אינם מוכיחים שהפורום נבדק מול הכמות הזו."
    blank = {"metrics": {name: {"values": {"count": value}} for name, value in categories.items()}}
    base = summarize.make_report(blank)
    rate = failed / total if total else None
    worst_p95 = max(p95s) if p95s else None
    worst_p99 = max(p99s) if p99s else None
    rows = [
        ("🎯 משתמשים במקביל — יעד", summarize.integer(plan["total_vus"])),
        ("🧩 מחשבי בדיקה עם דוח", f"{len(seen)} מתוך {len(expected)}"),
        ("👥 שיא מתוכנן שנצפה בכל המחשבים בחלונות חופפים", summarize.integer(plan["total_vus"]) if peak_confirmed else "לא אומת"),
        ("🔗 חפיפה בין חלונות השיא שנדגמו", f"{overlap_ms / 1000:.1f} שניות" if peak_confirmed else "לא אומתה"),
        ("📨 כניסות שנמדדו בדוחות הזמינים", summarize.integer(total)),
        ("✅ כניסות שהצליחו", summarize.integer(succeeded)),
        ("❌ כניסות שנכשלו", summarize.integer(failed)),
        ("📉 שיעור כניסות כושלות", summarize.percentage(rate)),
        ("⚡ זמן כניסה ממוצע — משוקלל לפי מספר כניסות", summarize.millis(weighted_time / total if total else None)),
        ("🐢 p95 הגבוה ביותר במחשב בדיקה אחד", summarize.millis(worst_p95)),
        ("🐌 p99 הגבוה ביותר במחשב בדיקה אחד", summarize.millis(worst_p99)),
        ("⌛ הכניסה האיטית ביותר בדוחות הזמינים", summarize.millis(maximum if total else None)),
    ]
    notes = [
        "כל המשתמשים מוכנים ב-k6 במצב עצור, וכל מחשבי הבדיקה משוחררים באותו מועד. אם חסר מחשב, העומס אינו משוחרר.",
        "אימות הכמות מסתמך על דגימות משתמשים פעילים בשיא ועל חפיפת חלונות השיא לפי שעוני המחשבים. שעונים לא מסונכרנים עלולים לפגוע באימות.",
        "הדוח מציג את p95 הגבוה ביותר בין המחשבים, ולא p95 גלובלי. ממוצע של אחוזונים אינו אחוזון של כל הבקשות.",
        "הממוצע הכללי משוקלל לפי מספר הכניסות בכל מחשב. מוני ההצלחות והכישלונות מחוברים מתוך הדוחות הזמינים.",
        "בדיקות המשאבים מזהות חוסר זיכרון ועומס CPU במחשבי המדידה; הן אינן מודדות כל מגבלת רשת אפשרית.",
        "100,000 משתמשים מדומים במקביל אינם 100,000 בקשות באותה אלפית שנייה: כל משתמש גם ממתין בין כניסות.",
        "בריצה חלקית גם המונים הם חלקיים. יש לבדוק את הלוגים של המחשבים שלא השיגו את היעד.",
    ] + base["notes"]
    if reasons:
        notes = ["⚠️ " + reason for reason in dict.fromkeys(reasons)] + notes
    return {"title": title, "tone": "success" if passed else "warning", "explanation": explanation, "target": "https://bnebrak.com/",
            "rows": rows, "errors": base["errors"], "targets": [("אימות הכמות בחלונות שיא חופפים", "✅ הושג" if peak_confirmed else "❌ לא אומת"),
                ("בכל מחשב: p95 נמוך מ־3 שניות ופחות מ־2% תקלות", "✅ הושג" if all_passed and not missing else "❌ לא הושג")],
            "notes": notes, "passed": passed, "workers": sorted(rows_by_worker, key=lambda row: int(row[0]))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, default=Path("report.md"))
    parser.add_argument("--html", type=Path, default=Path("report.html"))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    entries = []
    for filename in args.results.glob("**/summary.json"):
        try:
            data = json.loads(filename.read_text(encoding="utf-8"))
            metrics_file = filename.with_name("worker-metrics.json")
            metrics = json.loads(metrics_file.read_text(encoding="utf-8")) if metrics_file.is_file() else {}
            entries.append((data, metrics))
        except (OSError, ValueError):
            continue
    report = aggregate(plan, entries)
    markdown = summarize.render_markdown(report)
    args.markdown.write_text(markdown, encoding="utf-8")
    args.html.write_text(summarize.render_html(report), encoding="utf-8")
    print(markdown, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
