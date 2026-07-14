const state = { data: null, filter: 'all', selected: '' };

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
  renderSupport(data.support || {});
  setText('workspacePath', data.workspace_root || '');
  if (!program) {
    setText('programTitle', '暂无已完成的双任务 Program');
    setText('programStatus', data.error ? '读取失败' : '等待闭环');
    setText('programId', '-');
    setText('missionIds', '-');
    setText('providerModel', '-');
    setText('workerRepair', '-');
    setText('acceptanceLine', data.error || '');
    byId('pillarList').innerHTML = '<div class="empty">没有可展示的闭环证据。</div>';
    byId('evidencePanel').innerHTML = '';
    return;
  }
  setText('programTitle', program.objective || '已完成 Program');
  setText('evidenceMode', data.mode === 'native' ? 'Pacer native evidence' : 'Legacy Program evidence');
  const programStatus = program.status || '-';
  setText('programStatus', programStatus);
  document.querySelector('.program-state')?.classList.toggle('attention', !['completed', 'passed', 'verified'].includes(programStatus));
  setText('programId', program.program_id || '-');
  setText('missionIds', (data.missions || []).map(item => item.mission_id).filter(Boolean).join(', ') || '-');
  setText('providerModel', [program.provider, program.model].filter(Boolean).join(' / ') || '-');
  setText('workerRepair', `${program.worker_count || 0} / ${program.repair_count || 0}`);
  setText('acceptanceLine', `${program.verification_verdict || '无结论'} · ${brief(program.verification_command || '无验收命令', 150)}`);
  renderPillars(data.pillars || []);
  renderDetail((data.pillars || []).find(item => item.id === state.selected) || (data.pillars || [])[0]);
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
      <span class="row-status">${item.status === 'passed' ? '通过' : '关注'}</span>
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
  byId('evidencePanel').innerHTML = `
    <div class="detail-head"><span class="detail-state ${escapeHtml(pillar.status)}">${pillar.status === 'passed' ? '已通过' : '需关注'}</span><h2>${escapeHtml(pillar.title)}</h2></div>
    <dl class="detail-list">
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
