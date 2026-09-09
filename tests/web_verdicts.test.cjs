// Dependency-free renderer regression tests: node --test tests/web_verdicts.test.cjs
// Execute the shipped JS with a small DOM stub; no browser, server or model calls.
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function app() {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {value: 'single', innerHTML: '', textContent: '', addEventListener() {}});
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => []},
    sessionStorage: {getItem: () => ''}, Headers, TextEncoder,
    // init() is deliberately held before API/DOM setup; tests call render directly.
    fetch: () => new Promise(() => {}),
  });
  vm.runInContext(readFileSync(path.join(__dirname, '../hy3_tracejudge/web_assets/app.js'), 'utf8'), context);
  return {context, html: () => element('#result').innerHTML};
}

function sample(overrides = {}) {
  return {answer: {code: 'def solve_case(case): return True', reasoning_steps: []}, evaluation: {
    final_correct: true, process_correct: null, first_error_step: null,
    execution: {passed: 10, total: 10, tests: []},
    hypothesis: {enabled: false, status: 'unsupported', found: false, examples_checked: 0, max_examples: 500},
    error_labels: [], assessment_note: '评审证据不足',
    review_coverage: {expected: 5, completed: 4, conclusive: 4}, ...overrides,
  }};
}

test('generation renders strict three-state verdicts without truthiness conversion', () => {
  const {context, html} = app();
  for (const [value, expected] of [[true, '成立'], [false, '不成立'], [null, '证据不足'], ['false', '证据不足']]) {
    context.render(sample({process_correct: value}));
    assert.equal(html().match(/<span>推理过程<\/span><b>([^<]+)<\/b>/)[1], expected);
  }
});

test('disabled Hypothesis is not described as no counterexample found', () => {
  const {context, html} = app();
  context.render(sample());
  assert.match(html(), /尚未配置属性测试策略/);
  assert.match(html(), /实际检查 0 次/);
  assert.match(html(), /生成预算 500/);
  assert.doesNotMatch(html(), /未发现反例/);
  assert.match(html(), /评审完成 4\/5/);
  assert.match(html(), /结论明确 4\/5/);
});

test('property rendering distinguishes bounded pass, failure, error and no run', () => {
  const {context} = app();
  assert.equal(context.propertyCheckText(null), '未执行');
  assert.match(context.propertyCheckText({enabled: false}), /尚未配置/);
  assert.match(context.propertyCheckText({enabled: true, status: 'error', error: 'failed'}), /结果不可用/);
  assert.match(context.propertyCheckText({enabled: true, status: 'failed', found: true, examples_checked: 75, max_examples: 60}), /发现反例.*实际检查 75 次.*生成预算 60/);
  assert.match(context.propertyCheckText({enabled: true, status: 'passed', found: false, examples_checked: 60}), /当前预算内未发现反例/);
  assert.match(context.propertyCheckText({enabled: true, found: false, examples_checked: 0}), /未执行/);
});

test('confirmed but unlocalized issue stays invalid without an invented step', () => {
  const {context, html} = app();
  context.render(sample({process_correct: false, localization_status: 'unlocalized', error_labels: ['算法逻辑错误']}));
  assert.match(html(), /尚未定位/);
  assert.doesNotMatch(html(), /步骤 null|步骤 undefined|步骤 1/);
});

test('code validation and reasoning counterevidence are separate and the first-error example is prominent', () => {
  const {context, html} = app();
  context.render(sample({
    process_correct: false,
    first_error_step: 5,
    error_types: ['concept_error'],
    error_labels: ['概念理解错误'],
    reasoning_evidence: [{
      step: 5,
      source: 'boundary_agent',
      stage: 'boundary',
      confidence: 0.95,
      reason: 'coins=[1,5]、amount=10 时最优答案是 2，不是 10。',
      evidence: ['步骤 5 错把“含面额 1”解释为最优硬币数等于 amount。'],
    }],
  }));
  assert.match(html(), /代码验证证据/);
  assert.match(html(), /推理反例证据/);
  assert.match(html(), /步骤 5/);
  assert.match(html(), /coins=\[1,5\].*最优答案是 2/);
  assert.ok(html().indexOf('推理反例证据') < html().indexOf('查看完整推理过程'));
});

test('saved supervisor jobs recover the selected first-error evidence without a rerun', () => {
  const {context, html} = app();
  context.render(sample({
    process_correct: false,
    first_error_step: 5,
    orchestration: {
      topology: 'pipeline', model_calls: 5, conflicts: [],
      decision: {supporting_sources: ['boundary_agent']},
      specialist_reviews: [{
        agent: 'boundary_agent', stage: 'boundary', status: 'completed', assessment_status: 'invalid',
        first_error_step: 5, confidence: 0.95, reason: '旧任务中的具体反例', evidence: ['输入 A 得到 B'],
      }],
    },
  }));
  assert.match(html(), /旧任务中的具体反例/);
  assert.match(html(), /输入 A 得到 B/);
});

test('failed and inconclusive reviewers are not displayed as passing', () => {
  const {context, html} = app();
  context.render(sample({orchestration: {topology: 'pipeline', model_calls: 1, conflicts: [], specialist_reviews: [
    {agent: 'failed', stage: 'proof', status: 'error', valid: null, assessment_status: 'uncertain'},
    {agent: 'weak', stage: 'boundary', status: 'completed', valid: true, assessment_status: 'uncertain'},
  ]}}));
  assert.match(html(), /调用失败 · 证据不足/);
  assert.doesNotMatch(html(), /通过审查/);
});

test('shape errors with no execution or valid step array remain renderable', () => {
  const {context, html} = app();
  const value = sample({final_correct: null, process_correct: false, execution: null, hypothesis: null});
  value.answer.reasoning_steps = 'invalid array';
  context.render(value);
  assert.match(html(), /固定测试 未执行/);
  assert.match(html(), /尚未定位/);
});

test('notes, errors and model text remain escaped', () => {
  const {context, html} = app();
  context.render(sample({
    process_correct: false,
    assessment_note: '<img src=x onerror=alert(1)>',
    error_labels: ['<script>bad</script>'],
    reasoning_evidence: [{step: 2, source: '<svg onload=bad()>', reason: '<iframe>bad</iframe>', evidence: ['<object>bad</object>']}],
  }));
  assert.doesNotMatch(html(), /<img|<script/);
  assert.match(html(), /&lt;img/);
  assert.doesNotMatch(html(), /<svg|<iframe|<object/);
  assert.match(html(), /&lt;iframe/);
});

test('failure ownership is explicit and distinguishes provisional from adjudicated', () => {
  const {context, html} = app();
  context.render(sample({failure_owner: 'infrastructure', failure_owner_status: 'provisional'}));
  assert.match(html(), /失败归属：基础设施（自动暂定，待人工复核）/);
  context.render(sample({failure_owner: 'evaluator', failure_owner_status: 'adjudicated'}));
  assert.match(html(), /失败归属：评估器（人工裁决）/);
  context.render(sample({failure_owner: '<script>', failure_owner_status: 'provisional'}));
  assert.doesNotMatch(html(), /<script>/);
  assert.match(html(), /失败归属：未知归属/);
});

test('manual submissions use the same property text and retain no-steps semantics', () => {
  const {context, html} = app();
  const value = sample({process_status: 'not_provided', code_correct: null});
  value.source = 'user_submission';
  value.problem_id = 'fixture';
  context.render(value);
  assert.match(html(), /未提交步骤/);
  assert.match(html(), /尚未配置属性测试策略/);
  assert.doesNotMatch(html(), /未发现反例/);
});
