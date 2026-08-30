'use strict';

let problems = [];
let authenticationRequired = false;
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
}

function showProblem() {
  const problem = problems.find(item => item.id === $('#problem').value);
  if (!problem) {
    $('#problemMeta').replaceChildren();
    $('#statement').textContent = '';
    return;
  }
  const metadata = [
    tierName(problem),
    problem.difficulty.toUpperCase(),
    `函数 ${problem.function_name}`,
    `公开 ${problem.public_test_count} / 隐藏 ${problem.hidden_test_count} 个测试`
  ];
  $('#problemMeta').replaceChildren(...metadata.map(value => {
    const chip = document.createElement('span');
    chip.className = 'meta-chip';
    chip.textContent = value;
    return chip;
  }));
  $('#statement').textContent = problem.statement;
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
  if (!orchestration) return '<p>当前使用 Single 基线评审。</p>';
  const cards = orchestration.specialist_reviews.map(item => {
    const failed = item.status !== 'completed' || item.valid === false;
    const state = item.status !== 'completed' ? '调用失败' : (item.valid ? '通过' : '发现问题');
    const firstError = item.first_error_step != null ? `<small>候选首错：步骤 ${item.first_error_step} · ${esc(item.error_type || '')}</small>` : '';
    return `<div class="agent ${failed ? 'bad' : ''}"><b>${esc(item.agent)}</b><span class="badge ${failed ? 'bad' : ''}">${state}</span><small>审查阶段：${esc(item.stage)} · 置信度 ${Math.round((item.confidence || 0) * 100)}%</small><div>${esc(item.reason || '无补充说明')}</div>${firstError}</div>`;
  }).join('');
  const arbitration = orchestration.arbitration ? `<h3>Swarm 仲裁</h3><p>${esc(orchestration.arbitration.rationale || '已完成仲裁')}</p>` : '';
  return `<p>拓扑：${esc(orchestration.topology)}；本轮审查模型调用 ${orchestration.model_calls} 次；冲突 ${orchestration.conflicts.length} 项。</p><div class="agents">${cards}</div>${arbitration}`;
}

function render(value) {
  const evaluation = value.evaluation;
  const answer = value.answer;
  const hypothesis = evaluation.hypothesis;
  $('#result').className = 'card';
  $('#result').innerHTML = `<div class="metrics"><div class="metric"><span>最终答案</span><b>${evaluation.final_correct ? '正确' : '错误'}</b></div><div class="metric"><span>推理过程</span><b>${evaluation.process_correct ? '成立' : '不成立'}</b></div><div class="metric"><span>首个错误</span><b>${evaluation.first_error_step == null ? '—' : `步骤 ${esc(evaluation.first_error_step)}`}</b></div></div><h2>推理链</h2><div class="steps">${answer.reasoning_steps.map(step => `<div class="step ${step.id === evaluation.first_error_step ? 'bad' : ''}"><b>${esc(step.id)}. ${esc(step.title)}</b><div>${esc(step.content)}</div></div>`).join('')}</div><h2>多 Agent 审查</h2>${renderAgents(evaluation)}<h2>验证证据</h2><p>固定测试 ${evaluation.execution.passed}/${evaluation.execution.total}；Hypothesis ${hypothesis ? (hypothesis.found ? '发现并缩减出反例' : '未发现反例') : '未启用'}；错误类型：${evaluation.error_labels.join('、') || '无'}</p>${hypothesis && hypothesis.counterexample ? `<pre>${esc(JSON.stringify(hypothesis.counterexample, null, 2))}</pre>` : ''}<h2>Hy3 代码</h2><pre>${esc(answer.code)}</pre>`;
}

async function pollJob(statusUrl) {
  const deadline = Date.now() + 20 * 60 * 1000;
  while (Date.now() < deadline) {
    const value = await api(statusUrl);
    const job = value.job;
    setPipeline(job.phase);
    if (job.status === 'succeeded') return job.result;
    if (job.status === 'failed') throw new APIRequestError(job.error.message, 500, job.error.code);
    $('#result').className = 'card empty';
    $('#result').innerHTML = `<div><b>评估任务正在运行</b><br>任务 ${esc(job.id.slice(0, 8))} · 阶段 ${esc(job.phase)}</div>`;
    await sleep(1200);
  }
  throw new APIRequestError('等待评估结果超时，可稍后使用任务编号查询', 408, 'poll_timeout');
}

async function runEvaluation() {
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
  const button = $('#run');
  button.disabled = true;
  button.textContent = '正在提交任务…';
  setPipeline('queued');
  $('#result').className = 'card empty';
  $('#result').innerHTML = '<div><b>正在提交评估任务</b><br>服务端将限制并发，避免 Hy3 过载</div>';
  try {
    const submitted = await api('/api/v1/evaluations', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        problem_id: $('#problem').value,
        hypothesis_examples: Number($('#examples').value),
        review_mode: $('#reviewMode').value
      })
    });
    button.textContent = `运行中 · ${submitted.job.id.slice(0, 8)}`;
    const result = await pollJob(submitted.status_url);
    setPipeline('completed');
    render(result);
  } catch (error) {
    $('#result').className = 'card empty';
    $('#result').innerHTML = `<div class="error"><b>运行失败</b><br>${esc(error.message)}</div>`;
  } finally {
    button.disabled = !$('#problem').value;
    button.textContent = '调用 Hy3 解答并评估所选题';
  }
}

async function init() {
  try {
    const config = await api('/api/v1/config');
    authenticationRequired = config.authentication_required;
    $('#authGroup').hidden = !authenticationRequired;
    $('#apiKey').value = apiKey();
    problems = await api('/api/v1/problems');
    renderProblemOptions();
    await checkHealth();
  } catch (error) {
    setHealth(error.message, 'bad');
  }
}

$('#problem').addEventListener('change', showProblem);
$('#problemSearch').addEventListener('input', renderProblemOptions);
$('#difficulty').addEventListener('change', renderProblemOptions);
$('#reviewMode').addEventListener('change', showMode);
$('#run').addEventListener('click', runEvaluation);
$('#checkKey').addEventListener('click', async () => {
  sessionStorage.setItem('tracejudge_api_key', $('#apiKey').value.trim());
  await checkHealth();
});
showMode();
init();
