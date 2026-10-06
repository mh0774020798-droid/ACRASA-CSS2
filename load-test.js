import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

// הבדיקה הרגילה מופעלת ידנית מ-GitHub Actions מול פורום בני ברק.
const BASE_URL = (__ENV.TARGET_URL || 'https://bnebrak.com').replace(/\/+$/, '');
const rawUsers = __ENV.TOTAL_VUS || '5';
const rawDuration = __ENV.TEST_DURATION || '1m';
if (!/^\d+$/.test(rawUsers) || Number(rawUsers) < 1 || Number(rawUsers) > 100) {
  throw new Error('👥 יש לבחור מספר שלם של משתמשים מדומים בין 1 ל־100. מומלץ להתחיל ב־5.');
}
const durationMatch = /^(\d+)(s|m)$/.exec(rawDuration);
const TEST_SECONDS = durationMatch ? Number(durationMatch[1]) * (durationMatch[2] === 'm' ? 60 : 1) : 0;
if (TEST_SECONDS < 10 || TEST_SECONDS > 300) {
  throw new Error('⏱️ משך הבדיקה חייב להיות בין 10 שניות ל־5 דקות, למשל 1m.');
}
// localhost משמש אך ורק לבדיקות הקוד האוטומטיות.
if (!/^https:\/\/bnebrak\.com$/.test(BASE_URL) && !/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE_URL)) {
  throw new Error('🎯 הגרסה הזו מותאמת לכתובת https://bnebrak.com.');
}
const TOTAL_VUS = Number(rawUsers);
const RAMP_SECONDS = Math.max(2, Math.floor(TEST_SECONDS * 0.2));
const HOLD_SECONDS = TEST_SECONDS - RAMP_SECONDS * 2;
const P95_TARGET_MS = 3000;
const ERROR_TARGET = 0.02;
const EXPECTED_TEXT = 'פורום בני ברק';

const visits = new Counter('visits');
const successfulVisits = new Counter('successful_visits');
const failedVisits = new Counter('failed_visits');
const errorRate = new Rate('errors');
const visitTime = new Trend('visit_duration', true);
const homepageTime = new Trend('homepage_duration', true);
const waitingTime = new Trend('waiting_duration', true);
const blocked403 = new Counter('blocked_403');
const limited429 = new Counter('limited_429');
const serverErrors = new Counter('server_errors');
const networkErrors = new Counter('network_errors');
const unexpectedResponses = new Counter('unexpected_responses');

export const options = {
  scenarios: {
    forum_visits: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: `${RAMP_SECONDS}s`, target: TOTAL_VUS },
        { duration: `${HOLD_SECONDS}s`, target: TOTAL_VUS },
        { duration: `${RAMP_SECONDS}s`, target: 0 },
      ],
      gracefulRampDown: '12s',
      gracefulStop: '12s',
    },
  },
  thresholds: {
    visit_duration: [`p(95)<${P95_TARGET_MS}`],
    errors: [
      `rate<${ERROR_TARGET}`,
      { threshold: 'rate<0.10', abortOnFail: true, delayAbortEval: '30s' },
    ],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  userAgent: 'BneBrak-Forum-Performance-Test/1.0 (k6)',
  // הפניות אינן נעקבות כדי לא להעביר עומס לדומיין אחר או למדוד מסך התחברות.
  maxRedirects: 0,
  setupTimeout: '15s',
};

function hasForumContent(response) {
  return response.status === 200 && typeof response.body === 'string' && response.body.includes(EXPECTED_TEXT);
}

function responseMeaning(response) {
  if (response.status === 0) return 'אין תגובת HTTP: ייתכן timeout, תקלה ברשת, DNS או TLS';
  if (response.status === 403) return '403: הגישה נחסמה; ייתכן כלל אבטחה או הרשאות';
  if (response.status === 429) return '429: האתר מגביל את קצב הבקשות';
  if (response.status >= 500) return `${response.status}: שגיאה בשרת או בשרת מתווך`;
  if (response.status >= 300 && response.status < 400) return `${response.status}: התקבלה הפניה במקום עמוד הפורום`;
  if (response.status === 200 && !hasForumContent(response)) return '200: התקבלה תשובה, אך תוכן הפורום לא זוהה';
  if (response.status === 200) return '200: עמוד הפורום התקבל';
  return `${response.status}: תגובה לא צפויה`;
}

export function setup() {
  console.log(`🎯 פורום לבדיקה: ${BASE_URL}/`);
  console.log(`👥 שיא מתוכנן: ${TOTAL_VUS} משתמשים מדומים במקביל`);
  console.log(`⏱️ משך מתוכנן: ${TEST_SECONDS} שניות, עם תוספת אפשרית לסיום בקשות פעילות`);
  console.log(`🪜 עלייה ${RAMP_SECONDS} שניות ← שיא ${HOLD_SECONDS} שניות ← ירידה ${RAMP_SECONDS} שניות`);
  console.log('🎯 יעדים: 95% מהכניסות בפחות מ־3 שניות; פחות מ־2% כניסות כושלות');
  console.log('🧭 בודק תחילה שהפורום זמין ושלא מתקבל דף חסימה...');
  const response = http.get(`${BASE_URL}/`, { timeout: '10s', tags: { name: 'preflight', phase: 'preflight' } });
  if (!hasForumContent(response)) {
    exec.test.abort(`🛑 הבדיקה לא התחילה: ${responseMeaning(response)}. פרטים נוספים בלוגים.`);
    return;
  }
  console.log('✅ עמוד הפורום זוהה. מתחיל את הבדיקה ההדרגתית.');
}

let lastProgressLog = -15000;
let errorSamples = 0;

export default function () {
  const startedAt = Date.now();
  const response = http.get(`${BASE_URL}/`, { timeout: '10s', tags: { name: 'homepage', phase: 'load' } });
  const elapsedMs = Date.now() - startedAt;
  const success = check(response, {
    '✅ עמוד הבית החזיר HTTP 200': (r) => r.status === 200,
    '📄 תוכן פורום בני ברק זוהה': hasForumContent,
  });

  // כניסה אחת יכולה להיכשל בשתי בדיקות; סופרים אותה פעם אחת בלבד.
  visits.add(1);
  successfulVisits.add(success ? 1 : 0);
  failedVisits.add(success ? 0 : 1);
  errorRate.add(!success);
  visitTime.add(elapsedMs);
  homepageTime.add(response.timings.duration);
  waitingTime.add(response.timings.waiting);
  blocked403.add(!success && response.status === 403 ? 1 : 0);
  limited429.add(!success && response.status === 429 ? 1 : 0);
  serverErrors.add(!success && response.status >= 500 ? 1 : 0);
  networkErrors.add(!success && response.status === 0 ? 1 : 0);
  unexpectedResponses.add(!success && response.status !== 0 && response.status !== 403 && response.status !== 429 && response.status < 500 ? 1 : 0);

  // משתמש אחד מדפיס דוגמאות בלבד; הסיכום כולל את כל המשתמשים והבקשות.
  if (__VU === 1) {
    const elapsed = exec.instance.currentTestRunDuration;
    if (elapsed - lastProgressLog >= 15000) {
      lastProgressLog = elapsed;
      const progress = exec.scenario.progress;
      const stage = progress < 0.2 ? '🟦 עלייה בעומס' : progress < 0.8 ? '🟩 עומס השיא' : '🟨 ירידה בעומס';
      console.log(`📈 ${stage} | התקדמות ${Math.round(progress * 100)}% | פעילים כרגע ${exec.instance.vusActive} | דוגמת כניסה: ${Math.round(elapsedMs)} מילישניות, HTTP ${response.status}`);
    }
    if (!success && errorSamples < 3) {
      errorSamples += 1;
      console.warn(`⚠️ דוגמת תקלה ${errorSamples}/3: ${responseMeaning(response)}`);
    }
  }
  sleep(1 + Math.random() * 2);
}

function values(data, metric) {
  return data.metrics && data.metrics[metric] ? data.metrics[metric].values || {} : {};
}

function milliseconds(value) {
  return Number.isFinite(value) ? `${Math.round(value)} מילישניות (${(value / 1000).toFixed(2)} שניות)` : 'לא נמדד';
}

export function handleSummary(data) {
  const total = values(data, 'visits').count || 0;
  const failed = values(data, 'failed_visits').count || 0;
  const times = values(data, 'visit_duration');
  const thresholds = [];
  for (const metric of Object.values(data.metrics || {})) {
    thresholds.push(...Object.values(metric.thresholds || {}));
  }
  const passed = total > 0 && Number.isFinite(times['p(95)']) && thresholds.length > 0 && thresholds.every((item) => item.ok === true);
  const output = [
    '',
    total === 0 ? '🛑 אין תוצאות עומס: לא נמדדו כניסות. יש לבדוק את הלוגים.' : passed ? '✅ הבדיקה עמדה ביעדים שהוגדרו לעומס הזה.' : '⚠️ הבדיקה לא עמדה בכל היעדים; ייתכן שנעצרה מוקדם.',
    `📨 כניסות שנבדקו: ${total}`,
    `✅ כניסות שהצליחו: ${values(data, 'successful_visits').count || 0}`,
    `❌ כניסות שנכשלו: ${failed}${total ? ` (${(failed / total * 100).toFixed(2)}%)` : ''}`,
    `⚡ זמן כניסה ממוצע: ${milliseconds(times.avg)}`,
    `🐢 95% מהכניסות הסתיימו בתוך: ${milliseconds(times['p(95)'])}`,
    `🚫 חסימות 403: ${values(data, 'blocked_403').count || 0}`,
    `🚦 הגבלת קצב 429: ${values(data, 'limited_429').count || 0}`,
    `🖥️ שגיאות שרת 5xx: ${values(data, 'server_errors').count || 0}`,
    `🔌 תקלות חיבור או timeout: ${values(data, 'network_errors').count || 0}`,
    '📄 דוח מפורט בעברית יופיע בסיכום הריצה ובקובץ report.html.',
    'ℹ️ המדידה היא של בקשת עמוד הבית; תמונות, JavaScript, התחברות ופוסטים אינם נבדקים.',
    '',
  ].join('\n');
  const summary = {
    ...data,
    forum_test: {
      schema_version: 1,
      target_url: `${BASE_URL}/`,
      planned_vus: TOTAL_VUS,
      planned_duration_seconds: TEST_SECONDS,
      p95_target_ms: P95_TARGET_MS,
      error_target_rate: ERROR_TARGET,
      expected_text: EXPECTED_TEXT,
    },
  };
  return { stdout: output, 'summary.json': JSON.stringify(summary, null, 2) };
}
