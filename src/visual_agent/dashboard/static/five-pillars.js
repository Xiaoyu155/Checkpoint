const state = { data: null, filter: 'all', selected: '' };

const byId = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
const setText = (id, value) => { const node = byId(id); if (node) node.textContent = String(value ?? ''); };

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
  setText('programStatus', program.status || '-');
  setText('programId', program.program_id || '-');
  setText('missionIds', (data.missions || []).map(item => item.mission_id).filter(Boolean).join(', ') || '-');
  setText('providerModel', [program.provider, program.model].filter(Boolean).join(' / ') || '-');
  setText('workerRepair', `${program.worker_count || 0} / ${program.repair_count || 0}`);
  setText('acceptanceLine', `${program.verification_verdict || '无结论'} · ${program.verification_command || '无验收命令'}`);
  renderPillars(data.pillars || []);
  renderDetail((data.pillars || []).find(item => item.id === state.selected) || (data.pillars || [])[0]);
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
  byId('evidencePanel').innerHTML = `
    <div class="detail-head"><span class="detail-state ${escapeHtml(pillar.status)}">${pillar.status === 'passed' ? '已通过' : '需关注'}</span><h2>${escapeHtml(pillar.title)}</h2></div>
    <dl class="detail-list">
      <div><dt>Program ID</dt><dd><code>${escapeHtml(program.program_id || '-')}</code></dd></div>
      <div><dt>Provider / Model</dt><dd><code>${escapeHtml([program.provider, program.model].filter(Boolean).join(' / ') || '-')}</code></dd></div>
      <div><dt>上游 Memory ID</dt><dd><code>${escapeHtml(program.upstream_memory_id || '-')}</code></dd></div>
      <div><dt>验收结论</dt><dd>${escapeHtml(program.verification_verdict || '-')}</dd></div>
      <div><dt>验收命令</dt><dd><code>${escapeHtml(program.verification_command || '-')}</code></dd></div>
      <div><dt>路线 SHA-256</dt><dd><code>${escapeHtml(program.source_plan_sha256 || '-')}</code></dd></div>
      <div><dt>完成顺序</dt><dd><code class="sequence">${escapeHtml(sequence || '-')}</code></dd></div>
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
