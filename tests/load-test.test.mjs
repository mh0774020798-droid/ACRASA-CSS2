import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(project, 'load-test.js'), 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace('export default function ()', 'function iterate()')
  .replace(/^export /gm, '');

function load(env = {}, response = {}) {
  const measurements = {};
  const messages = [];
  let requestCount = 0;
  class Metric {
    constructor(name) { this.name = name; measurements[name] = []; }
    add(value) { measurements[this.name].push(value); }
  }
  const context = vm.createContext({
    __ENV: env, __VU: 1, Counter: Metric, Rate: Metric, Trend: Metric,
    http: { get() { requestCount++; return { status: 200, body: '<title>פורום בני ברק</title>', timings: { duration: 120, waiting: 100 }, ...response }; } },
    check(result, checks) { return Object.values(checks).every((check) => check(result)); },
    sleep() {},
    exec: { test: { abort(reason) { throw new Error(reason); } }, instance: { currentTestRunDuration: 1000, vusActive: 1 }, scenario: { progress: 0.05 } },
    console: { log(message) { messages.push(message); }, warn(message) { messages.push(message); } },
  });
  new vm.Script(source + '\nglobalThis.api = { options, setup, iterate, handleSummary };').runInContext(context);
  return { api: context.api, measurements, messages, requests: () => requestCount };
}

test('ברירת מחדל: 5 משתמשים, דקה אחת, בלי חלוקה לרצים מקבילים', () => {
  const { api } = load();
  const scenario = api.options.scenarios.forum_visits;
  assert.equal(scenario.startVUs, 1);
  assert.deepEqual(Array.from(scenario.stages, (stage) => [stage.duration, stage.target]), [['12s', 5], ['36s', 5], ['12s', 0]]);
  assert.equal(api.options.maxRedirects, 0);
});

test('קלט שגוי נעצר לפני פנייה לאתר', () => {
  for (const total of ['0', '-1', '101', '5abc', '1.5', '$(id)']) assert.throws(() => load({ TOTAL_VUS: total }), /👥/);
  for (const duration of ['0s', '9s', '6m', 'one minute']) assert.throws(() => load({ TEST_DURATION: duration }), /⏱️/);
  assert.throws(() => load({ TARGET_URL: 'https://example.com' }), /🎯/);
});

test('בדיקת הזמינות אינה נספרת ככניסת עומס', () => {
  const fixture = load();
  fixture.api.setup();
  assert.equal(fixture.requests(), 1);
  assert.equal(fixture.measurements.visits.length, 0);
});

test('403 ועמוד חסימה עם קוד 200 עוצרים את הבדיקה המקדימה', () => {
  assert.throws(() => load({}, { status: 403 }).api.setup(), /403/);
  assert.throws(() => load({}, { body: 'Just a moment...' }).api.setup(), /תוכן הפורום לא זוהה/);
});

test('כל כניסה נכשלת נספרת פעם אחת, עם סיבה אחת', () => {
  const cases = [
    [{ status: 0, body: '' }, 'network_errors'],
    [{ status: 403, body: '' }, 'blocked_403'],
    [{ status: 429, body: '' }, 'limited_429'],
    [{ status: 503, body: '' }, 'server_errors'],
    [{ status: 302, body: '' }, 'unexpected_responses'],
    [{ status: 200, body: 'Please sign in' }, 'unexpected_responses'],
  ];
  for (const [response, expected] of cases) {
    const { api, measurements } = load({}, response);
    api.iterate();
    assert.deepEqual(measurements.visits, [1]);
    assert.deepEqual(measurements.failed_visits, [1]);
    assert.deepEqual(measurements.successful_visits, [0]);
    assert.deepEqual(measurements.errors, [true]);
    const categories = ['network_errors', 'blocked_403', 'limited_429', 'server_errors', 'unexpected_responses'];
    assert.equal(categories.reduce((sum, name) => sum + measurements[name][0], 0), 1);
    assert.equal(measurements[expected][0], 1);
  }
});

test('כניסה תקינה, לוגים מדגמיים ומניעת הצפה', () => {
  const { api, measurements, messages } = load();
  for (let n = 0; n < 10; n++) api.iterate();
  assert.equal(measurements.successful_visits.reduce((a, b) => a + b, 0), 10);
  assert.equal(messages.filter((message) => message.includes('דוגמת כניסה')).length, 1);
});

test('handleSummary שומר JSON קריא ומטא-נתונים בלי להדפיס JSON בלוג', () => {
  const { api } = load();
  const data = { metrics: {
    visits: { values: { count: 100 } },
    successful_visits: { values: { count: 99 } },
    failed_visits: { values: { count: 1 } },
    visit_duration: { values: { avg: 500, 'p(95)': 800 }, thresholds: { 'p(95)<3000': { ok: true } } },
    errors: { values: { rate: 0.01 }, thresholds: { 'rate<0.02': { ok: true } } },
  } };
  const result = api.handleSummary(data);
  assert.match(result.stdout, /✅ הבדיקה עמדה/);
  assert.match(result.stdout, /1 \(1.00%\)/);
  assert.match(result.stdout, /800 מילישניות/);
  const saved = JSON.parse(result['summary.json']);
  assert.equal(saved.forum_test.target_url, 'https://bnebrak.com/');
  assert.equal(saved.forum_test.planned_vus, 5);
  assert.match(api.handleSummary({ metrics: {} }).stdout, /אין תוצאות עומס/);
});
