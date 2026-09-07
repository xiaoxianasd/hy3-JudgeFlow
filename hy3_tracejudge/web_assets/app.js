'use strict';

let problems = [];
let authenticationRequired = false;
let evaluationRunning = false;
let selectedProblemId = null;
let workflow = 'generation';
let submissionEnabled = false;
let submissionMaxBytes = 65536;
const submissionDrafts = new Map();
const $ = selector => document.querySelector(selector);
const esc = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

class APIRequestError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

function apiKey() { return sessionStorage.getItem('tracejudge_api_key') || ''; }

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const key = apiKey();
  if (key) headers.set('X-API-Key', key);
  const response = await fetch(path, {...options, headers});
  let value;
  try { value = await response.json(); } catch (_) { value = {}; }
  if (!response.ok) {
    const error = value.error || {};
    throw new APIRequestError(error.message || `请求失败 (${response.status})`, response.status, error.code);
  }
  return value;
}

function setHealth(message, state = '') {
  $('#health').textContent = message;
  $('#dot').className = `dot ${state}`;
}

function problemCode(problem) {
  if (problem.id.startsWith('mbpp_Mbpp/')) return problem.id.replace('mbpp_Mbpp/', 'MBPP ');
  return problem.id;
}

function tierName(problem) {
  return problem.tier === 'external' ? 'MBPP / EvalPlus' : '原创核心题';
}

function renderProblemOptions() {
  if (evaluationRunning) return;
  const select = $('#problem');
  const previous = select.value;
  const query = $('#problemSearch').value.trim().toLocaleLowerCase();
  const difficulty = $('#difficulty').value;
  const filtered = problems.filter(problem => {
    const matchesDifficulty = difficulty === 'all' || problem.difficulty === difficulty;
    const haystack = `${problem.id} ${problem.title} ${problem.statement}`.toLocaleLowerCase();
    return matchesDifficulty && (!query || haystack.includes(query));
  });

  const groups = [
    ['seed', '原创核心题'],
    ['external', 'MBPP / EvalPlus 外部题']
  ];
  const nodes = [];
  groups.forEach(([tier, label]) => {
    const entries = filtered.filter(problem => (problem.tier || 'seed') === tier);
    if (!entries.length) return;
    const group = document.createElement('optgroup');
    group.label = `${label}（${entries.length} 道）`;
    entries.forEach(problem => {
      const option = document.createElement('option');
      option.value = problem.id;
      option.textContent = `${problemCode(problem)} · [${problem.difficulty}] ${problem.title}`;
      group.append(option);
    });
    nodes.push(group);
  });
  select.replaceChildren(...nodes);
  select.disabled = filtered.length === 0;
  $('#run').disabled = filtered.length === 0;
  if (filtered.some(problem => problem.id === previous)) select.value = previous;
  $('#problemCount').textContent = filtered.length
    ? `当前可选 ${filtered.length} / ${problems.length} 道题`
    : `没有匹配的题目，请修改搜索条件（题库共 ${problems.length} 道）`;
  showProblem();
  updateSubmissionControls();
}

function showProblem() {
  if (evaluationRunning) return;
  const problem = problems.find(item => item.id === $('#problem').value);
  const nextId = problem ? problem.id : null;
  if (nextId !== selectedProblemId) {
    selectedProblemId = nextId;
    const draft = submissionDrafts.get(nextId) || {code: '', steps: ''};
    $('#userCode').value = draft.code;
    $('#userSteps').value = draft.steps;
    $('#submissionValidation').hidden = true;
    setPipeline('queued');
    $('#result').className = 'card empty';
    $('#result').innerHTML = '<div><b>尚未开始评估</b><br>确认上方题目内容后，点击解答与评估按钮开始</div>';
  }
  $('#problemContent').hidden = !problem;
  $('#submissionProblem').textContent = problem ? `${problemCode(problem)} · ${problem.title}` : '请先选择题目';
  updateSubmissionControls();
  $('#problemNotice').hidden = Boolean(problem);
  if (!problem) {
    $('#problemTitle').textContent = '未选择题目';
    $('#problemId').textContent = '';
    $('#problemNotice').textContent = '当前筛选条件下没有匹配的题目，请调整搜索条件。';
    $('#problemMeta').replaceChildren();
    $('#statement').textContent = '';
    return;
  }
  $('#problemTitle').textContent = problem.title;
  $('#problemId').textContent = problemCode(problem);
  const metadata = [
    tierName(problem),
    problem.difficulty.toUpperCase(),
    `公开 ${problem.public_test_count} / 隐藏 ${problem.hidden_test_count} 个测试`
  ];
  $('#problemMeta').replaceChildren(...metadata.map(value => {
    const chip = document.createElement('span');
    chip.className = 'meta-chip';
    chip.textContent = value;
    return chip;
  }));
  $('#statement').textContent = displayStatement(problem);
  $('#problemEntrypoint').textContent = `${problem.function_name || 'solve_case'}(case)`;
  $('#adapterNote').hidden = problem.tier !== 'external';
  const fields = Object.entries(problem.input_schema || {});
  $('#inputSchema').innerHTML = fields.length
    ? fields.map(([name, type]) => `<dt><code>${esc(name)}</code></dt><dd><code>${esc(typeof type === 'string' ? type : formatValue(type))}</code></dd>`).join('')
    : '<dt>输入字段</dt><dd>题库未提供结构化输入格式，请以题目描述为准。</dd>';
  const constraints = problem.constraints || [];
  $('#constraints').innerHTML = (constraints.length ? constraints : ['题库未提供额外约束，请以题目描述为准。'])
    .map(item => `<li>${esc(item)}</li>`).join('');
  const examples = problem.public_tests || [];
  $('#publicExamples').innerHTML = examples.length
    ? examples.map((example, index) => `<article class="public-example"><h4>示例 ${index + 1}</h4><div class="example-values"><div><span>输入 · case</span><pre>${esc(formatValue(example.input))}</pre></div><div><span>预期输出</span><pre>${esc(formatValue(example.expected))}</pre></div></div></article>`).join('')
    : '<p class="detail-note">该题暂无公开示例。</p>';
  $('#problemSource').textContent = problem.source || '未标注';
}

function displayStatement(problem) {
  const lines = String(problem.source_statement || problem.statement || '').trim().split(/\r?\n/);
  const wrapper = lines[0].trim();
  // Imported prompts can be wrapped in a Python docstring; keep all content inside it.
  if (lines.length > 1 && (wrapper === '"""' || wrapper === "'''") && lines[lines.length - 1].trim() === wrapper) {
    lines.shift();
    lines.pop();
  }
  return lines.join('\n').trim() || '题库暂未提供题目描述。';
}

function formatValue(value) {
  return JSON.stringify(value, null, 2) ?? '未提供';
}

function setEvaluationRunning(running) {
  evaluationRunning = running;
  ['#problemSearch', '#difficulty', '#examples', '#reviewMode', '#userCode', '#userSteps', '#generateMode', '#submissionMode'].forEach(selector => {
    $(selector).disabled = running;
  });
  $('#problem').disabled = running || !$('#problem').value;
  $('#run').disabled = running || !$('#problem').value;
  updateSubmissionControls();
}

function updateSubmissionControls() {
  $('#submitCode').disabled = evaluationRunning || !$('#problem').value || !submissionEnabled;
  $('#submissionSafety').hidden = submissionEnabled;
}

function rememberSubmissionDraft() {
  if (selectedProblemId) submissionDrafts.set(selectedProblemId, {code: $('#userCode').value, steps: $('#userSteps').value});
}

function setWorkflow(next) {
  if (evaluationRunning || next === workflow) return;
  workflow = next;
  const manual = workflow === 'submission';
  $('#generateMode').classList.toggle('selected', !manual);
  $('#submissionMode').classList.toggle('selected', manual);
  $('#generateMode').setAttribute('aria-pressed', String(!manual));
  $('#submissionMode').setAttribute('aria-pressed', String(manual));
  $('#submissionPanel').hidden = !manual;
  $('#generationOptions').hidden = manual;
  $('#generationPipeline').hidden = manual;
  $('#submissionHint').hidden = !manual;
  $('#run').hidden = manual;
  $('#problemSelectLabel').textContent = manual ? '选择你的代码对应的题目' : '选择要让 Hy3 解答的题目';
  $('#submissionValidation').hidden = true;
  $('#result').className = 'card empty';
  $('#result').innerHTML = manual
    ? '<div><b>等待代码提交</b><br>填写你的代码，可选填写解题步骤，然后提交评估</div>'
    : '<div><b>尚未开始评估</b><br>选择题目后，让 Hy3 生成解答并评估</div>';
  setPipeline('queued');
  updateSubmissionControls();
}

function showMode() {
  const hints = {
    supervisor:'推荐：5 个专业 Agent 并行审查，额外调用 Hy3 5 次。',
    swarm:'5 个专家并行；存在冲突或错误时再调用 1 次仲裁。',
    single:'兼容基线：仅调用 1 次通用过程评审。'
  };
  $('#modeHint').textContent = hints[$('#reviewMode').value];
}

async function checkHealth() {
  if (authenticationRequired && !apiKey()) {
    setHealth('请输入 API 访问密钥');
    return;
  }
  setHealth('正在检查 Hy3…');
  try {
    const value = await api('/api/v1/health/hy3');
    setHealth(value.ok ? `${value.requested_model} 已连接 · ${value.latency_ms}ms` : 'Hy3 模型不可用', value.ok ? 'ok' : 'bad');
  } catch (error) {
    setHealth(error.message, 'bad');
  }
}

function setPipeline(phase) {
  const items = [...document.querySelectorAll('.pipe')];
  items.forEach(item => item.className = 'pipe');
  let done = 0;
  let active = -1;
  if (phase === 'hy3_generation') active = 0;
  if (phase === 'process_evaluation') { done = 1; active = 2; }
  if (phase === 'completed') done = items.length;
  items.forEach((item, index) => {
    if (index < done || phase === 'completed') item.classList.add('done');
    else if (index === active) item.classList.add('active');
  });
}

function renderAgents(evaluation) {
  const orchestration = evaluation.orchestration;
  if (!orchestration) return `<p>${evaluation.review_coverage?.expected === 0 ? '当前仅使用规则校验，未调用模型评审。' : '当前使用 Single 基线评审。'}</p>`;
  const cards = orchestration.specialist_reviews.map(item => {
    const status = item.assessment_status || 'uncertain';
    const tone = status === 'invalid' ? 'bad' : status === 'valid' ? '' : 'uncertain';
    const state = item.status !== 'completed' ? '调用失败 · 证据不足' : item.ignored_by_supervisor ? '意见待复核' : ({valid: '通过审查', invalid: '发现问题', uncertain: '证据不足'}[status] || '证据不足');
    const firstError = status === 'invalid' && item.first_error_step != null ? `<small>模型报告位置：步骤 ${esc(item.first_error_step)} · ${esc(item.error_type || '')}（以汇总定位为准）</small>` : '';
    return `<div class="agent ${tone}"><b>${esc(item.agent)}</b><span class="badge ${tone}">${state}</span><small>审查阶段：${esc(item.stage)} · 置信度 ${Math.round((item.confidence || 0) * 100)}%</small><div>${esc(item.reason || '无补充说明')}</div>${firstError}</div>`;
  }).join('');
  const arbitration = orchestration.arbitration ? `<h3>Swarm 仲裁</h3><p>${esc(orchestration.arbitration.rationale || '已完成仲裁')}</p>` : '';
  return `<p>拓扑：${esc(orchestration.topology)}；本轮审查模型调用 ${orchestration.model_calls} 次；冲突 ${orchestration.conflicts.length} 项。</p><div class="agents">${cards}</div>${arbitration}`;
}

function verdictText(value, positive = '成立', negative = '不成立') {
  return value === true ? positive : value === false ? negative : '证据不足';
}

function verdictTone(value) {
  return value === true ? '' : value === false ? 'bad' : 'uncertain';
}

function propertyCheckText(hypothesis) {
  if (!hypothesis) return '未执行';
  // Old saved jobs have no status field; never interpret disabled as a passing run.
  const status = hypothesis.error ? 'error' : hypothesis.status || (
    hypothesis.enabled === false ? 'unsupported' : hypothesis.enabled !== true ? 'not_run'
      : hypothesis.examples_checked === 0 ? 'not_run' : hypothesis.found === true ? 'failed'
        : hypothesis.found === false ? 'passed' : 'not_run'
  );
  const labels = {unsupported: '该题尚未配置属性测试策略', disabled: '未启用', not_run: '未执行',
    error: '验证异常，结果不可用', failed: '发现反例', passed: '当前预算内未发现反例'};
  const count = Number.isInteger(hypothesis.examples_checked) ? `；实际检查 ${hypothesis.examples_checked} 次（含收缩与重放）` : '';
  const budget = Number.isInteger(hypothesis.max_examples) ? `；生成预算 ${hypothesis.max_examples}` : '';
  return (labels[status] || '状态未知') + count + budget;
}

function render(value) {
  if (value.source === 'user_submission') return renderSubmission(value);
  const evaluation = value.evaluation;
  const answer = value.answer;
  const hypothesis = evaluation.hypothesis;
  const execution = evaluation.execution;
  const coverage = evaluation.review_coverage;
  const steps = Array.isArray(answer.reasoning_steps) ? answer.reasoning_steps.filter(step => step && typeof step === 'object') : [];
  const position = evaluation.first_error_step != null ? `步骤 ${evaluation.first_error_step}`
    : evaluation.process_correct === false ? '尚未定位' : evaluation.process_correct === true ? '未发现错误' : '未确定';
  const coverageText = coverage && coverage.expected > 0
    ? `评审完成 ${coverage.completed}/${coverage.expected}，结论明确 ${coverage.conclusive}/${coverage.expected}`
    : coverage ? '未调用模型评审' : '历史记录未提供评审覆盖统计';
  const fixedText = execution ? `${execution.passed}/${execution.total}${execution.harness_error ? '（执行异常，请结合错误信息复核）' : ''}` : '未执行';
  $('#result').className = 'card';
  $('#result').innerHTML = `<div class="metrics">
    <div class="metric ${verdictTone(evaluation.final_correct)}"><span>测试验证</span><b>${verdictText(evaluation.final_correct, '通过当前测试', '未通过')}</b></div>
    <div class="metric ${verdictTone(evaluation.process_correct)}"><span>推理过程</span><b>${verdictText(evaluation.process_correct)}</b></div>
    <div class="metric"><span>错误定位</span><b>${esc(position)}</b></div></div>
    <p class="assessment-note ${verdictTone(evaluation.process_correct)}">${esc(evaluation.assessment_note || '历史记录未提供判定说明，建议重新评估。')}</p>
    <p class="detail-note">测试通过仅表示当前测试覆盖内未发现错误，不是完整正确性证明。</p>
    <h2>推理链</h2><div class="steps">${steps.map(step => `<div class="step ${step.id === evaluation.first_error_step ? 'bad' : ''}"><b>${esc(step.id)}. ${esc(step.title)}</b><div>${esc(step.content)}</div></div>`).join('')}</div>
    <h2>过程审查</h2><p>${esc(coverageText)}</p>${renderAgents(evaluation)}
    <h2>验证证据</h2><p>固定测试 ${esc(fixedText)}；Hypothesis：${esc(propertyCheckText(hypothesis))}。</p>
    <p>错误类型：${esc((evaluation.error_labels || []).join('、') || (evaluation.process_correct === true ? '未发现' : '未确认'))}</p>
    ${hypothesis?.counterexample && !hypothesis.error ? `<pre>${esc(formatValue(hypothesis.counterexample))}</pre>` : ''}
    <h2>Hy3 代码</h2><pre>${esc(answer.code || '')}</pre>`;
}

function renderSubmission(value) {
  const evaluation = value.evaluation;
  const answer = value.answer;
  const review = evaluation.submission_review;
  const execution = evaluation.execution;
  const hypothesis = evaluation.hypothesis;
  const verdict = (value, positive = '未发现问题') => verdictText(value, positive, '发现问题');
  const position = [
    evaluation.first_error_step != null ? `步骤 ${evaluation.first_error_step}` : '',
    evaluation.first_error_line != null ? `代码行 ${evaluation.first_error_line}` : '',
  ].filter(Boolean).join(' · ') || '未定位';
  const findings = (review?.findings || []).map(item => {
    const location = item.scope === 'reasoning' ? `推理步骤 ${item.step}` : (item.line == null ? '代码整体逻辑' : `代码第 ${item.line} 行`);
    return `<article class="finding"><b>${esc(location)}</b><p>${esc(item.reason)}</p>${item.suggestion ? `<p>建议：${esc(item.suggestion)}</p>` : ''}</article>`;
  }).join('');
  const steps = answer.reasoning_steps.length
    ? `<div class="steps">${answer.reasoning_steps.map(step => `<div class="step ${step.id === evaluation.first_error_step ? 'bad' : ''}"><b>用户步骤 ${esc(step.id)}</b><div class="submitted-step">${esc(step.content)}</div></div>`).join('')}</div>`
    : '<p class="detail-note">未提交分步说明。系统不会从代码反推或编造你的思考过程。</p>';
  const propertyStatus = propertyCheckText(hypothesis);
  const code = answer.code.split('\n').map((line, index) => `<span class="code-line ${index + 1 === evaluation.first_error_line ? 'flagged' : ''}">${esc(line) || ' '}</span>`).join('');
  const publicTests = (execution.tests || []).filter(item => item.visibility === 'public').map(item => `<details><summary>${esc(item.name)} · ${item.passed ? '通过' : '未通过'}</summary><pre>${esc(formatValue({expected: item.expected, actual: item.actual, error: item.error}))}</pre></details>`).join('');
  $('#result').className = 'card';
  $('#result').innerHTML = `<h2>我的代码 · 评估报告</h2><p class="detail-note">题目：${esc(value.problem_id)}</p>
    <div class="metrics submission-metrics"><div class="metric"><span>测试验证</span><b>${verdict(evaluation.final_correct, '通过当前测试')}</b></div><div class="metric"><span>实现逻辑</span><b>${verdict(evaluation.code_correct)}</b></div><div class="metric"><span>推理过程</span><b>${evaluation.process_status === 'not_provided' ? '未提交步骤' : verdict(evaluation.process_correct)}</b></div><div class="metric"><span>问题定位</span><b>${esc(position)}</b></div></div>
    <p class="submission-note">${esc(evaluation.assessment_note)}</p><p class="detail-note">测试通过不等于对所有输入的正确性证明。代码行定位来自模型审查，应结合下方证据复核。</p>
    <h3>Hy3 代码与过程审查</h3><p>${esc(review?.reason || '语义审查暂不可用，未将测试通过直接判为逻辑正确。')}</p>
    ${review ? `<p class="detail-note">审查置信度 ${Math.round(review.confidence * 100)}%${review.localization_rejected ? ' · 已排除无效位置，结论待复核' : review.confidence < 0.65 ? ' · 低置信度意见，仅供复核' : ''}</p>` : ''}${findings}
    <h3>你提交的解题步骤</h3>${steps}<h3>验证证据</h3><div class="submission-evidence"><p>固定测试 ${execution.passed}/${execution.total}；Hypothesis：${esc(propertyStatus)}。</p><p>错误类型：${esc(evaluation.error_labels.join('、') || '未确认')}</p>${execution.harness_error ? `<p class="error">${esc(execution.harness_error)}</p>` : ''}${publicTests}${hypothesis?.counterexample ? `<h4>差分反例</h4><pre>${esc(formatValue(hypothesis.counterexample))}</pre>` : ''}</div>
    <h3>你提交的代码（行号从 1 开始）</h3><pre class="code-lines">${code}</pre>`;
}

async function pollJob(statusUrl) {
  const deadline = Date.now() + 20 * 60 * 1000;
  while (Date.now() < deadline) {
    const value = await api(statusUrl);
    const job = value.job;
    setPipeline(job.phase);
    if (job.status === 'succeeded') return job.result;
    if (job.status === 'failed') {
      const failure = job.error || {};
      const message = `${failure.message || '评估任务失败'}（任务编号：${job.id}）`;
      throw new APIRequestError(message, 500, failure.code || 'evaluation_failed');
    }
    $('#result').className = 'card empty';
    const phases = {code_execution: '沙盒测试', property_testing: '属性测试与反例搜索', submission_review: '代码与过程审查'};
    $('#result').innerHTML = `<div><b>评估任务正在运行</b><br>任务 ${esc(job.id.slice(0, 8))} · 阶段 ${esc(phases[job.phase] || job.phase)}</div>`;
    await sleep(1200);
  }
  throw new APIRequestError('等待评估结果超时，可稍后使用任务编号查询', 408, 'poll_timeout');
}

async function runEvaluation() {
  if (evaluationRunning) return;
  if (authenticationRequired && !apiKey()) {
    $('#apiKey').focus();
    $('#result').className = 'card empty';
    $('#result').innerHTML = '<div class="error"><b>无法提交</b><br>请先输入 API 访问密钥</div>';
    return;
  }
  if (!$('#problem').value) {
    $('#result').className = 'card empty';
    $('#result').innerHTML = '<div class="error"><b>请选择题目</b><br>当前筛选条件下没有可提交的题目</div>';
    return;
  }
  const manual = workflow === 'submission';
  const payload = {problem_id: $('#problem').value, hypothesis_examples: Number($('#examples').value)};
  const button = manual ? $('#submitCode') : $('#run');
  if (manual) {
    const validation = $('#submissionValidation');
    payload.code = $('#userCode').value;
    payload.reasoning_steps = $('#userSteps').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const issue = !submissionEnabled ? '请先启用 Docker 安全沙盒。'
      : !payload.code.trim() ? '请先填写 Python 代码。'
      : payload.code.length > 20000 ? '代码不能超过 20,000 字符。'
      : payload.reasoning_steps.length > 20 ? '解题说明最多 20 步，请每行填写一步。'
      : payload.reasoning_steps.some(step => step.length > 2000) ? '每步说明不能超过 2,000 字符。'
      : new TextEncoder().encode(JSON.stringify(payload)).length > submissionMaxBytes ? '提交内容超过服务端请求大小上限，请精简代码或步骤。'
      : '';
    validation.hidden = !issue;
    validation.textContent = issue;
    if (issue) { (payload.code.trim() ? $('#userSteps') : $('#userCode')).focus(); return; }
  } else {
    payload.review_mode = $('#reviewMode').value;
  }
  if (!Number.isInteger(payload.hypothesis_examples) || payload.hypothesis_examples < 1 || payload.hypothesis_examples > 500) {
    $('#examples').reportValidity();
    return;
  }
  setEvaluationRunning(true);
  button.textContent = '正在提交任务…';
  setPipeline('queued');
  $('#result').className = 'card empty';
  $('#result').innerHTML = '<div><b>正在提交评估任务</b><br>服务端将限制并发，避免 Hy3 过载</div>';
  try {
    const submitted = await api(manual ? '/api/v1/code-submissions' : '/api/v1/evaluations', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    button.textContent = `运行中 · ${submitted.job.id.slice(0, 8)}`;
    const result = await pollJob(submitted.status_url);
    setPipeline('completed');
    render(result);
  } catch (error) {
    $('#result').className = 'card empty';
    $('#result').innerHTML = `<div class="error"><b>运行失败</b><br>${esc(error.message)}</div>`;
  } finally {
    setEvaluationRunning(false);
    button.textContent = manual ? '提交代码并评估' : '调用 Hy3 解答并评估所选题';
  }
}

async function init() {
  try {
    const config = await api('/api/v1/config');
    authenticationRequired = config.authentication_required;
    submissionEnabled = Boolean(config.code_submission?.enabled);
    submissionMaxBytes = config.code_submission?.max_request_bytes || 65536;
    updateSubmissionControls();
    $('#authGroup').hidden = !authenticationRequired;
    $('#apiKey').value = apiKey();
    problems = await api('/api/v1/problems');
    renderProblemOptions();
    await checkHealth();
  } catch (error) {
    setHealth(error.message, 'bad');
    if (!problems.length) {
      $('#problemTitle').textContent = '题库加载失败';
      $('#problemNotice').textContent = `${error.message}。请检查服务后刷新页面。`;
    }
  }
}

$('#problem').addEventListener('change', showProblem);
$('#problemSearch').addEventListener('input', renderProblemOptions);
$('#difficulty').addEventListener('change', renderProblemOptions);
$('#reviewMode').addEventListener('change', showMode);
$('#run').addEventListener('click', runEvaluation);
$('#submitCode').addEventListener('click', runEvaluation);
$('#generateMode').addEventListener('click', () => setWorkflow('generation'));
$('#submissionMode').addEventListener('click', () => setWorkflow('submission'));
$('#userCode').addEventListener('input', rememberSubmissionDraft);
$('#userSteps').addEventListener('input', rememberSubmissionDraft);
$('#checkKey').addEventListener('click', async () => {
  sessionStorage.setItem('tracejudge_api_key', $('#apiKey').value.trim());
  await checkHealth();
});
showMode();
init();
