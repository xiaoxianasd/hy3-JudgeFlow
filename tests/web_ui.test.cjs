// Optional browser regression tests: node --test tests/web_ui.test.cjs
// Requires Playwright; JUDGEFLOW_BROWSER_CHANNEL=msedge uses installed Edge.
// All browser requests are intercepted. No Hy3, database, or live queue is used.
'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync, mkdirSync, existsSync } = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const assets = path.join(root, 'hy3_tracejudge', 'web_assets');
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.JUDGEFLOW_TEST_PYTHON || (existsSync(localPython) ? localPython : 'python');
const catalog = JSON.parse(execFileSync(python, ['-c', [
  'import json',
  'from fastapi.testclient import TestClient',
  'from hy3_tracejudge.api.app import create_app',
  'from hy3_tracejudge.api.config import WebConfig',
  'client = TestClient(create_app(WebConfig(app_env="test")))',
  'print(json.dumps(client.get("/api/v1/problems").json()))',
].join('; ')], { cwd: root, encoding: 'utf8', windowsHide: true }));

let browser;
before(async () => {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.JUDGEFLOW_BROWSER_CHANNEL ? { channel: process.env.JUDGEFLOW_BROWSER_CHANNEL } : {}),
  });
});
after(async () => { await browser?.close(); });

async function openPage(t, { problems = catalog, jobHandler, healthHandler, submissionEnabled = true, viewport = { width: 1440, height: 1080 }, pagePath = '/' } = {}) {
  const context = await browser.newContext({ viewport });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, [], 'no browser JavaScript errors'));
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://judgeflow.test') return route.abort();
    const files = {
      '/': ['index.html', 'text/html; charset=utf-8'],
      '/submit': ['submission.html', 'text/html; charset=utf-8'],
      '/assets/styles.css': ['styles.css', 'text/css'],
      '/assets/app.js': ['app.js', 'text/javascript'],
    };
    if (files[url.pathname]) {
      const [file, contentType] = files[url.pathname];
      return route.fulfill({ contentType, body: readFileSync(path.join(assets, file)) });
    }
    if (url.pathname === '/api/v1/config') return route.fulfill({ json: { authentication_required: false, model_runtime: { models: ['hy3', 'hy4-preview'], default_model: 'hy3', per_job_credentials: true }, code_submission: { enabled: submissionEnabled, max_request_bytes: 65536 } } });
    if (url.pathname === '/api/v1/problems') return route.fulfill({ json: problems });
    if (url.pathname === '/api/v1/health/hy3') {
      return route.fulfill({ json: { ok: true, requested_model: 'hy3 (UI test)', latency_ms: 0 } });
    }
    if (url.pathname === '/api/v1/health/model') {
      if (healthHandler) return healthHandler(route);
      const model = route.request().postDataJSON().model;
      return route.fulfill({ json: { ok: true, requested_model: model, latency_ms: 0, probe: 'inference' } });
    }
    if ((url.pathname.startsWith('/api/v1/evaluations') || url.pathname === '/api/v1/code-submissions') && jobHandler) return jobHandler(route);
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`http://judgeflow.test${pagePath}`);
  await page.locator('#problemContent').waitFor({ state: 'visible' });
  await page.click('#checkModel');
  await page.locator('#health').getByText(/已通过真实推理检查/).waitFor();
  return page;
}

test('model selector checks hy4-preview with a session-scoped provider key', async t => {
  const requests = [];
  const page = await openPage(t, { healthHandler: route => {
    const payload = route.request().postDataJSON();
    requests.push(payload);
    return route.fulfill({ json: { ok: true, requested_model: payload.model, latency_ms: 1, probe: 'inference' } });
  } });
  await page.selectOption('#modelName', 'hy4-preview');
  assert.equal(await page.locator('#reviewMode').inputValue(), 'single');
  await page.fill('#modelApiKey', 'browser-provider-key');
  const checked = page.waitForResponse(response => response.url().endsWith('/api/v1/health/model'));
  await page.click('#checkModel');
  await checked;
  assert.deepEqual(requests.at(-1), { model: 'hy4-preview', provider_api_key: 'browser-provider-key' });
  assert.match(await page.locator('#health').textContent(), /hy4-preview 已通过真实推理检查/);
  assert.equal(await page.evaluate(() => localStorage.getItem('tracejudge_model_api_key')), null);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('tracejudge_model_api_key')), 'browser-provider-key');
});

test('professional heading and complete public problem details', async t => {
  const page = await openPage(t);
  assert.equal(await page.locator('h1').textContent(), 'Hy3 推理评估');
  assert.equal(await page.locator('.journey li').count(), 3);
  assert.equal(await page.locator('#submissionPanel').count(), 0);
  assert.equal(await page.locator('.advanced-settings').getAttribute('open'), null);
  assert.match(await page.title(), /Hy3 JudgeFlow/);
  assert.equal(await page.locator('#statement').textContent(), catalog[0].source_statement);
  assert.match(await page.locator('#inputSchema').textContent(), /nums.*list\[int\].*target.*int/s);
  assert.match(await page.locator('#constraints').textContent(), /200000/);
  assert.equal(await page.locator('.public-example').count(), catalog[0].public_tests.length);
  assert.match(await page.locator('.public-example').nth(1).textContent(), /false/);
  assert.equal(await page.locator('#problemSource').textContent(), catalog[0].source);
  assert.equal(await page.locator('#result #problemDetails').count(), 0);
  assert.equal(await page.locator('#statement').evaluate(el => getComputedStyle(el).maxHeight), 'none');
  mkdirSync(path.join(root, 'tmp'), { recursive: true });
  await page.screenshot({ path: path.join(root, 'tmp', 'judgeflow-ui-desktop.png'), fullPage: true });
});

test('all catalog problems can be selected with their full descriptions', async t => {
  const page = await openPage(t);
  for (const problem of catalog) {
    await page.selectOption('#problem', problem.id);
    assert.equal(await page.locator('#problemTitle').textContent(), problem.title);
    const expected = (problem.source_statement || problem.statement).trim().replace(/^("""|''')\r?\n([\s\S]*)\r?\n\1$/, '$2').trim();
    assert.equal(await page.locator('#statement').textContent(), expected, problem.id);
    assert.equal(await page.locator('.public-example').count(), problem.public_tests.length, problem.id);
    assert.equal(await page.locator('#adapterNote').isVisible(), problem.tier === 'external');
  }
});

test('search, difficulty filters, and empty states stay synchronized', async t => {
  const page = await openPage(t);
  await page.selectOption('#difficulty', 'hard');
  const hard = catalog.filter(item => item.difficulty === 'hard');
  assert.equal(await page.locator('#problem option').count(), hard.length);
  assert.equal(await page.locator('#problemTitle').textContent(), hard[0].title);
  await page.fill('#problemSearch', 'no-matching-problem-xyz');
  assert.equal(await page.locator('#problemContent').isVisible(), false);
  assert.equal(await page.locator('#problemTitle').textContent(), '未选择题目');
  assert.equal(await page.locator('#run').isDisabled(), true);
  await page.fill('#problemSearch', '');
  await page.selectOption('#difficulty', 'all');
  await page.fill('#problemSearch', 'mbpp_Mbpp/8');
  assert.match(await page.locator('#problemId').textContent(), /MBPP 8/);
  assert.equal(await page.locator('#run').isEnabled(), true);
});

test('literal markup in problem data is rendered safely', async t => {
  const literal = '<img src=x onerror="alert(1)">';
  const page = await openPage(t, { problems: [{
    ...catalog[0], title: literal, source_statement: literal, source: literal,
    input_schema: { [literal]: literal }, constraints: [literal],
    public_tests: [{ name: 'unsafe', input: literal, expected: literal }],
  }] });
  assert.equal(await page.locator('#problemTitle').textContent(), literal);
  assert.equal(await page.locator('#statement').textContent(), literal);
  assert.match(await page.locator('#inputSchema').textContent(), /<img/);
  assert.match(await page.locator('#publicExamples').textContent(), /<img/);
  assert.equal(await page.locator('#problemDetails img').count(), 0);
});

test('missing optional detail fields have honest empty states', async t => {
  const page = await openPage(t, { problems: [{
    ...catalog[0], source_statement: '', statement: '', input_schema: {}, constraints: [], public_tests: [],
  }] });
  assert.match(await page.locator('#statement').textContent(), /暂未提供/);
  assert.match(await page.locator('#inputSchema').textContent(), /未提供/);
  assert.match(await page.locator('#constraints').textContent(), /未提供/);
  assert.match(await page.locator('#publicExamples').textContent(), /暂无公开示例/);
});

test('problem stays visible and locked while evaluating; switching clears stale results', async t => {
  let submitted;
  let releaseResult;
  const resultReady = new Promise(resolve => { releaseResult = resolve; });
  const job = { id: 'a'.repeat(32), phase: 'completed', status: 'succeeded', result: {
    answer: { reasoning_steps: [], code: 'def solve_case(case): return True' },
    evaluation: { final_correct: true, process_correct: true, first_error_step: null,
      hypothesis: null, execution: { passed: 2, total: 2 }, error_labels: [] },
  } };
  const page = await openPage(t, { jobHandler: async route => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON();
      return route.fulfill({ status: 202, json: { job: { id: job.id }, status_url: `/api/v1/evaluations/${job.id}` } });
    }
    await resultReady;
    return route.fulfill({ json: { job } });
  } });
  await page.selectOption('#problem', 'mbpp_Mbpp/8');
  const statement = await page.locator('#statement').textContent();
  const submission = page.waitForResponse(response => response.request().method() === 'POST');
  await page.click('#run');
  await submission;
  assert.equal(submitted.problem_id, 'mbpp_Mbpp/8');
  for (const id of ['problem', 'problemSearch', 'difficulty', 'examples', 'reviewMode', 'run']) {
    assert.equal(await page.locator(`#${id}`).isDisabled(), true, id);
  }
  assert.equal(await page.locator('#statement').isVisible(), true);
  assert.equal(await page.locator('#statement').textContent(), statement);
  releaseResult();
  await page.locator('#result .metrics').waitFor();
  assert.equal(await page.locator('#result .code-evidence').isVisible(), true);
  assert.equal(await page.locator('#result .reasoning-evidence').isVisible(), true);
  assert.equal(await page.locator('#result .result-details').first().getAttribute('open'), null);
  assert.equal(await page.locator('#statement').textContent(), statement);
  assert.equal(await page.locator('#problem').isEnabled(), true);
  await page.selectOption('#problem', catalog[0].id);
  assert.equal(await page.locator('#result .metrics').count(), 0);
  assert.match(await page.locator('#result').textContent(), /等待评估结果/);
});

test('first-error counterexample is shown below the verdict and before collapsed details', async t => {
  const job = { id: 'e'.repeat(32), phase: 'completed', status: 'succeeded', result: {
    answer: { reasoning_steps: [
      {id: 5, title: '边界', content: '只要 coins 中包含 1，结果就等于 amount。'},
    ], code: 'def solve_case(case): return 0' },
    evaluation: {
      final_correct: true, process_correct: false, first_error_step: 5,
      assessment_note: '当前最早定位的错误证据出现在步骤 5。',
      hypothesis: {enabled: true, status: 'passed', found: false, examples_checked: 60, max_examples: 60},
      execution: {passed: 6, total: 6}, error_types: ['concept_error'], error_labels: ['概念理解错误'],
      reasoning_evidence: [{
        step: 5, source: 'boundary_agent', confidence: 0.95,
        reason: 'coins=[1,5]、amount=10 时最优答案是 2，不是 10。',
        evidence: ['步骤 5 将可达性错误地当成了最优硬币数。'],
      }],
    },
  } };
  const page = await openPage(t, {jobHandler: route => route.request().method() === 'POST'
    ? route.fulfill({status: 202, json: {job: {id: job.id}, status_url: `/api/v1/evaluations/${job.id}`}})
    : route.fulfill({json: {job}})});
  await page.selectOption('#problem', 'coin_change');
  await page.click('#run');
  await page.locator('#result .reasoning-finding').waitFor();
  assert.match(await page.locator('#result .code-evidence').textContent(), /固定测试 6\/6.*实际检查 60 次/s);
  assert.match(await page.locator('#result .reasoning-evidence').textContent(), /步骤 5.*coins=\[1,5\].*最优答案是 2/s);
  const positions = await page.locator('#result').evaluate(result => ({
    verdict: result.querySelector('.assessment-note').getBoundingClientRect().bottom,
    reasoning: result.querySelector('.reasoning-evidence').getBoundingClientRect().top,
    details: result.querySelector('.result-details').getBoundingClientRect().top,
  }));
  assert.ok(positions.verdict <= positions.reasoning);
  assert.ok(positions.reasoning < positions.details);
  await page.screenshot({path: path.join(root, 'tmp', 'judgeflow-reasoning-evidence.png'), fullPage: true});
});

test('failed submissions unlock selectors without removing the question', async t => {
  const page = await openPage(t, { jobHandler: route => route.fulfill({
    status: 503, json: { error: { code: 'queue_full', message: '队列已满' } },
  }) });
  await page.click('#run');
  await page.locator('#result .error').waitFor();
  assert.equal(await page.locator('#problem').isEnabled(), true);
  assert.equal(await page.locator('#statement').textContent(), catalog[0].source_statement);
});

test('narrow layouts keep question details readable without horizontal page overflow', async t => {
  const page = await openPage(t, { viewport: { width: 390, height: 844 } });
  await page.selectOption('#problem', 'mbpp_Mbpp/8');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  assert.equal(await page.locator('#statement').evaluate(el => getComputedStyle(el).maxHeight), 'none');
  assert.equal(await page.locator('.example-values').first().evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), 1);
  mkdirSync(path.join(root, 'tmp'), { recursive: true });
  await page.screenshot({ path: path.join(root, 'tmp', 'judgeflow-ui-mobile.png'), fullPage: true });
});

test('manual workspace is separate and code drafts are bound to their problem', async t => {
  const page = await openPage(t);
  await page.click('#submissionMode');
  await page.locator('#submissionPanel').waitFor();
  assert.equal(new URL(page.url()).pathname, '/submit');
  assert.equal(await page.locator('#submissionPanel').isVisible(), true);
  assert.equal(await page.locator('#generationOptions').count(), 0);
  assert.equal(await page.locator('#run').count(), 0);
  assert.equal(await page.locator('#problemDetails').isVisible(), true);
  await page.fill('#userCode', 'def solve_case(case):\n    return True');
  await page.fill('#userSteps', '我的步骤');
  await page.selectOption('#problem', 'mbpp_Mbpp/8');
  assert.equal(await page.locator('#userCode').inputValue(), '');
  await page.selectOption('#problem', catalog[0].id);
  assert.match(await page.locator('#userCode').inputValue(), /return True/);
  assert.equal(await page.locator('#userSteps').inputValue(), '我的步骤');
  await page.click('#generateMode');
  await page.locator('#run').waitFor();
  assert.equal(new URL(page.url()).pathname, '/');
  assert.equal(await page.locator('#submissionPanel').count(), 0);
});

test('manual submission validates empty code and step limits before making a request', async t => {
  const page = await openPage(t, { pagePath: '/submit' });
  await page.click('#submitCode');
  assert.match(await page.locator('#submissionValidation').textContent(), /填写 Python 代码/);
  await page.fill('#userCode', 'pass');
  await page.fill('#userSteps', Array(21).fill('步骤').join('\n'));
  await page.click('#submitCode');
  assert.match(await page.locator('#submissionValidation').textContent(), /最多 20 步/);
  await page.fill('#userSteps', 'x'.repeat(2001));
  await page.click('#submitCode');
  assert.match(await page.locator('#submissionValidation').textContent(), /2,000/);
});

test('manual workspace fails closed when Docker is not configured', async t => {
  const page = await openPage(t, { submissionEnabled: false, pagePath: '/submit' });
  assert.equal(await page.locator('#submissionSafety').isVisible(), true);
  assert.equal(await page.locator('#submitCode').isDisabled(), true);
  await page.click('#generateMode');
  await page.locator('#run').waitFor();
  assert.equal(await page.locator('#run').isEnabled(), true);
});

test('submitted code reaches its own API and reports code lines separately from reasoning', async t => {
  let payload;
  let release;
  const released = new Promise(resolve => { release = resolve; });
  const code = 'def solve_case(case):\n    return True # <img src=x onerror="alert(1)">';
  const job = {id: 'b'.repeat(32), status: 'succeeded', phase: 'completed', result: {
    source: 'user_submission', problem_id: catalog[0].id,
    answer: {code, reasoning_steps: []},
    evaluation: {final_correct: false, code_correct: false, process_correct: null, process_status: 'not_provided',
      first_error_line: 2, first_error_step: null, assessment_note: '未提交推理步骤，只审查代码。',
      submission_review: {confidence: 0.9, reason: '无条件返回 True', findings: [{scope: 'code', line: 2, step: null, reason: '忽略输入条件', suggestion: '比较数组中的两个不同下标'}]},
      execution: {passed: 1, total: 6, tests: [], harness_error: null}, hypothesis: {enabled: false}, error_labels: ['实现逻辑错误']},
  }};
  const page = await openPage(t, {pagePath: '/submit', jobHandler: async route => {
    if (route.request().method() === 'POST') {
      assert.equal(new URL(route.request().url()).pathname, '/api/v1/code-submissions');
      payload = route.request().postDataJSON();
      return route.fulfill({status: 202, json: {job: {id: job.id}, status_url: `/api/v1/evaluations/${job.id}`}});
    }
    await released;
    return route.fulfill({json: {job}});
  }});
  await page.fill('#userCode', code);
  const response = page.waitForResponse(res => res.request().method() === 'POST');
  await page.click('#submitCode');
  await response;
  assert.equal(payload.code, code);
  assert.deepEqual(payload.reasoning_steps, []);
  assert.equal(payload.problem_id, catalog[0].id);
  assert.equal(payload.review_mode, undefined);
  for (const id of ['userCode', 'userSteps', 'submitCode', 'problem']) {
    assert.equal(await page.locator(`#${id}`).isDisabled(), true, id);
  }
  release();
  await page.locator('#result .submission-metrics').waitFor();
  assert.match(await page.locator('.submission-metrics .metric').nth(2).textContent(), /未提交步骤/);
  assert.match(await page.locator('.submission-metrics .metric').nth(3).textContent(), /代码行 2/);
  assert.equal(await page.locator('.code-line.flagged').count(), 1);
  assert.equal(await page.locator('#result img').count(), 0);
  assert.match(await page.locator('#result').textContent(), /尚未配置属性测试策略/);
  assert.equal(await page.locator('#userCode').isEnabled(), true);
  await page.screenshot({path: path.join(root, 'tmp', 'judgeflow-submission-desktop.png'), fullPage: true});
});

test('manual mode sends only the user supplied steps and recovers from API failure', async t => {
  let payload;
  const page = await openPage(t, {pagePath: '/submit', jobHandler: route => {
    payload = route.request().postDataJSON();
    return route.fulfill({status: 503, json: {error: {message: 'Docker 沙盒不可用'}}});
  }});
  await page.fill('#userCode', 'def solve_case(case):\n    return True');
  await page.fill('#userSteps', '  步骤一\n\n步骤二  ');
  await page.click('#submitCode');
  await page.locator('#result .error').waitFor();
  assert.deepEqual(payload.reasoning_steps, ['步骤一', '步骤二']);
  assert.equal(await page.locator('#submitCode').isEnabled(), true);
  assert.equal(await page.locator('#problem').isEnabled(), true);
});

test('manual code editor does not overflow the mobile viewport', async t => {
  const page = await openPage(t, { viewport: { width: 390, height: 844 }, pagePath: '/submit' });
  await page.fill('#userCode', 'def solve_case(case):\n    # ' + 'long-code-line'.repeat(50));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({path: path.join(root, 'tmp', 'judgeflow-submission-mobile.png'), fullPage: true});
});
