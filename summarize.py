"""דוח קריא בעברית מנתוני handleSummary של k6, ללא חבילות נוספות."""

import argparse
import html
import json
import math
from pathlib import Path


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def metric_values(data, name):
    metric = data.get("metrics", {}).get(name, {})
    # handleSummary משתמש ב-values; summary-export הישן משתמש בשדות ישירים.
    return metric.get("values", metric) if isinstance(metric, dict) else {}


def metric_number(data, name, stat="count"):
    return number(metric_values(data, name).get(stat))


def integer(value):
    return f"{int(value):,}" if value is not None else "לא נמדד"


def millis(value):
    return f"{value:,.0f} מילישניות ({value / 1000:.2f} שניות)" if value is not None else "לא נמדד"


def percentage(value):
    return f"{value * 100:.2f}%" if value is not None else "לא נמדד"


def make_report(data, exit_code=None, read_error=None):
    metadata = data.get("forum_test", {})
    total = metric_number(data, "visits")
    failed = metric_number(data, "failed_visits")
    succeeded = metric_number(data, "successful_visits")
    duration_name = "visit_duration"
    is_legacy = total is None and "http_reqs" in data.get("metrics", {})
    if is_legacy:
        total = metric_number(data, "http_reqs")
        duration_name = "http_req_duration"
        failure_rate = metric_number(data, "http_req_failed", "rate")
        if total is not None and failure_rate is not None:
            failed = round(total * failure_rate)
            succeeded = total - failed
    failure_rate = failed / total if total and failed is not None else None
    times = metric_values(data, duration_name)
    p95 = number(times.get("p(95)"))
    p95_target = number(metadata.get("p95_target_ms")) or 3000
    error_target = number(metadata.get("error_target_rate")) or 0.02
    thresholds = [
        threshold
        for metric in data.get("metrics", {}).values()
        if isinstance(metric, dict)
        for threshold in metric.get("thresholds", {}).values()
    ]
    failed_threshold = any(isinstance(item, dict) and item.get("ok") is False for item in thresholds)
    observed_ms = number(data.get("state", {}).get("testRunDurationMs"))
    planned_seconds = number(metadata.get("planned_duration_seconds"))
    short_run = bool(observed_ms is not None and planned_seconds and observed_ms < planned_seconds * 1000 * 0.95)
    incomplete = read_error is not None or not total or failure_rate is None or p95 is None or short_run or exit_code not in (None, 0, 99)
    passed = not incomplete and not failed_threshold and p95 < p95_target and failure_rate < error_target and exit_code in (None, 0)
    if incomplete:
        title = "🛑 הבדיקה לא הושלמה או שאין מספיק נתונים"
        tone = "warning"
        explanation = "אין להסיק מתוצאה זו שהפורום עומד בעומס שבחרתם. בדקו את השלב שנכשל בלוגים."
    elif passed:
        title = "✅ הפורום עמד ביעדים של הבדיקה הזו"
        tone = "success"
        explanation = "הכניסות היו מהירות מספיק ושיעור התקלות נמוך מהיעד, בתנאים ובעומס שנבדקו."
    else:
        title = "⚠️ הפורום לא עמד בכל יעדי הבדיקה"
        tone = "warning"
        explanation = "המהירות, שיעור השגיאות או יעד אחר חרגו מהגבולות שהוגדרו. ייתכן שהבדיקה נעצרה מוקדם."

    rows = [
        ("👥 משתמשים מדומים — שיא מתוכנן", integer(number(metadata.get("planned_vus")))),
        ("⏱️ משך מתוכנן", f"{planned_seconds:g} שניות" if planned_seconds else "לא נמדד"),
        ("⏱️ משך הריצה שנמדד ב־k6", f"{observed_ms / 1000:.1f} שניות" if observed_ms is not None else "לא נמדד"),
        ("📨 כניסות שנבדקו" if not is_legacy else "📨 בקשות HTTP בדוח הישן", integer(total)),
        ("✅ כניסות שהצליחו" if not is_legacy else "✅ בקשות HTTP שהצליחו", integer(succeeded)),
        ("❌ כניסות שנכשלו" if not is_legacy else "❌ בקשות HTTP שנכשלו", integer(failed)),
        ("📉 שיעור כניסות כושלות", percentage(failure_rate)),
        ("⚡ זמן כניסה ממוצע", millis(number(times.get("avg")))),
        ("🟰 חציון — מחצית מהכניסות מהירות יותר", millis(number(times.get("med")))),
        ("🐢 p95 — 95% מהכניסות הסתיימו בתוך", millis(p95)),
        ("🐌 p99 — 99% מהכניסות הסתיימו בתוך", millis(number(times.get("p(99)")))),
        ("⌛ הכניסה האיטית ביותר", millis(number(times.get("max")))),
        ("🖥️ המתנה ממוצעת לתחילת תגובת HTTP", millis(metric_number(data, "waiting_duration", "avg"))),
    ]
    kinds = [
        ("🚫 403 — הגישה נחסמה", "blocked_403", "ייתכן כלל אבטחה, הרשאות או שירות הגנה; יש לבדוק בלוגים של האתר."),
        ("🚦 429 — יותר מדי בקשות", "limited_429", "האתר מגביל את קצב הבקשות. הקטינו את מספר המשתמשים ובדקו את ההגבלה."),
        ("🖥️ 5xx — שגיאת שרת", "server_errors", "השרת או שירות מתווך החזירו שגיאה. בדקו משאבים ולוגים בזמן הבדיקה."),
        ("🔌 אין תגובת HTTP", "network_errors", "ייתכן timeout, DNS, TLS או תקלה בחיבור; המדד לבדו אינו מזהה את הסיבה."),
        ("📄 תגובה לא צפויה", "unexpected_responses", "לדוגמה: הפניה, 404 או HTTP 200 בלי הטקסט המזוהה של הפורום."),
    ]
    errors = [(label, integer(metric_number(data, name)), meaning) for label, name, meaning in kinds]
    speed_verdict = "לא נמדד" if p95 is None else "✅ הושג" if p95 < p95_target else "❌ לא הושג"
    error_verdict = "לא נמדד" if failure_rate is None else "✅ הושג" if failure_rate < error_target else "❌ לא הושג"
    targets = [
        (f"95% מהכניסות בפחות מ־{p95_target / 1000:g} שניות", speed_verdict),
        (f"פחות מ־{error_target * 100:g}% כניסות כושלות", error_verdict),
    ]
    notes = [
        "משתמש מדומה הוא תהליך שמבקש את עמוד הבית, ממתין 1–3 שניות וחוזר שוב. זו אינה התחברות של משתמש אמיתי לפורום.",
        "p95 הוא גבול הזמן ש־95 מתוך 100 כניסות הסתיימו עד אליו; הוא אינו זמן ההמתנה של כל משתמש ואינו הממוצע.",
        "זמן הכניסה החדש כולל את כל פעולת ה־GET, לרבות הקמת חיבור כשנדרשת. המדידה מתחילה במחשב הבודק ב־GitHub.",
        "הבדיקה המקדימה אינה נכללת במוני הכניסות או בזמן הכניסה בדוח החדש. זמן הריצה הכללי עשוי לכלול הכנה וסיום בקשות.",
        "הבדיקה אינה טוענת תמונות או מריצה JavaScript, ואינה בודקת התחברות, כתיבת פוסטים, WebSocket או מהירות ציור המסך.",
        "אם שירות מטמון עונה לבקשה, התוצאה מתארת גם אותו. היא אינה מדידה ישירה של מסד הנתונים או של שרת המקור.",
        "מעבר הבדיקה מתאר רק את העומס שנבחר באותה ריצה. לדוגמה, הצלחה עם 5 משתמשים אינה מוכיחה תמיכה ב־1,000.",
        "הבדיקה עוצרת בהערכת הספים אם 10% מהכניסות או יותר נכשלות, לאחר השהיה של 30 שניות. אפשר לבטל גם דרך Cancel workflow.",
    ]
    if short_run:
        notes.insert(0, "⚠️ משך הריצה קצר מהמתוכנן; ייתכן שהבדיקה בוטלה או נעצרה בעקבות שגיאות.")
    if exit_code is not None:
        notes.insert(0, f"קוד סיום כלי המדידה: {exit_code}. 0 = הצלחה; 99 = חריגה מיעד/עצירה בעקבות יעד; קוד אחר מצריך עיון בלוגים.")
    if read_error:
        notes.insert(0, f"⚠️ לא ניתן לקרוא תוצאות תקינות: {read_error}")
    if is_legacy:
        notes.insert(0, "זהו דוח ישן: המונים מתארים בקשות HTTP לפי הגדרת k6. זמן HTTP הישן אינו כולל הקמת חיבור; סיבות השגיאה לא נשמרו בנפרד.")
    if total is not None and succeeded is not None and failed is not None and succeeded + failed != total:
        title = "🛑 מוני הכניסות אינם עקביים"
        explanation = "ייתכן שהריצה נעצרה באמצע עדכון המדדים. אין להסתמך על שיעור ההצלחה בדוח הזה."
        tone = "warning"
        passed = False
    return {"title": title, "tone": tone, "explanation": explanation, "target": metadata.get("target_url", "לא נשמרה כתובת בדוח"), "rows": rows, "errors": errors, "targets": targets, "notes": notes, "passed": passed}


def markdown_escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")


def render_markdown(report):
    lines = [f"## {report['title']}", "", report["explanation"], "", f"🎯 כתובת שנבדקה: {markdown_escape(report['target'])}", "", "| מדד | תוצאה |", "| --- | --- |"]
    lines.extend(f"| {markdown_escape(label)} | {markdown_escape(value)} |" for label, value in report["rows"])
    lines.extend(["", "### 🎯 האם היעדים הושגו?", "", "| יעד | תוצאה |", "| --- | --- |"])
    lines.extend(f"| {markdown_escape(label)} | {verdict} |" for label, verdict in report["targets"])
    lines.extend(["", "### 🔎 מה פירוש השגיאות?", "", "| סוג תקלה | מספר כניסות | פירוש |", "| --- | --- | --- |"])
    lines.extend(f"| {markdown_escape(label)} | {value} | {markdown_escape(meaning)} |" for label, value, meaning in report["errors"])
    lines.extend(["", "### 💡 איך לקרוא את הבדיקה", ""])
    lines.extend(f"- {markdown_escape(note)}" for note in report["notes"])
    return "\n".join(lines) + "\n"


def render_html(report):
    escape = lambda value: html.escape(str(value), quote=True)
    cards = "".join(f"<article class='card'><div class='label'>{escape(label)}</div><strong>{escape(value)}</strong></article>" for label, value in report["rows"])
    targets = "".join(f"<tr><td>{escape(label)}</td><td>{escape(verdict)}</td></tr>" for label, verdict in report["targets"])
    errors = "".join(f"<tr><td>{escape(label)}</td><td>{escape(value)}</td><td>{escape(meaning)}</td></tr>" for label, value, meaning in report["errors"])
    notes = "".join(f"<li>{escape(note)}</li>" for note in report["notes"])
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>דוח מהירות — פורום בני ברק</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fc;color:#17253b;font-family:Arial,'Segoe UI',sans-serif;line-height:1.65}}main{{max-width:1120px;margin:36px auto;padding:0 24px 36px}}header{{background:#fff;border:1px solid #dfe7f3;border-top:5px solid #2463d3;border-radius:16px;padding:28px;margin-bottom:20px}}h1{{font-size:26px;line-height:1.45;margin:0 0 12px}}h2{{font-size:20px;margin:0 0 14px}}p{{margin:10px 0}}.status{{border-radius:10px;padding:16px;margin-top:18px}}.success{{background:#e9f8ee;color:#185c35}}.warning{{background:#fff4dc;color:#6f4b09}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}}.card{{background:#fff;border:1px solid #dfe7f3;border-radius:12px;padding:20px}}.label{{color:#586980;font-size:14px;margin-bottom:10px}}strong{{font-size:18px;display:block}}section{{background:#fff;border:1px solid #dfe7f3;border-radius:14px;padding:24px;margin-top:20px}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:480px}}th,td{{text-align:right;border-bottom:1px solid #e4eaf3;padding:12px 8px;vertical-align:top}}th{{font-size:14px;color:#586980}}td:nth-child(2){{white-space:nowrap}}ul{{padding-right:22px;margin-bottom:0}}li{{margin-bottom:10px}}bdi{{unicode-bidi:isolate;color:#2463d3}}footer{{font-size:13px;color:#586980;margin-top:20px}}@media(max-width:760px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}main{{padding:0 14px;margin:18px auto}}header,section{{padding:20px}}h1{{font-size:22px}}}}@media(max-width:440px){{.grid{{grid-template-columns:1fr}}}}@media print{{body{{background:#fff}}main{{margin:0}}section,.card{{break-inside:avoid}}}}
</style></head><body><main><header><h1>📊 דוח מהירות — פורום בני ברק</h1><p>כתובת שנבדקה: <bdi dir="ltr">{escape(report['target'])}</bdi></p><div class="status {report['tone']}"><strong>{escape(report['title'])}</strong><p>{escape(report['explanation'])}</p></div></header>
<div class="grid">{cards}</div><section><h2>🎯 יעדי הבדיקה</h2><div class="scroll"><table><thead><tr><th>יעד</th><th>תוצאה</th></tr></thead><tbody>{targets}</tbody></table></div></section>
<section><h2>🔎 תקלות והמשמעות שלהן</h2><div class="scroll"><table><thead><tr><th>סוג תקלה</th><th>מספר כניסות</th><th>פירוש</th></tr></thead><tbody>{errors}</tbody></table></div></section>
<section><h2>💡 איך להבין את התוצאות</h2><ul>{notes}</ul></section><footer>הדוח פועל כקובץ מקומי, ללא אינטרנט וללא שירות חיצוני.</footer></main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description="📊 יצירת דוח בדיקת פורום בעברית")
    parser.add_argument("summary", type=Path)
    parser.add_argument("--exit-code-file", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--html", type=Path)
    args = parser.parse_args()
    read_error = None
    data = {}
    exit_code = None
    try:
        data = json.loads(args.summary.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
            raise ValueError("מבנה נתונים לא מוכר; נדרש קובץ summary.json של הבדיקה")
    except (OSError, ValueError, TypeError) as exc:
        read_error = str(exc)
        data = {}
    if args.exit_code_file:
        try:
            exit_code = int(args.exit_code_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            read_error = read_error or f"לא נשמר קוד סיום הבדיקה: {exc}"
    report = make_report(data, exit_code=exit_code, read_error=read_error)
    markdown = render_markdown(report)
    print(markdown, end="")
    if args.markdown:
        args.markdown.write_text(markdown, encoding="utf-8")
    if args.html:
        args.html.write_text(render_html(report), encoding="utf-8")
    return 1 if read_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
