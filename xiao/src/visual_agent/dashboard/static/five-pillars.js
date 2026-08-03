const state = { data: null, filter: 'all', selected: '' };
const PILLAR_STATUSES = new Set(['passed', 'failed', 'partial', 'indeterminate']);
const STATUS_LABELS = {
  passed: '已通过',
  failed: '未通过',
  partial: '部分满足',
  indeterminate: '无法判定',
};

const byId = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const setText = (id, value) => { const node = byId(id); if (node) node.textContent = String(value ?? ''); };
const brief = (value, limit = 180) => {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
};

async function loadEvidence() {
  const button = byId('refreshButton');
  button.disabled = true;
  button.classList.add('is-loading');
  try {
    const response = await fetch('/api/five-pillars', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (!state.selected && state.data.pillars?.length) state.selected = state.data.pillars[0].id;
  } catch (error) {
    state.data = { program: null, missions: [], pillars: [], error: String(error?.message || error) };
  } finally {
    button.disabled = false;
    button.classList.remove('is-loading');
    render();
  }
}

function render() {
  const data = state.data || {};
  const program = data.program;
  const activeLaunch = data.support?.launches?.active || {};
  const trigger = activeLaunch.task_trigger || {};
  const triggerDiagnosis = activeLaunch.trigger_diagnosis || {};
  const triggerStatus = triggerDiagnosis.status || trigger.status || '';
  const triggerReason = triggerDiagnosis.reason_code || '';
  const triggerNotice = triggerStatus && triggerStatus !== 'not_required'
    ? `任务触发：${triggerStatus}${triggerReason ? ` · ${triggerReason}` : ''}`
    : '';
  renderSupport(data.support || {});
  setText('workspacePath', data.workspace_root || '');
  if (!program) {
    setText('evidenceMode', activeLaunch.launch_id ? 'Pacer trigger diagnostics' : 'Pacer native evidence');
    setText('programTitle', activeLaunch.launch_id ? '当前 Pacer 触发诊断' : '暂无已完成的双任务 Program');
    setText('programStatus', data.error ? '读取失败' : triggerNotice || '等待闭环');
    setText('programId', activeLaunch.launch_id || '-');
    setText('missionIds', activeLaunch.task_generation ? `task generation ${activeLaunch.task_generation}` : '-');
    setText('providerModel', '-');
    setText('workerRepair', '-');
    setText('acceptanceLine', data.error || triggerNotice || '');
    byId('pillarList').innerHTML = '<div class="empty">没有可展示的闭环证据。</div>';
    byId('evidencePanel').innerHTML = '';
    return;
  }
  setText('programTitle', program.objective || '已完成 Program');
  setText('evidenceMode', data.mode === 'native' ? 'Pacer native evidence' : 'Legacy Program evidence');
  const programStatus = program.status || '-';
  const latestReview = data.support?.memory?.latest?.task_review || {};
  const productVerdict = latestReview.product_verdict || 'indeterminate';
  const programTone = productVerdict === 'pass' ? 'passed' : productVerdict === 'fail' ? 'failed' : 'indeterminate';
  setText('programStatus', data.mode === 'native' ? `${programStatus} / ${STATUS_LABELS[programTone]}` : programStatus);
  const programState = document.querySelector('.program-state');
  programState?.classList.remove('passed', 'failed', 'partial', 'indeterminate');
  programState?.classList.add(data.mode === 'native' ? programTone : 'partial');
  setText('programId', program.program_id || '-');
  setText('missionIds', (data.missions || []).map(item => item.mission_id).filter(Boolean).join(', ') || '-');
  setText('providerModel', [program.provider, program.model].filter(Boolean).join(' / ') || '-');
  setText('workerRepair', `${program.worker_count || 0} / ${program.repair_count || 0}`);
  setText(
    'acceptanceLine',
    triggerNotice
      ? `${triggerNotice} · ${latestReview.evidence_integrity || program.verification_verdict || '等待结果'}`
      : latestReview.evidence_integrity
      ? `证据 ${latestReview.evidence_integrity} · 标准 ${latestReview.acceptance_adequacy || 'unknown'} · 产品 ${productVerdict}`
      : `${program.verification_verdict || '无结论'} · ${brief(program.verification_command || '无验收命令', 150)}`,
  );
  const pillars = normalizePillars(data.pillars || [], data);
  renderPillars(pillars);
  renderDetail(pillars.find(item => item.id === state.selected) || pillars[0]);
}

function normalizePillars(pillars, data) {
  const supportLaunch = data.support?.launches?.active || {};
  const aggregate = data.mode === 'native' ? supportLaunch.assessment?.pillars || {} : {};
  const rawPillars = data.mode === 'native' ? supportLaunch.pillars || {} : {};
  return pillars.map(item => {
    const embedded = item.assessment && typeof item.assessment === 'object' ? item.assessment : null;
    const aggregateAssessment = aggregate[item.id] && typeof aggregate[item.id] === 'object' ? aggregate[item.id] : null;
    const rawAssessment = rawPillars[item.id]?.assessment && typeof rawPillars[item.id].assessment === 'object'
      ? rawPillars[item.id].assessment
      : null;
    const assessment = embedded || aggregateAssessment || rawAssessment || {};
    let status = PILLAR_STATUSES.has(assessment.status) ? assessment.status : '';
    if (!status && PILLAR_STATUSES.has(item.status)) status = item.status;
    if (!status) status = item.status === 'passed' ? 'partial' : 'indeterminate';
    return { ...item, status, assessment };
  });
}

function renderSupport(support) {
  const account = support.account || {};
  const profile = support.profile || {};
  const memory = support.memory || {};
  const commands = support.commands || {};
  const telemetry = support.telemetry || {};
  const usage = telemetry.usage || {};
  const compactions = telemetry.compactions || {};
  const agents = telemetry.agents || {};
  const methodLabels = {
    chatgpt_subscription: 'ChatGPT subscription',
    api_key: 'API key / relay token',
    codex_login: 'Codex login',
    none: '未认证',
  };
  setText('accountStatus', account.authenticated ? '已登录' : '未登录');
  setText('accountMethod', methodLabels[account.auth_method] || account.status || '未知');
  setText('profileStatus', profile.configured ? (profile.display_name || profile.email || '已保存') : '未绑定');
  setText('memoryMetric', `${memory.total_outcomes || 0} 条`);
  setText('memoryDetail', `${memory.completed || 0} completed · ${memory.failed_or_blocked || 0} failed/blocked`);
  setText('commandMetric', `${commands.passed_runs || 0} / ${commands.total_runs || 0}`);
  setText('commandDetail', `${commands.passed_steps || 0}/${commands.executed_steps || 0} steps · ${Number(commands.elapsed_seconds || 0).toFixed(1)}s`);
  if (telemetry.status === 'captured') {
    setText('telemetryMetric', `${Number(usage.input_tokens || 0).toLocaleString()} in · ${Number(usage.output_tokens || 0).toLocaleString()} out`);
    setText('telemetryDetail', `${Number(usage.cached_input_tokens || 0).toLocaleString()} cached · ${compactions.count || 0} compact · ${agents.completed || 0}/${agents.total || 0} agents · ${telemetry.attribution_confidence || 'none'}`);
  } else {
    setText('telemetryMetric', telemetry.status || '等待数据');
    setText('telemetryDetail', '新一次 pacer 会话退出后生成');
  }
}

function renderPillars(pillars) {
  const visible = pillars.filter(item => state.filter === 'all' || item.status === state.filter);
  byId('pillarList').innerHTML = visible.length ? visible.map(item => `
    <button class="pillar-row ${escapeHtml(item.status)} ${item.id === state.selected ? 'active' : ''}" type="button" data-id="${escapeHtml(item.id)}">
      <span class="row-index">${String(pillars.indexOf(item) + 1).padStart(2, '0')}</span>
      <span class="row-copy"><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.evidence)}</small></span>
      <span class="metric">${escapeHtml(item.metric)}</span>
      <span class="row-status">${STATUS_LABELS[item.status] || STATUS_LABELS.indeterminate}</span>
    </button>`).join('') : '<div class="empty">当前筛选没有项目。</div>';
  document.querySelectorAll('.pillar-row').forEach(row => row.addEventListener('click', () => {
    state.selected = row.dataset.id || '';
    render();
  }));
}

function renderDetail(pillar) {
  if (!pillar) { byId('evidencePanel').innerHTML = ''; return; }
  const program = state.data.program || {};
  const sequence = (program.task_sequence || []).map(item => `${item.task_id}  ${item.mission_id}  ${item.status}`).join('\n');
  const support = state.data.support || {};
  const runtime = support.runtime || {};
  const memory = support.memory || {};
  const commands = support.commands || {};
  const telemetry = support.telemetry || {};
  const usage = telemetry.usage || {};
  const compactions = telemetry.compactions || {};
  const agents = telemetry.agents || {};
  const assessment = pillar.assessment || {};
  const latestReview = support.memory?.latest?.task_review || {};
  const acceptanceAssessment = latestReview.acceptance_assessment || {};
  const reasons = (assessment.reason_codes || []).join(', ') || '-';
  byId('evidencePanel').innerHTML = `
    <div class="detail-head"><span class="detail-state ${escapeHtml(pillar.status)}">${escapeHtml(STATUS_LABELS[pillar.status] || STATUS_LABELS.indeterminate)}</span><h2>${escapeHtml(pillar.title)}</h2></div>
    <dl class="detail-list">
      <div><dt>核心状态</dt><dd><code>${escapeHtml(pillar.status)}</code></dd></div>
      <div><dt>证据充分性</dt><dd><code>${escapeHtml(assessment.adequacy || 'unknown')}</code></dd></div>
      <div><dt>原因代码</dt><dd><code>${escapeHtml(reasons)}</code></dd></div>
      <div><dt>证据完整性</dt><dd><code>${escapeHtml(latestReview.evidence_integrity || 'unknown')}</code></dd></div>
      <div><dt>验收标准</dt><dd><code>${escapeHtml(latestReview.acceptance_adequacy || 'unknown')}</code></dd></div>
      <div><dt>产品结论</dt><dd><code>${escapeHtml(latestReview.product_verdict || 'indeterminate')}</code></dd></div>
      <div><dt>标准来源</dt><dd><code>${escapeHtml(acceptanceAssessment.standard_source || 'unknown')}</code></dd></div>
      <div><dt>Program ID</dt><dd><code>${escapeHtml(program.program_id || '-')}</code></dd></div>
      <div><dt>Provider / Model</dt><dd><code>${escapeHtml([program.provider, program.model].filter(Boolean).join(' / ') || '-')}</code></dd></div>
      <div><dt>上游 Memory ID</dt><dd><code>${escapeHtml(program.upstream_memory_id || '-')}</code></dd></div>
      <div><dt>验收结论</dt><dd>${escapeHtml(program.verification_verdict || '-')}</dd></div>
      <div><dt>验收摘要</dt><dd><code>${escapeHtml(brief(program.verification_command || '-', 320))}</code></dd></div>
      <div><dt>路线 SHA-256</dt><dd><code>${escapeHtml(program.source_plan_sha256 || '-')}</code></dd></div>
      <div><dt>完成顺序</dt><dd><code class="sequence">${escapeHtml(sequence || '-')}</code></dd></div>
      <div><dt>Native auto compact</dt><dd><code>${escapeHtml(runtime.auto_compact_limit ? `${runtime.auto_compact_limit} tokens` : '-')}</code></dd></div>
      <div><dt>Outcome ledger</dt><dd><code>${escapeHtml(memory.path || '-')}</code></dd></div>
      <div><dt>命令证据目录</dt><dd><code>${escapeHtml(commands.root || '-')}</code></dd></div>
      <div><dt>Rollout token</dt><dd><code>${escapeHtml(telemetry.status === 'captured' ? `${Number(usage.input_tokens || 0).toLocaleString()} input · ${Number(usage.cached_input_tokens || 0).toLocaleString()} cached · ${Number(usage.output_tokens || 0).toLocaleString()} output` : telemetry.status || '-')}</code></dd></div>
      <div><dt>压缩 / 子代理</dt><dd><code>${escapeHtml(`${compactions.count || 0} compactions · ${agents.completed || 0}/${agents.total || 0} completed · ${telemetry.attribution_confidence || 'none'} confidence`)}</code></dd></div>
    </dl>`;
}

function setFilter(filter) {
  state.filter = filter;
  document.querySelectorAll('#filterSegments button').forEach(button => button.classList.toggle('active', button.dataset.filter === filter));
  render();
}

byId('refreshButton').addEventListener('click', loadEvidence);
document.querySelectorAll('#filterSegments button').forEach(button => button.addEventListener('click', () => setFilter(button.dataset.filter || 'all')));
loadEvidence();
