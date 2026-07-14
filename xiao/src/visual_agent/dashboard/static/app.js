// ---------- 前端错误上报：任何 JS 崩溃都落盘到 ~/.pacer/logs/dashboard.log ----------
function _reportClientError(message, source, line, stack){
  try{
    fetch('/api/client-error',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:String(message||''),source:String(source||''),line:line||0,stack:String(stack||'')})});
  }catch(e){/* 上报本身失败不再抛出 */}
  try{ toast('页面出现错误，已记录到诊断日志：'+String(message||'').slice(0,80), false); }catch(e){}
}
window.onerror = function(msg, src, line, col, err){
  _reportClientError(msg, src, line, err && err.stack);
  return false;
};
window.addEventListener('unhandledrejection', function(ev){
  const r = ev && ev.reason;
  _reportClientError(r && r.message ? r.message : String(r), 'promise', 0, r && r.stack);
});

// ---------- 停止原因中文翻译 + 修复建议 ----------
const STOP_REASON = {
  coverage_gap:        { zh:'缺少验收方案', fix:'在主对话框补充人工/现场验收步骤，确认后再预览或发送' },
  manual_verification_required: { zh:'需要人工验收方案', fix:'在主对话框确认设备、场景、指标和结论模板' },
  review_plan_required: { zh:'需要审查计划确认', fix:'在主对话框确认审查范围和计划交付格式' },
  archived:            { zh:'已删除', fix:'任务已从看板隐藏，痕迹保留在 mission 目录里' },
  permission_required: { zh:'仓库有未提交的改动', fix:'先在项目目录 git commit 或 git stash，再重试' },
  same_failure_repeated:{ zh:'同一错误修复两次仍然失败', fix:'打开最终报告看具体报错，把目标改得更精确再重试' },
  budget_exhausted:    { zh:'超出轮次预算', fix:'目标拆得更小，或命令行加 --max-rounds 参数再续跑' },
  worker_error:        { zh:'AI Worker 进程异常退出', fix:'运行 checkpoint agents doctor 确认 Agent 可用，再恢复任务' },
  worker_toolchain_violation:{ zh:'验收通过但工具链违规', fix:'检查 worker 日志中的 Dart/Flutter 路径，必须使用任务指定的 SDK 可执行文件' },
  quota_exhausted:     { zh:'AI 额度已用完', fix:'打开右侧中转站继续付费开发，或等额度恢复后再恢复任务' },
  needs_clarification: { zh:'目标不够具体无法执行', fix:'把目标改写为：做什么 + 改哪里 + 什么叫完成' },
  verification_failed: { zh:'验收命令仍然失败', fix:'打开最终报告看具体报错，重新创建更精确的任务' },
  command_timeout:     { zh:'验收命令超时', fix:'加快测试速度，或先跑部分测试验证思路' },
  command_launch_error:{ zh:'验收命令无法启动', fix:'先手动运行一次验收命令确认它能跑' },
  test_command_invalid:{ zh:'验收命令有误', fix:'检查测试命令是否正确，先手动运行确认' },
  inspection_only:     { zh:'仅检查验收（无交互）', fix:'加 --run-profile supervised 做真实交互验收' },
};
function srInfo(code){ return STOP_REASON[code] || { zh: code||'', fix:'' }; }

const STATUS_ZH = {
  created:'待执行', running:'执行中', background_running:'后台执行中', preview_running:'预览中', preview:'预览完成',
  retrying:'重试派发中', retried:'已重新派发',
  verified:'验收通过', verified_blocked:'验收通过但阻断', merged:'已合并', archived:'已删除', stopped:'已停止', failed:'失败',
};
const zh = s => STATUS_ZH[s] || s;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const jsq = s => String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'');
function setText(id, value){
  const el = document.getElementById(id);
  if(el) el.textContent = String(value);
}
function setDisplay(id, value){
  const el = document.getElementById(id);
  if(el) el.style.display = value;
}
function fmtTime(value){
  const raw = String(value || '').trim();
  if(!raw) return '';
  const match = raw.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?/);
  const iso = match
    ? `${match[1]}T${match[2]}${match[3]?'.'+match[3].slice(0,3):''}${match[4]||''}`
    : raw;
  const d = new Date(iso);
  if(Number.isNaN(d.getTime())) return raw.slice(0,19).replace('T',' ');
  const pad = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function stClass(s){
  const t=(s||'').toLowerCase();
  if(t==='verified') return 'st-verified';
  if(t==='verified_blocked') return 'st-stopped';
  if(t==='running'||t==='background_running'||t==='preview_running'||t==='retrying') return 'st-running';
  if(t==='stopped'||t==='failed') return 'st-stopped';
  if(t==='merged') return 'st-merged';
  if(t==='preview') return 'st-preview';
  return '';
}
function pc(s){
  const t=(s||'').toLowerCase();
  if(t.includes('verified_blocked')||t.includes('toolchain')) return 'warn';
  if(t.includes('pass')||t.includes('verified')||t.includes('merged')) return 'ok';
  if(t.includes('fail')||t.includes('error')||t.includes('conflict')) return 'fail';
  if(t.includes('clarif')||t.includes('coverage')||t.includes('budget')||
     t.includes('stopped')||t.includes('exhausted')||t.includes('repeated')) return 'warn';
  if(t.includes('running')||t.includes('preview')) return 'acc';
  return 'mut';
}

// ---------- Toast ----------
let _toastTimer;
const PANEL_STATE_PREFIX = 'pacer.panel.focus.';
const WORKBENCH_VIEW_KEY = 'pacer.workbench.activeView';
function toast(msg, ok=true){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.style.borderColor=ok?'rgba(63,185,80,.4)':'rgba(248,81,73,.4)';
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>el.classList.remove('show'),3200);
}

function togglePanel(ev, key){
  if(ev) ev.stopPropagation();
  const panel = document.querySelector(`[data-collapse-key="${key}"]`);
  if(!panel) return;
  panel.classList.toggle('is-collapsed');
  try{ localStorage.setItem(PANEL_STATE_PREFIX+key, panel.classList.contains('is-collapsed') ? 'collapsed' : 'open'); }catch(e){}
  _syncPanelToggle(panel);
}

function focusWorkbenchPanel(key, focusSelector=''){
  switchWorkbenchView(key, focusSelector);
  const panel = document.querySelector(`[data-collapse-key="${key}"]`);
  if(!panel) return;
  if(panel.classList.contains('is-collapsed')){
    panel.classList.remove('is-collapsed');
    try{ localStorage.setItem(PANEL_STATE_PREFIX+key, 'open'); }catch(e){}
    _syncPanelToggle(panel);
  }
}

function switchWorkbenchView(key, focusSelector=''){
  const target = document.querySelector(`[data-workbench-view="${key}"]`);
  if(!target) return;
  const alreadyActive = target.classList.contains('is-active-view');
  document.querySelectorAll('[data-workbench-view]').forEach(view=>{
    const active = view === target;
    view.classList.toggle('is-active-view', active);
    view.setAttribute('aria-hidden', active ? 'false' : 'true');
  });
  document.querySelectorAll('[data-nav-view]').forEach(btn=>{
    btn.classList.toggle('active', btn.getAttribute('data-nav-view') === key);
  });
  const mobileNav = document.getElementById('mobileViewNav');
  if(mobileNav && mobileNav.value !== key) mobileNav.value = key;
  if(target.classList.contains('is-collapsed')){
    target.classList.remove('is-collapsed');
    _syncPanelToggle(target);
    try{ localStorage.setItem(PANEL_STATE_PREFIX+key, 'open'); }catch(e){}
  }
  try{ localStorage.setItem(WORKBENCH_VIEW_KEY, key); }catch(e){}
  if(!alreadyActive || focusSelector) _scrollViewTarget(target);
  _focusViewTarget(focusSelector);
  if(key === 'observability-panel') ensureObservabilityLoaded();
}

function _prefersReducedMotion(){
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function _scrollViewTarget(target){
  if(!target || typeof target.scrollIntoView !== 'function') return;
  setTimeout(()=>target.scrollIntoView({behavior:_prefersReducedMotion()?'auto':'smooth', block:'start'}), 0);
}

function _focusViewTarget(focusSelector=''){
  if(!focusSelector) return;
  setTimeout(()=>{
    const target = document.querySelector(focusSelector);
    if(target && typeof target.focus === 'function') target.focus();
  }, 180);
}

function initWorkbenchViews(){
  let saved = '';
  try{ saved = localStorage.getItem(WORKBENCH_VIEW_KEY) || ''; }catch(e){}
  const first = document.querySelector(`[data-workbench-view="${saved}"]`) ? saved : 'mission-control';
  switchWorkbenchView(first);
}

function initCollapsiblePanels(){
  document.querySelectorAll('[data-collapse-key]').forEach(panel=>{
    const key = panel.getAttribute('data-collapse-key');
    let saved = '';
    try{ saved = localStorage.getItem(PANEL_STATE_PREFIX+key) || ''; }catch(e){}
    if(saved === 'collapsed') panel.classList.add('is-collapsed');
    if(saved === 'open') panel.classList.remove('is-collapsed');
    _syncPanelToggle(panel);
  });
}

function _syncPanelToggle(panel){
  const collapsed = panel.classList.contains('is-collapsed');
  const btn = panel.querySelector('.panel-toggle');
  if(btn){
    btn.textContent = collapsed ? '展开' : '收起';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }
}

// ---------- Worker 控制 ----------
let _workerRunning = false;
async function toggleWorker(){
  const btn = document.getElementById('btnWorker');
  btn.disabled = true;
  try{
    const ep = _workerRunning ? '/api/worker/stop' : '/api/worker/start';
    const r = await (await fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(!r.ok && r.error) toast('操作失败：'+r.error, false);
    else toast(_workerRunning?'Worker 已停止':'Worker 已启动');
  }catch(e){ toast('请求失败：'+e, false); }
  btn.disabled = false;
  await load();
}
function updateWorker(w){
  const elsewhere = !!(w&&w.running&&w.active_for_workspace===false);
  _workerRunning = !!(w&&w.running&&!elsewhere);
  const dot = document.getElementById('wDot');
  const lbl = document.getElementById('wLabel');
  const btn = document.getElementById('btnWorker');
  dot.className = 'wdot' + ((w&&w.running)?' on':'');
  lbl.textContent = elsewhere ? 'Worker 正在其他项目运行' : (_workerRunning ? 'Worker 运行中' : 'Worker 已停止');
  btn.textContent = elsewhere ? '先切回原项目' : (_workerRunning ? '停止' : '启动 Worker');
  btn.className = _workerRunning ? 'danger' : 'ghost';
  btn.disabled = elsewhere;
}

// ---------- 项目切换 ----------
let _wsList = [];
function pickWorkspace(i){
  if(_wsList[i]) document.getElementById('switchPath').value = _wsList[i];
}
async function openProjModal(){
  document.getElementById('projModal').style.display='block';
  document.getElementById('switchMsg').textContent='';
  try{
    const r = await (await fetch('/api/workspaces')).json();
    const el = document.getElementById('knownProjects');
    _wsList = (r.workspaces||[]);
    if(_wsList.length){
      el.innerHTML = _wsList.map((w,i)=>`<div onclick="pickWorkspace(${i})"
        style="cursor:pointer;padding:6px 8px;border-radius:5px;font-size:11px;color:var(--acc);border:1px solid transparent;"
        onmouseenter="this.style.borderColor='rgba(96,165,250,.3)'" onmouseleave="this.style.borderColor='transparent'">
        ${esc(w)}</div>`).join('');
    } else {
      el.innerHTML = '<div style="font-size:11px;color:var(--mut);">未找到其他工作空间（在兄弟目录和主目录中查找）</div>';
    }
  }catch(e){ _reportClientError('openProjModal failed: '+e.message, 'openProjModal', 0, e.stack); }
}
function closeProjModal(){ document.getElementById('projModal').style.display='none'; }
async function switchWorkspace(){
  const path = document.getElementById('switchPath').value.trim();
  const msg = document.getElementById('switchMsg');
  if(!path){ msg.textContent='请输入路径'; return; }
  try{
    const r = await (await fetch('/api/workspace/switch',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})).json();
    if(r.ok){
      closeProjModal();
      document.getElementById('fRepo').value = '';
      toast('✓ 已切换到：'+r.workspace);
      load();
    } else{ msg.style.color='var(--fail)'; msg.textContent='切换失败：'+(r.error||'未知'); }
  }catch(e){ msg.style.color='var(--fail)'; msg.textContent='请求失败：'+e; }
}

// ---------- 新建任务 ----------
const CORE_AGENTS = ['codex','claude-code','gemini'];
function _cheapBackendAgent(d){
  if(d && d.bugteam_available) return 'bugteam';
  if(d && d.mimo_available) return 'mimo';
  return '';
}
function _agentLabel(a){
  if(a==='bugteam') return 'bugteam（低成本）';
  if(a==='mimo') return 'mimo（兼容）';
  return a;
}
function fillAgents(d){
  const sel = document.getElementById('fAgent');
  const inst = d.installed_agents||[];
  const cur = sel.value;
  const detected = CORE_AGENTS.filter(a=>inst.includes(a));
  const others = inst.filter(a=>!CORE_AGENTS.includes(a));
  const rest = CORE_AGENTS.filter(a=>!inst.includes(a));
  let opts = [...detected, ...others, ...rest];
  const cheap = _cheapBackendAgent(d);
  if(cheap){ opts = [cheap, ...opts.filter(a=>a!==cheap)]; }
  sel.innerHTML = opts.map(a=>`<option value="${esc(a)}"${a===cur?' selected':''}>${esc(_agentLabel(a))}</option>`).join('');
  const repoEl = document.getElementById('fRepo');
  if(d.repo_root && !repoEl.value) repoEl.value = d.repo_root;
}

async function startMission(execute){
  const msg = document.getElementById('launchMsg');
  const intake = _mainIntakeState.payload;
  if(intake && !intake.already_clear){
    msg.textContent='先把上方目标对话补完，再预览或发送。';
    toast('目标还没收口，先继续对话', false);
    return;
  }
  if(intake && intake.suggested_goal){
    document.getElementById('fGoal').value = intake.suggested_goal;
  }
  const goal = document.getElementById('fGoal').value.trim();
  if(!goal){ msg.textContent='请先填写目标。'; return; }
  const repoRoot = document.getElementById('fRepo').value.trim() || _inferRepoFromGoal(goal) || '';
  const testCommand = document.getElementById('fTest').value.trim();
  const agent = document.getElementById('fAgent').value;
  const dispatchMode = document.getElementById('fDispatchMode').value || 'tracked';
  const intakeAnswers = (_mainIntakeState.answers || []).filter(Boolean);
  const intakeContract = intake ? {...intake, answers: intakeAnswers} : null;
  if(execute && !testCommand && !_isFieldVerificationGoal(goal) && !/review|plan|审查|计划/i.test(goal)){
    const fallbackQuestions = [
      '我还缺验收方案，不能直接派发。请直接补一条测试命令，比如 `python -m pytest -q`、`npm test`，或者说这是人工验收。',
      '如果你只是想先把任务说清楚，我也可以继续帮你收口成可执行合同。'
    ];
    msg.textContent = '先补验收方案';
    _appendMainChat('assistant', fallbackQuestions.join('\n'));
    document.getElementById('mainIntakeActions').style.display='flex';
    return;
  }
  const body = {
    goal,
    repo_root: repoRoot,
    test_command: testCommand,
    agent,
    dispatch_mode: dispatchMode,
    merge_policy: document.getElementById('fAutoMerge').checked ? 'auto' : 'manual',
    execute: !!execute,
    intake: intakeContract,
    answers: intakeAnswers,
    spec: {
      schema_version: 1,
      goal,
      scope: [{repo_root: repoRoot || '.', agent: agent || 'default', mode: execute ? 'execute' : 'preview'}],
      plan: [goal],
      test: [testCommand || 'auto-detect verification command'],
      risk: ['Workbench-generated spec; human review is required before merge.'],
      rollback: ['Keep merge policy manual unless explicitly approved.'],
      requirement_contract: intakeContract
    }
  };
  document.getElementById('btnPreview').disabled = true;
  document.getElementById('btnRun').disabled = true;
  msg.textContent = execute ? '派发中…' : '生成预览中…';
  _appendMainChat('system', `${execute ? '派发执行' : '生成预览'}：${goal}`);
  try{
    const r = await (await fetch('/api/mission/start',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.ok){
      msg.textContent = execute ? '✓ 已派发' : '✓ 预览生成中';
      _appendMainChat('system', `${execute ? '已派发执行' : '预览已创建'}：${r.launch_id || r.mission_id || '等待任务列表刷新'}`);
      document.getElementById('fGoal').value = '';
      toast(execute ? '✓ 任务已派发，在「执行中」列查看' : '✓ 预览生成中，在「待办」列查看');
    } else {
      msg.textContent = '错误：'+esc(r.error||'未知');
      _appendMainChat('system', '启动失败：'+(r.error||'未知'));
      if(String(r.error||'').includes('真实验收命令')){
        _appendMainChat('assistant', [
          '执行模式现在还不能直接派发，因为缺少验收方案。',
          '请直接回复测试命令，或者说这是人工验收，我会把它收口后再继续。'
        ].join('\n'));
        document.getElementById('mainIntakeActions').style.display='flex';
      }
    }
  }catch(e){ msg.textContent='请求失败：'+esc(String(e)); }
  finally{
    document.getElementById('btnPreview').disabled = false;
    document.getElementById('btnRun').disabled = false;
    load();
  }
}

// ---------- 任务确认对话（本地澄清，不消耗模型额度） ----------
let _intakeSeed = null;
let _intakeDraft = '';
let _mainIntakeState = { goal:'', answers:[], payload:null };

function openTaskIntake(seedGoal){
  switchWorkbenchView('mission-control', '#fGoal');
  const goal = String(seedGoal || document.getElementById('fGoal').value || '').trim();
  _intakeSeed = {
    goal,
    repo: document.getElementById('fRepo').value.trim(),
    test: document.getElementById('fTest').value.trim(),
    agent: document.getElementById('fAgent').value || 'codex'
  };
  _intakeDraft = _buildIntakeDraft(_intakeSeed, _answerTemplate(_intakeSeed.goal));
  document.getElementById('fGoal').value = goal;
  _resetMainChat();
  _appendMainChat('user', goal || '继续确认这个任务');
  _appendMainChat('assistant', [
    '我把这个任务收口到主对话区，不再打开单独的回填弹窗。',
    '',
    _buildIntakePrompt(_intakeSeed),
    '',
    '按默认假设生成的执行稿：',
    '',
    _intakeDraft
  ].join('\n'));
  document.getElementById('mainIntakeActions').style.display='flex';
  document.querySelector('.main-chat')?.scrollIntoView({behavior:'smooth', block:'start'});
  document.getElementById('fGoal').focus();
}

function _buildIntakePrompt(seed){
  const goal = seed.goal || '（还没有填写目标）';
  const questions = _intakeQuestions(goal, seed.repo, seed.test);
  return [
    '我先把这个任务确认成可执行合同。当前目标：',
    goal,
    '',
    '请直接按下面问题补充；如果默认假设正确，写“按默认继续”也可以。',
    '',
    ...questions.map((q,i)=>`${i+1}. ${q}`)
  ].join('\n');
}

function _intakeQuestions(goal, repo, testCommand){
  const text = String(goal||'').toLowerCase();
  if(_isFieldVerificationGoal(goal)){
    return [
      '真机设备、系统版本、测试账号和测试房间如何准备？',
      '弱网场景怎么制造或判定？例如 4G/5G、限速、丢包、地铁/电梯/户外移动。',
      '户外噪声场景怎么记录？例如街边、风噪、多人说话、耳机/扬声器。',
      '必须记录哪些指标？默认：接通率、首包/入会耗时、端到端延迟、断连/重连、主观语音质量、问题日志。',
      '是否只是评估是否切生产主链路？默认不改生产开关，只输出验收结论和建议。'
    ];
  }
  if(/手机|数据线|adb|apk|flutter|android|ios|安装|传输/.test(text)){
    return [
      '目标手机是 Android 还是 iOS？默认按 Android。',
      `项目目录是否是 ${repo || '当前工作空间父目录或 yuansi_app'}？`,
      '是否允许执行构建和安装命令？默认命令：flutter build apk --release，然后 adb devices，再 adb install -r build/app/outputs/flutter-apk/app-release.apk。',
      '手机是否已经插线、解锁，并打开 USB 调试？',
      '完成标准是什么？默认：APK 构建成功、adb 识别设备、安装成功、手机能打开 App。'
    ];
  }
  return [
    '要做什么？请用一个可观察的动作描述。',
    `改哪里或操作哪个项目目录？当前目录：${repo || '未指定'}`,
    `验收命令是什么？当前命令：${testCommand || '未指定，可让 Pacer 自动探测'}`,
    '什么叫完成？请写成可以检查的结果。',
    '是否允许修改代码、运行命令或安装到设备？'
  ];
}

function _answerTemplate(goal){
  const text = String(goal||'').toLowerCase();
  if(_isFieldVerificationGoal(goal)){
    return '按默认继续：使用真实手机设备进行 LiveKit 语音通话验收；覆盖基准网络、弱网和户外噪声场景；记录接通率、入会耗时、延迟、断连/重连、主观语音质量和问题日志；本次只输出是否建议切换生产主链路的结论，不直接改生产开关。';
  }
  if(/手机|数据线|adb|apk|flutter|android|ios|安装|传输/.test(text)){
    return '按默认继续：Android；项目目录 yuansi_app；允许构建 release APK 并用 adb install -r 安装；手机已插线、解锁、打开 USB 调试；完成标准是构建成功、adb 识别设备、安装成功并能打开 App。';
  }
  return '补充：\n1. \n2. \n3. \n4. \n5. ';
}

function _buildIntakeDraft(seed, answer){
  const goal = seed.goal || '执行当前任务';
  const repo = _inferRepoFromGoal(goal) || seed.repo;
  const test = seed.test || _inferTestCommand(goal);
  const commands = _inferCommands(goal);
  return [
    `目标：${_normalizeGoal(goal)}`,
    '',
    `项目目录：${repo || '当前工作空间父目录'}`,
    `编码 Agent：${seed.agent || 'codex'}`,
    '',
    '用户确认：',
    answer.trim() || '按默认假设继续。',
    '',
    '执行步骤：',
    ...commands.map((cmd,i)=>`${i+1}. ${cmd}`),
    '',
    _isFieldVerificationGoal(goal) ? '验收方案：' : '验收命令：',
    test || (_isFieldVerificationGoal(goal) ? '人工现场验收，按执行步骤记录证据。' : '自动探测或按执行步骤中的命令验收。'),
    '',
    '完成标准：',
    _inferDoneState(goal)
  ].join('\n');
}

function _normalizeGoal(goal){
  return String(goal||'').replace(/^吧/, '把').replace(/，+$/,'').trim() || '完成用户确认的任务';
}

function _inferRepoFromGoal(goal){
  const winPath = String(goal||'').match(/[A-Za-z]:\\[^\n\r"'，。；; ]+/);
  if(winPath) return winPath[0];
  if(/元思|轻语|yuansi/i.test(goal)) return 'yuansi_app';
  return '';
}

function _inferTestCommand(goal){
  if(_isFieldVerificationGoal(goal)) return '';
  if(/flutter|apk|android|手机|数据线|adb|安装|传输/i.test(goal)) return 'flutter build apk --release';
  return '';
}

function _inferCommands(goal){
  if(_isFieldVerificationGoal(goal)){
    return [
      '准备两台真实手机或一台手机加 Web/桌面端，登录测试账号并进入同一 LiveKit 语音房间。',
      '记录基准网络下的接通率、入会耗时、端到端延迟、断连/重连和主观语音质量。',
      '制造弱网场景并重复通话：限速/丢包/4G-5G 切换/移动网络波动，记录日志和可复现步骤。',
      '在户外噪声场景重复通话：街边、风噪、多人背景音，分别记录听感、降噪表现和失败样本。',
      '汇总证据，给出是否切换生产主链路的明确结论：通过、暂不切换、或需要补测。'
    ];
  }
  if(/flutter|apk|android|手机|数据线|adb|安装|传输/i.test(goal)){
    return [
      '进入项目目录并确认 Flutter SDK 可用：flutter --version。',
      '构建 Android release APK：flutter build apk --release。',
      '确认手机连接：adb devices。',
      '安装 APK：adb install -r build/app/outputs/flutter-apk/app-release.apk。',
      '记录构建、设备识别和安装结果。'
    ];
  }
  return [
    '确认目标、作用范围和验收标准。',
    '按最小范围完成改动或操作。',
    '运行验收命令并记录结果。'
  ];
}

function _inferDoneState(goal){
  if(_isFieldVerificationGoal(goal)){
    return '完成真实设备 LiveKit 语音通话基准、弱网和户外噪声测试；保存测试记录、问题日志和结论；明确是否建议切换生产主链路。';
  }
  if(/flutter|apk|android|手机|数据线|adb|安装|传输/i.test(goal)){
    return 'Flutter release APK 构建成功；adb devices 能看到目标设备；adb install -r 成功；手机上能看到并打开 App。';
  }
  return '用户确认的目标已完成，验收命令通过，相关结果可在工作台报告中查看。';
}

function _isFieldVerificationGoal(goal){
  return /livekit|真机|弱网|户外|噪声|语音通话|通话|生产主链路|人工验收|现场/i.test(String(goal||''));
}

function _shouldUseLocalIntake(goal){
  const text = String(goal || '').toLowerCase();
  return _isFieldVerificationGoal(goal) || /手机|数据线|adb|apk|flutter|android|ios|安装|传输/.test(text);
}

function mainChatKeyDown(e){
  if(e.key==='Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); sendMainChat(); }
}

function _resetMainChat(){
  const el = document.getElementById('mainChatMsgs');
  if(!el) return;
  _ensureIntakeLogOpen();
  el.innerHTML = '<div class="main-chat-empty">等待任务收口。</div>';
  document.getElementById('mainIntakeActions').style.display='none';
}

function _ensureIntakeLogOpen(){
  const log = document.getElementById('intakeLog');
  if(log) log.open = true;
}

function _renderTerminalLine(role, content){
  const now = fmtTime(new Date().toISOString());
  const label = role === 'user' ? 'C:\\Pacer>' : (role === 'system' ? 'pacer.sys>' : 'pacer.ai>');
  return `<div class="terminal-line ${role}">
    <span class="terminal-prefix">${esc(label)}</span>
    <span class="terminal-body">${esc(content)}</span>
    <span class="terminal-time">${esc(now)}</span>
  </div>`;
}

function _appendMainChat(role, content){
  _ensureIntakeLogOpen();
  const emptyEl = document.querySelector('#mainChatMsgs .main-chat-empty');
  if(emptyEl) emptyEl.remove();
  const el = document.getElementById('mainChatMsgs');
  el.insertAdjacentHTML('beforeend', _renderTerminalLine(role, content));
  el.scrollTop = el.scrollHeight;
}

async function sendMainChat(){
  const input = document.getElementById('fGoal');
  const msg = input.value.trim();
  if(!msg){ toast('先写一句你要 Pacer 做什么', false); return; }
  const repo = document.getElementById('fRepo').value.trim();
  const test = document.getElementById('fTest').value.trim();
  const agent = document.getElementById('fAgent').value || 'codex';
  const hasOpenIntake = !!(_mainIntakeState.goal && _mainIntakeState.payload && !_mainIntakeState.payload.already_clear);
  const activeGoal = hasOpenIntake ? _mainIntakeState.goal : msg;
  const answers = hasOpenIntake ? [...(_mainIntakeState.answers || []), msg] : [];
  if(!hasOpenIntake && _shouldUseLocalIntake(msg)){
    openTaskIntake(msg);
    return;
  }
  _appendMainChat('user', msg);
  input.value = '';
  _appendMainChat('assistant', `我先用当前选择的 ${agent} 做目标收口；如果它不可用，本轮只做本地问题整理。`);
  const btns = document.getElementById('mainIntakeActions');
  btns.style.display='none';
  try{
    const r = await (await fetch('/api/goal/refine',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({goal:activeGoal, answers, use_model:true, repo_root:repo, test_command:test, agent})
    })).json();
    if(!r.ok){
      _appendMainChat('assistant', '目标接待失败：'+(r.error||'未知'));
      return;
    }
    _mainIntakeState = {
      goal: activeGoal,
      answers,
      payload: r,
    };
    const dialogue = (r.dialogue_lines||[]).join('\n');
    _appendMainChat('assistant', [
      dialogue || '我已经把你的目标理顺了。',
      '',
      r.already_clear ? '这版目标已经足够清晰，可以直接派活。' : '你可以继续补一条，再让我收口成可执行任务。'
    ].join('\n'));
    if(r.already_clear){
      _intakeSeed = { goal: r.suggested_goal || activeGoal, repo, test, agent };
      _intakeDraft = _buildIntakeDraft(_intakeSeed, r.acceptance_hint || _answerTemplate(r.suggested_goal || activeGoal));
      document.getElementById('fGoal').value = r.suggested_goal || activeGoal;
      if(!_intakeSeed.test && test) document.getElementById('fTest').value = test;
      if(r.suggested_goal){
        document.getElementById('fGoal').value = r.suggested_goal;
      }
    } else {
      _intakeSeed = { goal: activeGoal, repo, test, agent };
      _intakeDraft = _buildIntakeDraft(_intakeSeed, r.acceptance_hint || _answerTemplate(activeGoal));
    }
    document.getElementById('mainIntakeActions').style.display='flex';
  }catch(e){
    _appendMainChat('assistant', '请求失败：'+e);
  }
}

function applyMainDraftToMission(){
  const payload = _mainIntakeState.payload;
  if(payload && payload.already_clear && payload.suggested_goal){
    document.getElementById('fGoal').value = payload.suggested_goal;
    if(_intakeSeed && _intakeSeed.repo) document.getElementById('fRepo').value = _intakeSeed.repo;
    toast('已采用当前收口结果，可以预览或发送');
    return;
  }
  if(!_intakeDraft) return toast('还没有执行稿，请先把目标对话说清楚', false);
  const repo = _intakeSeed && (_inferRepoFromGoal(_intakeSeed.goal) || _intakeSeed.repo);
  const test = _intakeSeed && (_intakeSeed.test || _inferTestCommand(_intakeSeed.goal));
  document.getElementById('fGoal').value = _intakeDraft;
  if(repo) document.getElementById('fRepo').value = repo;
  if(test) document.getElementById('fTest').value = test;
  toast('已采用执行稿，可以预览或发送');
}

// ---------- 合并 / 重试 ----------
async function mergeMission(ev, id){
  ev.stopPropagation();
  if(!confirm('确认把这个已验收任务合并到主分支？合并会直接修改当前项目的主分支。')) return;
  const btn = ev.target; btn.disabled=true; btn.textContent='合并中…';
  try{
    const r = await (await fetch('/api/mission/merge',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(r.ok) toast('✓ 已合并到主分支');
    else toast('合并失败：'+(r.error||(r.merge&&r.merge.reason)||'未知'), false);
  }catch(e){ toast('合并失败：'+e, false); }
  load();
}

async function retryMission(ev, id){
  ev.stopPropagation();
  if(!confirm('确认使用原仓库、目标和验收命令重新派发？重试会再次消耗模型额度。')) return;
  const btn = ev.target; btn.disabled=true; btn.textContent='重试中…';
  try{
    const r = await (await fetch('/api/mission/retry',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(r.ok){ closeDrawer(); toast('✓ 已重新派发'); }
    else{ toast('重试失败：'+(r.error||'未知'), false); btn.disabled=false; btn.textContent='↺ 重试'; }
  }catch(e){ toast('重试失败：'+e, false); btn.disabled=false; btn.textContent='↺ 重试'; }
  load();
}

async function loadMissionToComposer(ev, id){
  ev.stopPropagation();
  try{
    const d = await (await fetch('/api/mission?id='+encodeURIComponent(id))).json();
    const m = d.mission || {};
    const evd = d.pacer_evidence || {};
    document.getElementById('fGoal').value = m.objective || '';
    document.getElementById('fRepo').value = m.repo_root || '';
    document.getElementById('fTest').value = _userFacingTestCommand(m.test_command || evd.verification_command || '');
    if(m.agent) document.getElementById('fAgent').value = m.agent;
    if(m.dispatch_mode) document.getElementById('fDispatchMode').value = m.dispatch_mode;
    closeDrawer();
    switchWorkbenchView('mission-control', '#fGoal');
    toast('已填回任务合同，可以补验收命令后预览或派发');
  }catch(e){
    toast('填回失败：'+e, false);
  }
}

function _userFacingTestCommand(value){
  const text = String(value || '').trim();
  if(!text || /codex-check|checkpoint|visual_agent\.cli/i.test(text)) return '';
  return text;
}

async function deleteMission(ev, id){
  ev.stopPropagation();
  const btn = ev.target;
  if(!confirm('确认删除这条 mission？会从看板隐藏，但 mission 痕迹会保留。')) return;
  btn.disabled = true;
  btn.textContent = '删除中…';
  try{
    const r = await (await fetch('/api/mission/delete',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(r.ok) toast('✓ 已删除并归档');
    else toast('删除失败：'+(r.error||'未知'), false);
  }catch(e){ toast('删除失败：'+e, false); }
  load();
}

async function deleteAllMissions(ev){
  ev.stopPropagation();
  if(!confirm('确认删除当前看板里的所有非运行 mission？会从看板隐藏，但痕迹会保留。')) return;
  const btn = ev.target;
  btn.disabled = true;
  btn.textContent = '删除中';
  try{
    const r = await (await fetch('/api/mission/delete-all',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(r.ok) toast(`✓ 已删除 ${r.archived||0} 条任务${r.skipped_running?`，跳过 ${r.skipped_running} 条运行中`:''}`);
    else toast('全部删除失败：'+((r.errors&&r.errors[0]&&r.errors[0].error)||'未知'), false);
  }catch(e){ toast('全部删除失败：'+e, false); }
  finally{
    btn.disabled = false;
    btn.textContent = '清理非运行任务';
  }
  load();
}

// ---------- 看板 ----------
let _missionFilter = 'all';
const _openMissionDetails = new Set();

function setMissionFilter(filter){
  _missionFilter = filter || 'all';
  document.querySelectorAll('[data-mission-filter]').forEach(btn=>{
    btn.classList.toggle('active', btn.getAttribute('data-mission-filter') === _missionFilter);
  });
  load();
}

function _missionGroup(m){
  const status = String(m.status || '').toLowerCase();
  const column = String(m.board_column || '').toLowerCase();
  if(['running','preview_running','background_running','starting'].includes(status) || column === 'in_progress') return 'active';
  if(['verified','merged'].includes(status) || column === 'done') return 'done';
  if(['stopped','failed','blocked'].includes(status)) return 'blocked';
  return 'review';
}

function _updateMissionFilters(missions, launches){
  const counts = {all:0, active:0, review:0, done:0, blocked:0};
  const activeLaunches = (launches||[]).filter(l=>l.state==='starting'||l.state==='error');
  counts.all += activeLaunches.length + (missions||[]).length;
  activeLaunches.forEach(l=>{
    counts[l.state==='error' ? 'blocked' : 'active'] += 1;
  });
  (missions||[]).forEach(m=>{
    counts[_missionGroup(m)] += 1;
  });
  document.querySelectorAll('[data-mission-filter]').forEach(btn=>{
    const filter = btn.getAttribute('data-mission-filter') || 'all';
    btn.classList.toggle('active', filter === _missionFilter);
    const n = btn.querySelector('span');
    if(n) n.textContent = String(counts[filter] || 0);
  });
}

function toggleMissionDetails(ev, id){
  ev.stopPropagation();
  const details = ev.currentTarget && ev.currentTarget.closest ? ev.currentTarget.closest('details') : null;
  const willOpen = !(details && details.open);
  if(willOpen) _openMissionDetails.add(id);
  else _openMissionDetails.delete(id);
}

function missionCard(m){
  const sr = srInfo(m.stop_reason);
  const isStopped = m.status==='stopped'||m.status==='failed';
  const canDelete = !['running','background_running','preview_running','retrying','starting','verified','merged','archived'].includes(String(m.status||''));
  const eff = m.efficiency || {};
  const ev = m.pacer_evidence || {};
  const commandMode = String(m.verification_mode || ev.verification_mode || '').toLowerCase() === 'command';
  const hasStop = m.stop_reason && m.stop_reason!=='verified' && !(commandMode && m.stop_reason==='coverage_gap');
  const effLine = _efficiencyLine(eff);
  const activity = _activityBlock(m);
  const mergeBtn = m.can_merge
    ? `<button class="act-merge" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并</button>` : '';
  const retryBtn = isStopped && hasStop
    ? `<button class="act-retry" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 重试</button>` : '';
  const loadBtn = !['running','background_running','preview_running','retrying','starting','merged','archived'].includes(String(m.status||''))
    ? `<button class="act-retry" onclick="loadMissionToComposer(event,'${esc(m.mission_id)}')">填回</button>` : '';
  const deleteBtn = canDelete
    ? `<button class="act-delete" onclick="deleteMission(event,'${esc(m.mission_id)}')">删除</button>` : '';
  const clarifyBtn = (!commandMode && (m.stop_reason==='needs_clarification' || m.stop_reason==='coverage_gap'))
    ? `<button class="act-retry" onclick="event.stopPropagation();openTaskIntake('${jsq(m.objective||'')}')">继续确认</button>` : '';
  const stopTag = hasStop && !m.can_merge
    ? `<span class="stop-tag">${esc(sr.zh)}</span>` : '';
  const agent = m.agent ? `<span class="pill mut" style="font-size:10px;">${esc(m.agent)}</span>` : '';
  const cardTime = fmtTime(m.updated_at || m.created_at);
  const contract = _requirementContractBlock(m);
  const evidence = (ev.worker_status||ev.verification_verdict||ev.backend||ev.log_path)?`
    <div class="tcard-evidence">
      <div class="evi"><span>Worker</span><code>${esc(ev.worker_status||'-')}${ev.worker_exit_code!==undefined&&ev.worker_exit_code!==null?` exit ${esc(ev.worker_exit_code)}`:''}</code></div>
      <div class="evi"><span>后端 / 模型</span><code>${esc(ev.backend||ev.agent||'-')} ${esc(ev.model||'')}</code></div>
      <div class="evi"><span>验收</span><code>${esc(ev.verification_verdict||'-')}</code></div>
      <div class="evi"><span>日志</span><code>${esc(ev.log_path||ev.final_report||'-')}</code></div>
    </div>`:'';
  const detailState = _openMissionDetails.has(m.mission_id) ? ' open' : '';
  const folded = (contract || effLine || evidence) ? `<details class="tcard-details"${detailState}>
      <summary onclick="toggleMissionDetails(event,'${esc(m.mission_id)}')">合同 / 证据</summary>
      ${contract}
      ${effLine?`<div class="tcard-metrics">${effLine}</div>`:''}
      ${evidence}
    </details>` : '';
  return `<div class="tcard ${stClass(m.status)}" onclick="detail('${esc(m.mission_id)}')">
    <div class="tcard-goal">${esc(m.objective||'（无目标）')}</div>
    <div class="tcard-foot">
      <span class="pill ${pc(m.status)}">${esc(zh(m.status))}</span>
      ${agent}${stopTag}
      <span class="tcard-actions">${mergeBtn}${clarifyBtn}${retryBtn}${loadBtn}${deleteBtn}</span>
    </div>
    ${activity}
    <div class="tcard-meta">
      <span>${esc((m.mission_id||'').slice(0,17))}</span>
      ${cardTime?`<span>更新 ${esc(cardTime)}</span>`:''}
    </div>
    ${folded}
  </div>`;
}

function missionRow(m){
  const sr = srInfo(m.stop_reason);
  const status = String(m.status || '');
  const isStopped = status==='stopped'||status==='failed';
  const canDelete = !['running','background_running','preview_running','retrying','starting','verified','merged','archived'].includes(status);
  const eff = m.efficiency || {};
  const ev = m.pacer_evidence || {};
  const commandMode = String(m.verification_mode || ev.verification_mode || '').toLowerCase() === 'command';
  const hasStop = m.stop_reason && m.stop_reason!=='verified' && !(commandMode && m.stop_reason==='coverage_gap');
  const effLine = _efficiencyLine(eff);
  const activity = _activityBlock(m);
  const mergeBtn = m.can_merge ? `<button class="act-merge" onclick="mergeMission(event,'${esc(m.mission_id)}')">合并</button>` : '';
  const retryBtn = isStopped && hasStop ? `<button class="act-retry" onclick="retryMission(event,'${esc(m.mission_id)}')">重试</button>` : '';
  const loadBtn = !['running','background_running','preview_running','retrying','starting','merged','archived'].includes(status)
    ? `<button class="act-retry" onclick="loadMissionToComposer(event,'${esc(m.mission_id)}')">填回</button>` : '';
  const deleteBtn = canDelete ? `<button class="act-delete" onclick="deleteMission(event,'${esc(m.mission_id)}')">删除</button>` : '';
  const clarifyBtn = (!commandMode && (m.stop_reason==='needs_clarification' || m.stop_reason==='coverage_gap'))
    ? `<button class="act-retry" onclick="event.stopPropagation();openTaskIntake('${jsq(m.objective||'')}')">继续确认</button>` : '';
  const stopTag = hasStop && !m.can_merge ? `<span class="stop-tag">${esc(sr.zh)}</span>` : '';
  const contract = _requirementContractBlock(m);
  const evidence = (ev.worker_status||ev.verification_verdict||ev.backend||ev.log_path)?`
    <div class="tcard-evidence">
      <div class="evi"><span>Worker</span><code>${esc(ev.worker_status||'-')}${ev.worker_exit_code!==undefined&&ev.worker_exit_code!==null?` exit ${esc(ev.worker_exit_code)}`:''}</code></div>
      <div class="evi"><span>后端 / 模型</span><code>${esc(ev.backend||ev.agent||'-')} ${esc(ev.model||'')}</code></div>
      <div class="evi"><span>验收</span><code>${esc(ev.verification_verdict||'-')}</code></div>
      <div class="evi"><span>日志</span><code>${esc(ev.log_path||ev.final_report||'-')}</code></div>
    </div>`:'';
  const detailState = _openMissionDetails.has(m.mission_id) ? ' open' : '';
  const folded = (contract || effLine || evidence) ? `<details class="tcard-details"${detailState}>
      <summary onclick="toggleMissionDetails(event,'${esc(m.mission_id)}')">合同 / 证据</summary>
      ${contract}
      ${effLine?`<div class="tcard-metrics">${effLine}</div>`:''}
      ${evidence}
    </details>` : '';
  return `<div class="mission-row ${stClass(status)}" onclick="detail('${esc(m.mission_id)}')">
    <div class="mission-row-main">
      <div class="mission-row-title">${esc(m.objective||'（无目标）')}</div>
      ${activity}
      ${folded}
    </div>
    <div class="mission-row-state">
      <div class="mission-row-meta">
        <span class="pill ${pc(status)}">${esc(zh(status))}</span>
        ${m.agent?`<span class="pill mut">${esc(m.agent)}</span>`:''}
        ${stopTag}
        ${fmtTime(m.updated_at||m.created_at)?`<span>更新 ${esc(fmtTime(m.updated_at||m.created_at))}</span>`:''}
      </div>
      <span class="mission-id">${esc((m.mission_id||'').slice(0,17))}</span>
    </div>
    <div class="mission-row-actions">${mergeBtn}${clarifyBtn}${retryBtn}${loadBtn}${deleteBtn}</div>
  </div>`;
}

function _activityBlock(m){
  const ev = (m && m.pacer_evidence && typeof m.pacer_evidence === 'object') ? m.pacer_evidence : {};
  const progress = (ev.progress && typeof ev.progress === 'object') ? ev.progress : {};
  const activity = String(m?.activity || ev.activity || progress.activity || '').trim();
  const label = String(m?.activity_label || ev.activity_label || progress.activity_label || '').trim();
  if(!activity && !label) return '';
  const elapsedSource = [m?.activity_elapsed_seconds, ev.activity_elapsed_seconds, progress.activity_elapsed_seconds]
    .find(v => v !== undefined && v !== null && v !== '');
  const elapsed = Number(elapsedSource);
  const hasElapsed = Number.isFinite(elapsed);
  const command = String(m?.activity_command || ev.activity_command || progress.activity_command || '').trim();
  const risk = hasElapsed && elapsed >= 600 && (activity === 'dependency_install' || activity === 'tests_running');
  const title = command ? ` title="${esc(command)}"` : '';
  return `<div class="activity-line${risk?' risk':''}"${title}><span>当前动作</span><b>${esc(label||activity)}</b>${hasElapsed?`<code>${esc(_fmtSeconds(elapsed))}</code>`:''}</div>`;
}

function _efficiencyLine(eff){
  const runs = Number(eff.mimo_runs || eff.worker_runs || 0);
  const taskSec = Number(eff.actual_task_seconds || 0);
  const workerSec = Number(eff.actual_worker_seconds || 0);
  const cost = eff.actual_cost_available ? ('$'+Number(eff.actual_cost_usd || 0).toFixed(4)) : '未回传';
  const quota = _quotaSavedPercent(eff);
  if(!runs && !taskSec && !workerSec) return '';
  return [
    `<span>后端 ${runs}</span>`,
    `<span>套餐额度 ${quota.toFixed(1)}%</span>`,
    `<span>实际额度 ${cost}</span>`,
    `<span>任务耗时 ${_fmtSeconds(taskSec)}</span>`,
    `<span>Worker ${_fmtSeconds(workerSec)}</span>`
  ].join('');
}

function _requirementContractBlock(m){
  const rc = (m && m.requirement_contract && typeof m.requirement_contract === 'object') ? m.requirement_contract : {};
  const finalGoal = String(rc.final_goal || rc.suggested_goal || '').trim();
  const inputGoal = String(rc.input_goal || '').trim();
  const acceptance = String(rc.acceptance_hint || '').trim();
  const policy = _intakePolicyLabel(rc);
  const answers = Array.isArray(rc.answers) ? rc.answers.map(x=>String(x||'').trim()).filter(Boolean).slice(0,3).join('；') : '';
  const rows = [];
  if(inputGoal && inputGoal !== finalGoal) rows.push(['原始', inputGoal]);
  if(finalGoal && finalGoal !== String(m.objective||'').trim()) rows.push(['目标', finalGoal]);
  if(acceptance) rows.push(['验收', acceptance]);
  if(policy) rows.push(['收口', policy]);
  if(answers) rows.push(['补充', answers]);
  if(m.repo_root) rows.push(['目录', m.repo_root]);
  if(m.test_command) rows.push(['命令', m.test_command]);
  if(!rows.length) return '';
  return `<div class="tcard-contract">${rows.map(([label,value])=>
    `<div><span>${esc(label)}</span><code title="${esc(value)}">${esc(value)}</code></div>`
  ).join('')}</div>`;
}

function _requirementContractDetail(m){
  const rc = (m && m.requirement_contract && typeof m.requirement_contract === 'object') ? m.requirement_contract : {};
  const rows = [];
  const inputGoal = String(rc.input_goal || '').trim();
  const finalGoal = String(rc.final_goal || rc.suggested_goal || '').trim();
  const acceptance = String(rc.acceptance_hint || '').trim();
  const policy = _intakePolicyLabel(rc);
  const model = String(rc.model_id || '').trim();
  const answers = Array.isArray(rc.answers) ? rc.answers.map(x=>String(x||'').trim()).filter(Boolean).slice(0,8) : [];
  const questions = Array.isArray(rc.clarifying_questions) ? rc.clarifying_questions.map(x=>String(x||'').trim()).filter(Boolean).slice(0,6) : [];
  if(inputGoal) rows.push(['原始输入', inputGoal]);
  if(finalGoal) rows.push(['最终目标', finalGoal]);
  if(acceptance) rows.push(['验收提示', acceptance]);
  if(policy) rows.push(['收口方式', policy]);
  if(model) rows.push(['模型/Agent', model]);
  if(answers.length) rows.push(['用户补充', answers.join('\n')]);
  if(questions.length) rows.push(['澄清问题', questions.join('\n')]);
  if(m.repo_root) rows.push(['项目目录', m.repo_root]);
  if(m.test_command) rows.push(['验收命令', m.test_command]);
  if(!rows.length) return '';
  return `<h3>需求合同</h3><div class="detail-contract">${rows.map(([label,value])=>
    `<div class="detail-contract-row"><span>${esc(label)}</span><code>${esc(value)}</code></div>`
  ).join('')}</div>`;
}

function _intakePolicyLabel(rc){
  if(!rc || typeof rc !== 'object') return '';
  const policy = String(rc.intake_policy || '').trim();
  const source = String(rc.source || '').trim();
  const labels = {
    selected_agent_cli: '选中 Agent CLI 收口',
    selected_agent_model: '选中 Agent 模型收口',
    selected_agent_unavailable: '选中 Agent 不可用，本地整理',
    auto_backend: '自动后端收口',
    local_rules: '本地规则收口'
  };
  if(policy && labels[policy]) return labels[policy];
  if(policy) return policy;
  if(source === 'selected_agent_cli') return labels.selected_agent_cli;
  if(source === 'deterministic') return labels.local_rules;
  if(source) return source;
  return '';
}

function _fmtSeconds(seconds){
  const n = Number(seconds || 0);
  if(!n) return '0s';
  if(n < 60) return n.toFixed(n < 10 ? 1 : 0) + 's';
  return (n/60).toFixed(1) + 'm';
}

function _quotaSavedPercent(eff){
  const direct = Number(eff?.saved_quota_percent);
  if(Number.isFinite(direct) && direct > 0) return direct;
  const saved = Number(eff?.saved_usd || 0);
  const spent = Number(eff?.spent_usd || 0);
  if(saved <= 0 && spent <= 0) return 0;
  return Math.round((saved / Math.max(saved + spent, 0.01) * 100) * 10) / 10;
}

function renderTrace(d){
  const traces = (d.work_traces||[]).slice(0, 12);
  const el = document.getElementById('traceStream');
  document.getElementById('traceCount').textContent = String(traces.length);
  if(!traces.length){
    el.innerHTML = '<div class="empty">暂无工作痕迹。创建 mission 后，这里会显示 launch、worker、日志、报告、验收和队列状态。</div>';
    return;
  }
  el.innerHTML = traces.map(t=>{
    const meta = t.meta || {};
    const mid = meta.mission_id || '';
    const clickable = mid ? ` onclick="detail('${esc(mid)}')"` : '';
    const title = esc(t.title || '未命名痕迹');
    const detailText = t.detail ? `<div class="trace-detail">${esc(t.detail)}</div>` : '';
    const path = t.path ? `<code title="${esc(t.path)}">${esc(t.path)}</code>` : '';
    const time = fmtTime(t.timestamp || meta.updated_at || meta.created_at || meta.recorded_at || meta.saved_at);
    return `<div class="trace-item ${mid?'clickable':''}"${clickable}>
      <div class="trace-main">
        <span class="pill ${pc(t.status)}">${esc(t.kind || 'trace')} · ${esc(t.status || 'unknown')}</span>
        <span class="trace-title">${title}</span>
        ${time?`<span class="trace-time">${esc(time)}</span>`:''}
      </div>
      ${detailText}
      ${path?`<div class="trace-path">${path}</div>`:''}
    </div>`;
  }).join('');
}

// ---------- Pacer 可观测性 ----------
const OBSERVABILITY_API = '/api/observability';
const _obsState = {
  loaded:false,
  loading:false,
  launches:[],
  launchId:'',
  sessionId:'',
  detail:null,
  timeline:[],
  nextCursor:null,
  timelineTotal:0,
  detailRequest:0,
  timelineRequest:0,
};

function _obsRecord(value){
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function _obsArray(payload, keys){
  if(Array.isArray(payload)) return payload;
  const source = _obsRecord(payload);
  for(const key of keys){
    if(Array.isArray(source[key])) return source[key];
  }
  return [];
}

function _obsNumber(source, keys, fallback=0){
  const obj = _obsRecord(source);
  for(const key of keys){
    if(obj[key] === '' || obj[key] === null || obj[key] === undefined) continue;
    const value = Number(obj[key]);
    if(Number.isFinite(value)) return Math.max(0, value);
  }
  return fallback;
}

function _obsHasTokenFields(value){
  if(typeof value === 'number') return Number.isFinite(value);
  const source = _obsRecord(value);
  return [
    'input_tokens','cached_input_tokens','cache_read_input_tokens','uncached_input_tokens',
    'output_tokens','reasoning_output_tokens','reasoning_tokens','total_tokens','total',
  ].some(key=>source[key] !== null && source[key] !== undefined && Number.isFinite(Number(source[key])));
}

function _obsUsage(value){
  const usage = _obsRecord(value);
  const rawValue = usage.raw_ledger ?? usage.raw_usage ?? usage.raw;
  const raw = _obsRecord(rawValue);
  const actualValue = usage.deduplicated_actual ?? usage.actual_added ?? usage.actual_usage ?? usage.actual;
  const actual = _obsRecord(actualValue && typeof actualValue === 'object' ? actualValue : usage);
  const rawAvailable = _obsHasTokenFields(rawValue) || _obsHasTokenFields({
    total_tokens:usage.raw_total_tokens,
    input_tokens:usage.raw_input_tokens,
    output_tokens:usage.raw_output_tokens,
  });
  const actualAvailable = _obsHasTokenFields(actualValue) ||
    (actualValue === undefined && _obsHasTokenFields(usage));

  const rawInput = _obsNumber(raw, ['input_tokens','input'], _obsNumber(usage, ['raw_input_tokens'], 0));
  const rawCached = _obsNumber(raw, ['cached_input_tokens','cache_read_input_tokens','cached_tokens'], _obsNumber(usage, ['raw_cached_input_tokens'], 0));
  const rawOutput = _obsNumber(raw, ['output_tokens','output'], _obsNumber(usage, ['raw_output_tokens'], 0));
  const rawReasoning = _obsNumber(raw, ['reasoning_output_tokens','reasoning_tokens'], _obsNumber(usage, ['raw_reasoning_tokens'], 0));
  const rawTotal = _obsNumber(raw, ['total_tokens','total'], _obsNumber(usage, ['raw_total_tokens'], rawInput + rawOutput));

  const input = _obsNumber(actual, ['input_tokens','input'], _obsNumber(usage, ['actual_input_tokens'], 0));
  const cached = _obsNumber(actual, ['cached_input_tokens','cache_read_input_tokens','cached_tokens'], _obsNumber(usage, ['actual_cached_input_tokens'], 0));
  const uncached = _obsNumber(usage, ['uncached_input_tokens'], _obsNumber(actual, ['uncached_input_tokens','uncached_tokens'], Math.max(0, input - cached)));
  const output = _obsNumber(actual, ['output_tokens','output'], _obsNumber(usage, ['actual_output_tokens'], 0));
  const reasoning = _obsNumber(actual, ['reasoning_output_tokens','reasoning_tokens'], _obsNumber(usage, ['reasoning_output_tokens','reasoning_tokens'], 0));
  const scalarActual = Number(actualValue);
  const total = Number.isFinite(scalarActual)
    ? Math.max(0, scalarActual)
    : _obsNumber(actual, ['total_tokens','total'], _obsNumber(usage, ['actual_added_tokens','deduplicated_total_tokens'], input + output));

  return {rawAvailable,actualAvailable,rawInput,rawCached,rawOutput,rawReasoning,rawTotal,input,cached,uncached,output,reasoning,total};
}

function _obsToken(value){
  const number = Number(value || 0);
  return Number.isFinite(number) ? Math.max(0, Math.round(number)).toLocaleString('zh-CN') : '0';
}

function _obsPercent(value){
  const number = Number(value);
  if(!Number.isFinite(number)) return '未采集';
  const percent = number >= 0 && number <= 1 ? number * 100 : number;
  return `${Math.max(0, Math.min(100, percent)).toFixed(1)}%`;
}

function _obsConfidence(value){
  const text = String(value ?? '').trim();
  if(!text) return '未采集';
  return Number.isFinite(Number(text)) ? _obsPercent(Number(text)) : text;
}

function _obsDuration(value, startedAt, completedAt){
  let ms = Number(value);
  if(!Number.isFinite(ms) && startedAt && completedAt){
    const start = new Date(startedAt).getTime();
    const end = new Date(completedAt).getTime();
    if(Number.isFinite(start) && Number.isFinite(end)) ms = Math.max(0, end - start);
  }
  if(!Number.isFinite(ms) || ms < 0) return '';
  if(ms < 1000) return `${Math.round(ms)} ms`;
  if(ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function _obsStatusLevel(status){
  const value = String(status || '').toLowerCase();
  if(value.includes('fail') || value.includes('error') || value.includes('blocked')) return 'fail';
  if(value.includes('running') || value.includes('pending') || value.includes('start')) return 'running';
  if(value.includes('warn') || value.includes('compact') || value.includes('partial')) return 'warn';
  if(value.includes('pass') || value.includes('complete') || value.includes('success') || value.includes('verified')) return 'ok';
  return 'idle';
}

function _setObsStatus(text, level='idle'){
  const el = document.getElementById('obsStatus');
  if(!el) return;
  el.textContent = text;
  el.className = `obs-status is-${level}`;
}

function _setObsLoadState(text, level='idle', visible=true){
  const el = document.getElementById('obsLoadState');
  if(!el) return;
  el.textContent = text;
  el.dataset.level = level;
  el.hidden = !visible;
}

function _obsErrorText(error, subject){
  if(error && error.status === 404) return `${subject} API 尚未启用，其他工作台功能不受影响。`;
  if(error && error.status) return `${subject}加载失败（HTTP ${error.status}）。`;
  return `${subject}加载失败，请检查 Dashboard 服务状态。`;
}

async function _obsFetch(path){
  const response = await fetch(path, {headers:{Accept:'application/json'}, cache:'no-store'});
  let payload = null;
  try{ payload = await response.json(); }catch(e){ payload = null; }
  if(!response.ok || (payload && payload.ok === false)){
    const error = new Error('observability request failed');
    error.status = response.status || 500;
    throw error;
  }
  return payload || {};
}

function _obsLaunchId(launch){
  const item = _obsRecord(launch);
  return String(item.launch_id || item.id || '').trim();
}

function _obsSessionId(session){
  const item = _obsRecord(session);
  return String(item.session_id || item.id || '').trim();
}

function _obsShortId(value, size=14){
  const text = String(value || '');
  if(text.length <= size + 5) return text;
  return `${text.slice(0, size)}...${text.slice(-4)}`;
}

function _obsLaunchOption(launch){
  const item = _obsRecord(launch);
  const id = _obsLaunchId(item);
  const runtime = _obsRecord(item.runtime);
  const stamp = fmtTime(item.started_at || item.created_at || item.timestamp);
  const parts = [_obsShortId(id), item.status || 'unknown', runtime.model || '', stamp].filter(Boolean);
  return parts.join(' · ');
}

function _fillObsLaunches(launches, selectedId){
  const select = document.getElementById('obsLaunchSelect');
  if(!select) return;
  select.innerHTML = launches.map(item=>{
    const id = _obsLaunchId(item);
    return `<option value="${esc(id)}"${id===selectedId?' selected':''}>${esc(_obsLaunchOption(item))}</option>`;
  }).join('');
  select.disabled = !launches.length;
}

function _fillObsSessions(sessions, selectedId){
  const select = document.getElementById('obsSessionSelect');
  if(!select) return;
  if(!sessions.length){
    select.innerHTML = '<option value="">该 launch 暂无 session</option>';
    select.disabled = true;
    return;
  }
  select.innerHTML = sessions.map((session,index)=>{
    const item = _obsRecord(session);
    const id = _obsSessionId(item);
    const label = item.role || item.agent || item.name || `Session ${index + 1}`;
    const count = Number(item.turn_count || 0);
    return `<option value="${esc(id)}"${id===selectedId?' selected':''}>${esc(label)} · ${esc(_obsShortId(id))} · ${count} 轮</option>`;
  }).join('');
  select.disabled = false;
}

function _renderObsLaunch(detail){
  const payload = _obsRecord(detail);
  const summary = _obsState.launches.find(item=>_obsLaunchId(item) === _obsState.launchId) || {};
  const launch = {..._obsRecord(summary), ..._obsRecord(payload.launch || payload)};
  const runtime = _obsRecord(launch.runtime);
  const usageSource = launch.usage || payload.usage || {};
  const usage = _obsUsage(usageSource);

  setText('obsLedgerTitle', _obsShortId(_obsState.launchId, 24) || '未命名 launch');
  setText('obsActualAdded', usage.actualAvailable ? _obsToken(usage.total) : '未索引');
  setText('obsRawLedger', usage.rawAvailable ? _obsToken(usage.rawTotal) : '未索引');
  setText('obsCached', usage.actualAvailable ? _obsToken(usage.cached) : '未索引');
  setText('obsUncached', usage.actualAvailable ? _obsToken(usage.uncached) : '未索引');
  setText('obsOutput', usage.actualAvailable ? _obsToken(usage.output) : '未索引');
  setText('obsReasoning', usage.actualAvailable ? _obsToken(usage.reasoning) : '未索引');

  const status = String(launch.status || 'unknown');
  const started = fmtTime(launch.started_at || launch.created_at);
  const completed = fmtTime(launch.completed_at || launch.updated_at);
  const runtimeBits = [
    `<span class="obs-runtime-state is-${_obsStatusLevel(status)}">${esc(status)}</span>`,
    launch.project_name ? `<span>${esc(launch.project_name)}</span>` : '',
    runtime.provider ? `<span>${esc(runtime.provider)}</span>` : '',
    runtime.model ? `<span>${esc(runtime.model)}</span>` : '',
    runtime.reasoning_effort ? `<span>reasoning ${esc(runtime.reasoning_effort)}</span>` : '',
    started ? `<span>${esc(started)}${completed ? ` - ${esc(completed)}` : ''}</span>` : '',
  ].filter(Boolean);
  document.getElementById('obsRuntimeMeta').innerHTML = runtimeBits.join('<i></i>');

  const usageMeta = _obsRecord(usageSource);
  const metaBits = [
    `缓存率 ${_obsPercent(usageMeta.cache_ratio)}`,
    `归因置信度 ${_obsConfidence(launch.attribution_confidence)}`,
    `${Number(launch.session_count || (payload.sessions||[]).length || 0)} sessions`,
    `${Number(launch.agent_count || (payload.agents||[]).length || 0)} agents`,
    `${Number(launch.compaction_count || 0)} compactions`,
    'Reasoning 已包含于 Output',
  ];
  document.getElementById('obsUsageMeta').innerHTML = metaBits.map(bit=>`<span>${esc(bit)}</span>`).join('');
  document.getElementById('obsContent').hidden = false;
  _renderObsAgents(payload);
  _renderObsEvidence();
}

function _obsAgentRows(detail){
  const payload = _obsRecord(detail);
  const source = _obsArray(payload, ['agents']);
  const rows = source.length ? source : _obsArray(payload, ['sessions']);
  const normalized = rows.map((value,index)=>{
    const item = _obsRecord(value);
    const id = String(item.agent_id || item.session_id || item.id || `agent-${index + 1}`);
    const parent = String(item.parent_agent_id || item.parent_session_id || item.parent_id || '');
    return {item,id,parent,index};
  });
  const byId = new Map(normalized.map(row=>[row.id,row]));
  const children = new Map();
  normalized.forEach(row=>{
    const parent = byId.has(row.parent) ? row.parent : '';
    if(!children.has(parent)) children.set(parent, []);
    children.get(parent).push(row);
  });
  const ordered = [];
  const visited = new Set();
  function visit(row, depth){
    if(visited.has(row.id)) return;
    visited.add(row.id);
    ordered.push({...row,depth:Math.min(depth, 6)});
    (children.get(row.id) || []).forEach(child=>visit(child, depth + 1));
  }
  (children.get('') || []).forEach(row=>visit(row, 0));
  normalized.forEach(row=>visit(row, 0));
  return ordered;
}

function _renderObsAgents(detail){
  const rows = _obsAgentRows(detail);
  setText('obsAgentCount', `${rows.length} 个`);
  const el = document.getElementById('obsAgentTree');
  if(!rows.length){
    el.innerHTML = '<div class="obs-inline-state">该 launch 暂无 agent 归因数据。</div>';
    return;
  }
  el.innerHTML = rows.map(row=>{
    const item = row.item;
    const usage = _obsUsage(item.usage || {});
    const name = item.role || item.agent || item.name || (row.depth ? 'Child agent' : 'Root agent');
    const duration = _obsDuration(item.duration_ms, item.started_at, item.completed_at);
    const counts = [
      item.turn_count !== undefined ? `${Number(item.turn_count || 0)} 轮` : '',
      item.tool_count !== undefined ? `${Number(item.tool_count || 0)} tools` : '',
      usage.total ? `${_obsToken(usage.total)} tokens` : '',
      duration,
    ].filter(Boolean).join(' · ');
    const status = String(item.status || 'unknown');
    return `<div class="obs-agent-row" style="--depth:${row.depth}">
      <span class="obs-agent-branch" aria-hidden="true"></span>
      <div class="obs-agent-main"><b>${esc(name)}</b><code title="${esc(row.id)}">${esc(_obsShortId(row.id,18))}</code></div>
      <span class="obs-event-status is-${_obsStatusLevel(status)}">${esc(status)}</span>
      <small>${esc(counts || '暂无用量')}</small>
    </div>`;
  }).join('');
}

function _obsEventKey(event, index){
  const item = _obsRecord(event);
  return String(item.event_id || item.id || `${item.timestamp||''}|${item.kind||''}|${item.label||''}|${index}`);
}

function _obsHasUsage(event){
  const item = _obsRecord(event);
  if(!item.usage_delta && !item.usage) return false;
  const usage = _obsUsage(item.usage_delta || item.usage);
  return [usage.total,usage.rawTotal,usage.cached,usage.uncached,usage.output,usage.reasoning].some(value=>value > 0);
}

function _obsStack(usage){
  const total = Math.max(usage.cached + usage.uncached + usage.output, 1);
  const cached = usage.cached / total * 100;
  const uncached = usage.uncached / total * 100;
  const output = usage.output / total * 100;
  const reasoning = usage.output > 0 ? Math.min(100, usage.reasoning / usage.output * 100) : 0;
  const label = `Cached ${_obsToken(usage.cached)}，Uncached ${_obsToken(usage.uncached)}，Output ${_obsToken(usage.output)}，其中 Reasoning ${_obsToken(usage.reasoning)}`;
  return `<div class="obs-token-stack" aria-label="${esc(label)}" title="${esc(label)}">
    <span class="cached" style="width:${cached.toFixed(3)}%"></span>
    <span class="uncached" style="width:${uncached.toFixed(3)}%"></span>
    <span class="output" style="width:${output.toFixed(3)}%"><i style="width:${reasoning.toFixed(3)}%"></i></span>
  </div>`;
}

function _renderObsTurns(){
  const events = _obsState.timeline.filter(_obsHasUsage);
  setText('obsTurnCount', `${events.length} 轮`);
  const body = document.getElementById('obsTurnsBody');
  const state = document.getElementById('obsTurnsState');
  if(!events.length){
    body.innerHTML = '';
    state.hidden = false;
    state.textContent = _obsState.sessionId ? '该 session 暂无逐轮 token 增量。' : '选择 session 后显示逐轮 token。';
    return;
  }
  state.hidden = true;
  body.innerHTML = events.map((value,index)=>{
    const event = _obsRecord(value);
    const usage = _obsUsage(event.usage_delta || event.usage);
    const label = event.label || event.kind || `Turn ${index + 1}`;
    return `<tr>
      <td><b>${index + 1}</b><span title="${esc(label)}">${esc(String(label).slice(0,64))}</span></td>
      <td>${esc(fmtTime(event.timestamp) || '-')}</td>
      <td>${usage.actualAvailable?_obsToken(usage.total):'-'}</td>
      <td>${usage.rawAvailable?_obsToken(usage.rawTotal):'-'}</td>
      <td>${usage.actualAvailable?_obsToken(usage.cached):'-'}</td>
      <td>${usage.actualAvailable?_obsToken(usage.uncached):'-'}</td>
      <td>${usage.actualAvailable?_obsToken(usage.output):'-'}</td>
      <td>${usage.actualAvailable?_obsToken(usage.reasoning):'-'}</td>
      <td>${_obsStack(usage)}</td>
    </tr>`;
  }).join('');
}

function _obsIsToolEvent(event){
  const item = _obsRecord(event);
  const kind = String(item.kind || item.type || '').toLowerCase();
  const label = String(item.label || '').toLowerCase();
  return kind.includes('tool') || kind.includes('mcp') || kind.includes('command') || label.startsWith('mcp__');
}

function _obsRedactText(value){
  let text = String(value ?? '');
  text = text.replace(/[A-Za-z]:[\\/](?:[^\\/\s"'<>]+[\\/])*[^\\/\s"'<>]*/g, '[路径已隐藏]');
  return text;
}

function _obsSafePreview(value){
  if(typeof value === 'string') return _obsRedactText(value).slice(0, 4000);
  try{
    return _obsRedactText(JSON.stringify(value, (key,item)=>{
      if(/^(repo_root|path|cwd|prompt|response|raw_prompt|raw_response)$/i.test(key)) return '[已隐藏]';
      return item;
    }, 2)).slice(0, 4000);
  }catch(e){ return '安全预览不可解析。'; }
}

function _obsPreviewBlock(event){
  const item = _obsRecord(event);
  if(item.safe_preview === undefined || item.safe_preview === null || item.safe_preview === '') return '';
  return `<div class="obs-preview-wrap">
    <button class="obs-preview-toggle" onclick="toggleObservabilityPreview(this)" aria-expanded="false">展开安全预览</button>
    <pre class="obs-safe-preview" hidden>${esc(_obsSafePreview(item.safe_preview))}</pre>
  </div>`;
}

function toggleObservabilityPreview(button){
  if(!button || !button.parentElement) return;
  const preview = button.parentElement.querySelector('.obs-safe-preview');
  if(!preview) return;
  const opening = preview.hidden;
  preview.hidden = !opening;
  button.textContent = opening ? '收起安全预览' : '展开安全预览';
  button.setAttribute('aria-expanded', opening ? 'true' : 'false');
}

function _renderObsTools(message=''){
  const events = _obsState.timeline.filter(_obsIsToolEvent);
  setText('obsToolCount', `${events.length} 条`);
  const el = document.getElementById('obsToolTimeline');
  if(!events.length){
    el.innerHTML = `<div class="obs-inline-state">${esc(message || (_obsState.sessionId ? '该 session 暂无 Tool / MCP 事件。' : '选择 session 后显示 Tool / MCP 时间线。'))}</div>`;
  }else{
    el.innerHTML = events.map(value=>{
      const event = _obsRecord(value);
      const kind = String(event.kind || 'tool');
      const status = String(event.status || 'unknown');
      const duration = _obsDuration(event.duration_ms, event.started_at, event.completed_at);
      const label = String(event.label || '未命名调用').slice(0, 120);
      return `<div class="obs-timeline-item">
        <time>${esc(fmtTime(event.timestamp) || '-')}</time>
        <div class="obs-timeline-body">
          <div class="obs-event-line"><span class="obs-event-kind">${esc(kind)}</span><b title="${esc(label)}">${esc(label)}</b><span class="obs-event-status is-${_obsStatusLevel(status)}">${esc(status)}</span></div>
          ${duration?`<small>${esc(duration)}</small>`:''}
          ${_obsPreviewBlock(event)}
        </div>
      </div>`;
    }).join('');
  }
  const more = document.getElementById('obsLoadMore');
  more.hidden = _obsState.nextCursor === null || _obsState.nextCursor === undefined || _obsState.nextCursor === '';
  more.disabled = false;
}

function _obsEvidenceCategory(kind){
  const value = String(kind || '').toLowerCase();
  if(value.includes('compact')) return 'compaction';
  if(value.includes('memory') || value.includes('receipt') || value.includes('recovery')) return 'memory';
  if(value.includes('verif') || value.includes('test') || value.includes('check') || value.includes('acceptance')) return 'verification';
  if(value.includes('outcome') || value.includes('complete') || value.includes('result') ||
     value.includes('managed') || value.includes('dogfood') || value.includes('routing')) return 'outcome';
  return '';
}

function _obsEvidenceItems(){
  const detail = _obsRecord(_obsState.detail);
  const items = _obsArray(detail, ['evidence']).slice();
  const launch = _obsRecord(detail.launch);
  const compactionCount = Number(launch.compaction_count || 0);
  if(compactionCount > 0 && !items.some(item=>_obsEvidenceCategory(_obsRecord(item).kind) === 'compaction')){
    items.push({kind:'compaction',status:'recorded',label:`观测到 ${compactionCount} 次 compaction`,timestamp:launch.completed_at || launch.started_at});
  }
  if(launch.status && !items.some(item=>String(_obsRecord(item).kind || '').toLowerCase().includes('outcome'))){
    items.push({kind:'outcome',status:launch.status,label:`Launch 终态：${launch.status}`,timestamp:launch.completed_at || launch.started_at});
  }
  _obsState.timeline.forEach(event=>{
    if(_obsEvidenceCategory(_obsRecord(event).kind)) items.push(event);
  });
  const seen = new Set();
  return items.filter((item,index)=>{
    const value = _obsRecord(item);
    const key = String(value.event_id || value.id || `${value.kind||''}|${value.timestamp||''}|${value.label||''}|${index}`);
    if(seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function _obsEvidenceDetail(value){
  if(value === null || value === undefined || value === '') return '';
  if(typeof value === 'object'){
    try{
      return _obsRedactText(JSON.stringify(value, (key,item)=>{
        if(/(path|repo_root|cwd|prompt|response)/i.test(key)) return undefined;
        return item;
      })).slice(0, 220);
    }catch(e){ return ''; }
  }
  return _obsRedactText(value).slice(0, 220);
}

function _renderObsEvidenceList(category, id, emptyText){
  const items = _obsEvidenceItems().filter(item=>_obsEvidenceCategory(_obsRecord(item).kind) === category);
  const el = document.getElementById(id);
  if(!items.length){
    el.innerHTML = `<div class="obs-evidence-empty">${esc(emptyText)}</div>`;
    return;
  }
  el.innerHTML = items.map(value=>{
    const item = _obsRecord(value);
    const status = String(item.status || 'recorded');
    const detail = _obsEvidenceDetail(item.detail);
    return `<div class="obs-evidence-item">
      <div><span class="obs-evidence-dot is-${_obsStatusLevel(status)}"></span><b>${esc(String(item.label || item.kind || '证据').slice(0,100))}</b></div>
      <small>${esc([status,fmtTime(item.timestamp)].filter(Boolean).join(' · '))}</small>
      ${detail?`<p>${esc(detail)}</p>`:''}
      ${_obsPreviewBlock(item)}
    </div>`;
  }).join('');
}

function _renderObsEvidence(){
  _renderObsEvidenceList('compaction', 'obsEvidenceCompaction', '无 compaction 事件');
  _renderObsEvidenceList('memory', 'obsEvidenceMemory', '无 memory 证据');
  _renderObsEvidenceList('verification', 'obsEvidenceVerification', '无 verification 证据');
  _renderObsEvidenceList('outcome', 'obsEvidenceOutcome', '无 outcome 证据');
}

function _resetObsTimeline(message='选择 session 后显示事件。'){
  _obsState.timeline = [];
  _obsState.nextCursor = null;
  _obsState.timelineTotal = 0;
  _renderObsTurns();
  _renderObsTools(message);
  _renderObsEvidence();
}

async function _loadObservabilityTimeline(append=false){
  const sessionId = _obsState.sessionId;
  if(!sessionId){
    _resetObsTimeline();
    return;
  }
  const request = ++_obsState.timelineRequest;
  const cursor = append ? _obsState.nextCursor : null;
  const more = document.getElementById('obsLoadMore');
  if(more) more.disabled = true;
  if(!append) _resetObsTimeline('正在加载 Tool / MCP 事件...');
  _setObsStatus('加载 timeline', 'running');
  try{
    let url = `${OBSERVABILITY_API}/sessions/${encodeURIComponent(sessionId)}/timeline?launch_id=${encodeURIComponent(_obsState.launchId)}&limit=100`;
    if(cursor !== null && cursor !== undefined && cursor !== '') url += `&cursor=${encodeURIComponent(cursor)}`;
    const payload = await _obsFetch(url);
    if(request !== _obsState.timelineRequest || sessionId !== _obsState.sessionId) return;
    const incoming = _obsArray(payload, ['events','items','timeline']);
    const existing = append ? _obsState.timeline.slice() : [];
    const seen = new Set(existing.map(_obsEventKey));
    incoming.forEach((event,index)=>{
      const key = _obsEventKey(event,index);
      if(!seen.has(key)){
        seen.add(key);
        existing.push(event);
      }
    });
    _obsState.timeline = existing;
    _obsState.nextCursor = payload.next_cursor ?? null;
    _obsState.timelineTotal = Number(payload.total ?? existing.length) || existing.length;
    _renderObsTurns();
    _renderObsTools();
    _renderObsEvidence();
    _setObsStatus(`已同步 ${existing.length}/${_obsState.timelineTotal} 条`, 'ok');
  }catch(error){
    if(request !== _obsState.timelineRequest) return;
    const message = _obsErrorText(error, 'Session timeline');
    const turnState = document.getElementById('obsTurnsState');
    turnState.hidden = false;
    turnState.textContent = message;
    _renderObsTools(message);
    _setObsStatus(error.status === 404 ? 'Timeline 未启用' : 'Timeline 异常', error.status === 404 ? 'warn' : 'fail');
  }finally{
    if(more) more.disabled = false;
  }
}

async function _loadObservabilityDetail(launchId){
  const request = ++_obsState.detailRequest;
  _setObsStatus('加载 launch', 'running');
  _setObsLoadState('正在读取 launch 总账与证据...', 'loading', true);
  try{
    const payload = await _obsFetch(`${OBSERVABILITY_API}/launches/${encodeURIComponent(launchId)}`);
    if(request !== _obsState.detailRequest || launchId !== _obsState.launchId) return;
    _obsState.detail = payload;
    const sessions = _obsArray(payload, ['sessions']);
    const currentExists = sessions.some(item=>_obsSessionId(item) === _obsState.sessionId);
    _obsState.sessionId = currentExists ? _obsState.sessionId : (sessions[0] ? _obsSessionId(sessions[0]) : '');
    _fillObsSessions(sessions, _obsState.sessionId);
    _renderObsLaunch(payload);
    _setObsLoadState('', 'ok', false);
    if(_obsState.sessionId) await _loadObservabilityTimeline(false);
    else{
      _resetObsTimeline('该 launch 暂无 session 事件。');
      _setObsStatus('Launch 暂无 session', 'warn');
    }
  }catch(error){
    if(request !== _obsState.detailRequest) return;
    _obsState.detail = {};
    _fillObsSessions([], '');
    _renderObsLaunch({launch:_obsState.launches.find(item=>_obsLaunchId(item) === launchId) || {launch_id:launchId}});
    _resetObsTimeline('Launch detail 不可用。');
    const message = _obsErrorText(error, 'Launch detail');
    _setObsLoadState(message, error.status === 404 ? 'warn' : 'fail', true);
    _setObsStatus(error.status === 404 ? 'Detail 未启用' : 'Detail 异常', error.status === 404 ? 'warn' : 'fail');
  }
}

async function loadObservabilityLaunches(force=false){
  if(_obsState.loading && !force) return;
  _obsState.loading = true;
  const previousLaunch = _obsState.launchId;
  const refresh = document.getElementById('obsRefresh');
  if(refresh) refresh.disabled = true;
  _setObsStatus('加载 launches', 'running');
  _setObsLoadState('正在读取 Pacer launch 索引...', 'loading', true);
  try{
    const payload = await _obsFetch(`${OBSERVABILITY_API}/launches`);
    const launches = _obsArray(payload, ['launches','items']);
    _obsState.launches = launches;
    _obsState.loaded = true;
    if(!launches.length){
      _obsState.launchId = '';
      _obsState.sessionId = '';
      _obsState.detail = null;
      _fillObsLaunches([], '');
      _fillObsSessions([], '');
      document.getElementById('obsContent').hidden = true;
      _setObsLoadState('暂无可观测 launch。运行一次 Pacer 任务后，此处会出现 token、agent 与闭环证据。', 'empty', true);
      _setObsStatus('暂无数据', 'idle');
      return;
    }
    const selected = launches.some(item=>_obsLaunchId(item) === previousLaunch) ? previousLaunch : _obsLaunchId(launches[0]);
    _obsState.launchId = selected;
    if(selected !== previousLaunch) _obsState.sessionId = '';
    _fillObsLaunches(launches, selected);
    await _loadObservabilityDetail(selected);
  }catch(error){
    _obsState.loaded = true;
    _obsState.launches = [];
    _obsState.launchId = '';
    _obsState.sessionId = '';
    _fillObsLaunches([], '');
    _fillObsSessions([], '');
    document.getElementById('obsContent').hidden = true;
    const message = _obsErrorText(error, 'Observability launches');
    _setObsLoadState(message, error.status === 404 ? 'warn' : 'fail', true);
    _setObsStatus(error.status === 404 ? 'API 未启用' : '加载失败', error.status === 404 ? 'warn' : 'fail');
  }finally{
    _obsState.loading = false;
    if(refresh) refresh.disabled = false;
  }
}

function ensureObservabilityLoaded(){
  if(!_obsState.loaded && !_obsState.loading) loadObservabilityLaunches(false);
}

function refreshObservability(){
  return loadObservabilityLaunches(true);
}

function selectObservabilityLaunch(launchId){
  const id = String(launchId || '').trim();
  if(!id || id === _obsState.launchId) return;
  _obsState.launchId = id;
  _obsState.sessionId = '';
  _obsState.timelineRequest += 1;
  _fillObsLaunches(_obsState.launches, id);
  _fillObsSessions([], '');
  _resetObsTimeline('选择 session 后显示事件。');
  _loadObservabilityDetail(id);
}

function selectObservabilitySession(sessionId){
  const id = String(sessionId || '').trim();
  if(!id || id === _obsState.sessionId) return;
  _obsState.sessionId = id;
  _obsState.timelineRequest += 1;
  _loadObservabilityTimeline(false);
}

function loadMoreObservabilityTimeline(){
  if(_obsState.nextCursor === null || _obsState.nextCursor === undefined || _obsState.nextCursor === '') return;
  return _loadObservabilityTimeline(true);
}

let _eventSource = null;
let _streamReloadTimer = null;
function setStreamState(text, level='mut'){
  const el = document.getElementById('streamState');
  if(!el) return;
  el.textContent = text;
  el.className = 'stream-state ' + level;
}
function scheduleStreamLoad(){
  clearTimeout(_streamReloadTimer);
  _streamReloadTimer = setTimeout(()=>load(), 180);
}
function connectEventStream(){
  if(!window.EventSource){
    setStreamState('轮询备份', 'warn');
    return;
  }
  try{
    if(_eventSource) _eventSource.close();
    _eventSource = new EventSource('/api/events');
    _eventSource.onopen = () => setStreamState('实时连接', 'ok');
    _eventSource.addEventListener('snapshot', ev => {
      setStreamState('实时更新', 'ok');
      scheduleStreamLoad();
    });
    _eventSource.addEventListener('heartbeat', ev => {
      setStreamState('实时连接', 'ok');
    });
    _eventSource.onerror = () => {
      setStreamState('轮询备份', 'warn');
    };
  }catch(e){
    setStreamState('轮询备份', 'warn');
  }
}

function launchCard(l){
  const p = l.state==='error'?'fail':'acc';
  const txt = l.state==='error'?'启动失败':(l.execute?'派发中…':'预览中…');
  const extra = l.state==='error'?`<div class="tcard-meta" style="color:var(--fail)">${esc(l.error||'')}</div>`:'';
  return `<div class="tcard st-running"><div class="tcard-goal">${esc(l.goal)}</div>
    <div class="tcard-foot"><span class="pill ${p}">${txt}</span></div>${extra}</div>`;
}
function renderBoard(d){
  const cols={todo:0,in_progress:0,in_review:0,done:0};
  const missions = d.missions||[];
  const launches = d.launches||[];
  const runObjs = new Set(missions.filter(m=>m.board_column==='in_progress').map(m=>m.objective));
  launches.forEach(l=>{
    if(l.state==='starting'&&!runObjs.has(l.goal)) cols.in_progress += 1;
    else if(l.state==='error') cols.in_review += 1;
  });
  missions.forEach(m=>{
    const col = cols[m.board_column] !== undefined ? m.board_column : 'in_review';
    cols[col] += 1;
  });
  const emp=t=>`<div class="empty">${t}</div>`;
  _updateMissionFilters(missions, launches);
  const launchItems = launches
    .filter(l=>l.state==='starting'||l.state==='error')
    .filter(l=>_missionFilter==='all'||(_missionFilter==='active'&&l.state==='starting')||(_missionFilter==='blocked'&&l.state==='error'))
    .map(launchCard);
  const filteredMissions = _missionFilter==='all' ? missions : missions.filter(m=>_missionGroup(m) === _missionFilter);
  const listItems = [...launchItems, ...filteredMissions.map(missionRow)];
  const listEl = document.getElementById('missionList');
  if(listEl) listEl.innerHTML = listItems.join('') || emp(_missionFilter==='all' ? '暂无任务。先在左侧新建任务。' : '当前筛选下没有任务。');
  setText('cTodo', cols.todo);
  setText('cRun', cols.in_progress);
  setText('cReview', cols.in_review);
  setText('cDone', cols.done);
  setText('sRunning', cols.in_progress);

  const worker=d.worker||{};
  const status=d.status||{};
  const meta=document.getElementById('pageMeta');
  const agents=(d.installed_agents||[]).slice(0,4);
  const quotaPills = _subscriptionQuotaPills(d.subscription_quota);
  meta.innerHTML=[
    `<span class="pill ${worker.running?'ok':'mut'}">${worker.running?'Worker 运行中':'Worker 空闲'}</span>`,
    `<span class="pill acc">${esc(status.state||'unknown')}</span>`,
    agents.length?`<span class="pill mut">${agents.map(esc).join(' · ')}</span>`:'',
    ...quotaPills
  ].filter(Boolean).join('');

  const totalTokens=((d.value?.input_tokens||0)+(d.value?.output_tokens||0));
  setText('kVerified', d.value?.verified||0);
  setText('kRunning', cols.in_progress);
  setText('kQueue', (d.queue||[]).length);
  const globalEff=d.value?.mimo_efficiency||{};
  setText('kSavedUsd', '$'+Number(globalEff.saved_usd||0).toFixed(2));
  setText('kSavedMin', Number(globalEff.saved_minutes||0).toFixed(1)+' 分钟');
  setText('kQuotaSave', _quotaSavedPercent(globalEff).toFixed(1)+'%');
  setText('kSubQuota', _subscriptionQuotaHeadline(d.subscription_quota));
  setText('kEfficiency', String(globalEff.efficiency_gain_percent||0)+'%');
  setText('kTokens', '累计 Tokens '+totalTokens.toLocaleString());
}

function _subscriptionQuotaHeadline(q){
  const summary = q?.summary || {};
  const windows = Array.isArray(summary.windows) ? summary.windows : [];
  if(!windows.length) return '未采集';
  const maxUsed = Number(summary.max_used_percentage || 0);
  return `${maxUsed.toFixed(0)}% 已用`;
}

function _subscriptionQuotaPills(q){
  const summary = q?.summary || {};
  const windows = Array.isArray(summary.windows) ? summary.windows.slice(0,3) : [];
  if(!windows.length) return [`<span class="pill warn">订阅额度未采集</span>`];
  return windows.map(w=>{
    const provider = String(w.provider || '').replace('claude-code','Claude').replace('codex','Codex');
    const used = Number(w.used_percentage || 0);
    return `<span class="pill ${used>=80?'warn':'ok'}">${esc(provider)} ${esc(w.label||'窗口')} ${used.toFixed(0)}%</span>`;
  });
}

function renderSubscriptionQuotaPanel(q){
  const summary = q?.summary || {};
  const windows = Array.isArray(summary.windows) ? summary.windows : [];
  const maxUsed = Number(summary.max_used_percentage || 0);
  const stateEl = document.getElementById('quotaRailState');
  const headEl = document.getElementById('quotaRailHeadline');
  const listEl = document.getElementById('quotaRailList');
  if(!stateEl || !headEl || !listEl) return;
  stateEl.className = 'ops-badge';
  if(!windows.length){
    stateEl.textContent = '未采集';
    stateEl.classList.add('warn');
    headEl.textContent = '未采集';
    listEl.innerHTML = '<div class="rail-note">配置 Claude statusLine 或 Codex 状态命令后，这里会显示真实 5h / 周额度。</div>';
    return;
  }
  stateEl.textContent = maxUsed >= 90 ? '额度紧张' : (maxUsed >= 75 ? '注意用量' : '可用');
  stateEl.classList.add(maxUsed >= 90 ? 'fail' : (maxUsed >= 75 ? 'warn' : 'ok'));
  headEl.textContent = `${maxUsed.toFixed(0)}% 已用`;
  listEl.innerHTML = windows.map(w=>{
    const provider = String(w.provider || '').replace('claude-code','Claude').replace('codex','Codex');
    const used = Math.max(0, Math.min(100, Number(w.used_percentage || 0)));
    const cls = used >= 90 ? 'danger' : (used >= 75 ? 'hot' : '');
    return `<div class="quota-window ${cls}">
      <span>${esc(provider)} · ${esc(w.label || '窗口')}</span>
      <b>${used.toFixed(0)}%</b>
      <div class="quota-window-bar"><i style="width:${used.toFixed(0)}%"></i></div>
    </div>`;
  }).join('');
}

function renderPromotionReadinessPanel(readiness){
  const r = readiness || {};
  const badge = document.getElementById('readinessBadge');
  const score = document.getElementById('readinessScore');
  const headline = document.getElementById('readinessHeadline');
  const checksEl = document.getElementById('readinessChecks');
  const needsEl = document.getElementById('readinessNeeds');
  if(!badge || !score || !headline || !checksEl || !needsEl) return;
  const level = String(r.level || 'needs_config');
  const scoreValue = Number(r.score || 0);
  badge.className = 'ops-badge ' + (level === 'ready' ? 'ok' : (level === 'blocked' ? 'fail' : 'warn'));
  badge.textContent = level === 'ready' ? '可推广' : (level === 'blocked' ? '有阻断' : '需补配置');
  score.textContent = String(scoreValue);
  headline.textContent = r.headline || '等待数据';
  const checks = Array.isArray(r.checks) ? r.checks : [];
  checksEl.innerHTML = checks.length ? checks.map(item=>{
    const status = String(item.status || 'warning');
    const cls = status === 'success' ? 'ok' : (status === 'failed' ? 'fail' : 'warn');
    return `<div class="readiness-check ${cls}">
      <span class="readiness-dot"></span>
      <div><b>${esc(item.label || item.id || '检查项')}</b><small>${esc(item.detail || '')}</small></div>
    </div>`;
  }).join('') : '<div class="empty">暂无推广检查数据。</div>';
  const needs = Array.isArray(r.user_required) ? r.user_required.filter(Boolean) : [];
  needsEl.textContent = needs.length ? ('还需要你提供：' + needs.join('；')) : '当前没有必须由你补充的外部信息。';
}

function renderCoreReadinessPanel(readiness){
  const r = readiness || {};
  const badge = document.getElementById('coreReadinessBadge');
  const score = document.getElementById('coreReadinessScore');
  const headline = document.getElementById('coreReadinessHeadline');
  const checksEl = document.getElementById('coreReadinessChecks');
  const actionsEl = document.getElementById('coreReadinessActions');
  if(!badge || !score || !headline || !checksEl || !actionsEl) return;
  const level = String(r.level || 'needs_evidence');
  const scoreValue = Number(r.score || 0);
  badge.className = 'ops-badge ' + (level === 'usable' ? 'ok' : (level === 'blocked' ? 'fail' : 'warn'));
  badge.textContent = level === 'usable' ? '可用' : (level === 'blocked' ? '有阻断' : '需样本');
  score.textContent = String(scoreValue);
  headline.textContent = r.headline || '等待数据';
  const checks = Array.isArray(r.checks) ? r.checks : [];
  checksEl.innerHTML = checks.length ? checks.map(item=>{
    const status = String(item.status || 'warning');
    const cls = status === 'success' ? 'ok' : (status === 'failed' ? 'fail' : 'warn');
    return `<div class="readiness-check ${cls}">
      <span class="readiness-dot"></span>
      <div><b>${esc(item.label || item.id || '检查项')}</b><small>${esc(item.detail || '')}</small></div>
    </div>`;
  }).join('') : '<div class="empty">暂无产品可用度检查数据。</div>';
  const actions = Array.isArray(r.operator_actions) ? r.operator_actions.filter(Boolean) : [];
  actionsEl.textContent = actions.length ? ('下一步：' + actions.join('；')) : '核心闭环没有必须立即处理的本地动作。';
}

function updateRailMetrics(eff){
  setText('rSavedUsd', '$'+Number(eff?.saved_usd||0).toFixed(2));
  setText('rQuotaSave', _quotaSavedPercent(eff||{}).toFixed(1)+'%');
  setText('rSavedMin', Number(eff?.saved_minutes||0).toFixed(1)+' 分钟');
  setText('rEfficiency', String(eff?.efficiency_gain_percent||0)+'%');
}

// ---------- 主加载（带 AbortController + 缓存控制）----------
let _loadAbort = null;
let _loading = false;

async function load(){
  // 取消上一次未完成的请求
  if(_loadAbort) _loadAbort.abort();
  _loadAbort = new AbortController();
  _loading = true;

  try{
    const d = await (await fetch('/api/data', {signal: _loadAbort.signal})).json();
    fillAgents(d);
    _syncChatAgents(d.installed_agents||[], _cheapBackendAgent(d));

    const proj = document.getElementById('projPath');
    const ws = (d.workspace_root||'').replace(/\\/g, '/');
    const wsShort = ws.split('/').slice(-2).join('/') || ws;
    proj.textContent = wsShort;
    proj.title = '当前工作空间：' + d.workspace_root + '，点击切换项目';

    document.getElementById('agentsRow').innerHTML = (d.agents||[])
      .map(a=>`<span class="pill ${a.installed?'ok':'mut'}">${esc(a.display_name||a.agent)}</span>`).join(' ');

    updateWorker(d.worker||{});

    const v=d.value||{};
    setText('sVerified', v.verified||0);
    const totalTok=(v.input_tokens||0)+(v.output_tokens||0);
    if(totalTok>0){
      setText('sTokens', totalTok.toLocaleString());
      setDisplay('tokenStat', 'flex');
    }

    // MiMo efficiency metrics
    const me=v.mimo_efficiency||{};
    setText('mSavedUsd', '$'+Number(me.saved_usd||0).toFixed(2));
    setText('mQuotaSave', _quotaSavedPercent(me).toFixed(1)+'%');
    setText('mSavedMin', Number(me.saved_minutes||0).toFixed(1)+' 分钟');
    setText('mEfficiency', (me.efficiency_gain_percent||0)+'%');
    setText('mCapScore', me.capability_score||0);
    setText('mMimoRuns', me.mimo_runs||0);
    updateRailMetrics(me);
    renderSubscriptionQuotaPanel(d.subscription_quota);
    renderCoreReadinessPanel(d.core_readiness);
    renderPromotionReadinessPanel(d.promotion_readiness);
    refreshRelayPanel(false);

    renderBoard(d);
    renderTrace(d);

    // 队列
    const qEl=document.getElementById('queue');
    const qLen=(d.queue||[]).length;
    setText('sQueue', qLen);
    setText('cQueueSec', qLen);
    if(qLen){
      qEl.innerHTML=(d.queue||[]).map(q=>{
        const sr=srInfo(q.stop_reason);
        const stopTag=q.stop_reason?`<span class="pill ${pc(q.status)}">${esc(sr.zh||q.stop_reason)}</span>`:'';
        return `<div class="qrow">
          <span class="qgoal">${esc(q.objective||q.mission_id)}</span>
          <span class="qmeta">
            <span class="pill ${pc(q.status)}">${esc(q.status)}</span>
            ${stopTag}
            <span class="muts">${esc(q.agent||'codex')}</span>
          </span>
        </div>`;
      }).join('');
    } else {
      qEl.innerHTML=`<div class="empty">暂无队列任务。<br>用 <code>checkpoint mission queue</code> 批量排队，<br>或点右上角「启动 Worker」自动处理。</div>`;
    }

    // 项目托管
    const gEl=document.getElementById('programs');
    if((d.programs||[]).length){
      gEl.innerHTML=(d.programs||[]).map(g=>`<div class="qrow">
        <span class="qgoal">${esc(g.objective||g.program_id)}</span>
        <span class="qmeta">
          <span class="pill ${pc(g.status)}">${esc(g.status)}</span>
          <span class="muts">${esc(String(g.task_count||0))} 个任务</span>
        </span>
      </div>`).join('');
    } else {
      gEl.innerHTML=`<div class="empty">暂无项目托管。<br>用 <code>checkpoint autopilot --file 开发计划.md</code> 创建。</div>`;
    }
  }catch(e){
    if(e.name === 'AbortError') return; // 被取消的请求，忽略
    console.error('load',e);
  }finally{
    _loading = false;
  }
}

// ---------- 详情抽屉 ----------
function diffSection(rv){
  const ds=(rv||{}).diff_summary||{};
  if(!ds.file_count) return '';
  const files=(ds.changed_files||[]).slice(0,40).map(f=>`
    <div class="filerow"><span>${esc(f.path)}</span>
    <span><span class="add">+${f.lines_added||0}</span> <span class="del">-${f.lines_removed||0}</span></span></div>`).join('');
  const fns=(ds.functions_touched||[]).slice(0,20).map(x=>`<code>${esc(x)}</code>`).join('、');
  const checks=(ds.user_checklist||[]).map(c=>`<li>${esc(c)}</li>`).join('');
  const warn=(rv.warnings||[]).map(w=>`<div class="pill warn" style="display:block;margin:4px 0;">${esc(w)}</div>`).join('');
  return `<h3>改动摘要（${ds.file_count} 个文件，<span class="add">+${ds.lines_added}</span>/<span class="del">-${ds.lines_removed}</span>）</h3>
    ${warn}${files}
    ${fns?`<div class="muts" style="margin-top:5px;">涉及函数：${fns}</div>`:''}
    ${checks?`<h3>建议人工检查</h3><ul style="padding-left:16px;">${checks}</ul>`:''}`;
}

function closeDrawer(){ document.getElementById('drawer').style.display='none'; }

async function detail(id){
  const d = await (await fetch('/api/mission?id='+encodeURIComponent(id))).json();
  const m=d.mission||{};
  const rv=d.review||{};
  const ev=d.pacer_evidence||{};
  const sr=srInfo(m.stop_reason);
  const eff=m.efficiency || {};
  const isStopped=m.status==='stopped'||m.status==='failed';
  const commandMode = String(m.verification_mode || ev.verification_mode || '').toLowerCase() === 'command';
  const hasStop=m.stop_reason&&m.stop_reason!=='verified'&&!(commandMode&&m.stop_reason==='coverage_gap');
  const canRetry=isStopped&&hasStop;
  const canDelete=!['running','background_running','preview_running','retrying','starting','verified','merged','archived'].includes(String(m.status||''));

  const mergeBtn=d.can_merge?`<button class="act-merge" style="padding:6px 14px;font-size:12px;" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并到主分支</button>`:'';
  const clarifyBtn=(!commandMode&&(m.stop_reason==='needs_clarification'||m.stop_reason==='coverage_gap'))?`<button class="act-retry" style="padding:6px 14px;font-size:12px;" onclick="openTaskIntake('${jsq(m.objective||'')}')">继续确认任务</button>`:'';
  const relayBtn=m.stop_reason==='quota_exhausted'?`<button class="act-retry" style="padding:6px 14px;font-size:12px;" onclick="closeDrawer();focusWorkbenchPanel('relay-panel','#relayBaseUrl')">打开中转站</button>`:'';
  const retryBtn=canRetry?`<button class="act-retry" style="padding:6px 14px;font-size:12px;" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 用相同目标重试</button>`:'';
  const deleteBtn=canDelete?`<button class="act-delete" style="padding:6px 14px;font-size:12px;" onclick="deleteMission(event,'${esc(m.mission_id)}')">删除任务</button>`:'';
  const createdAt = fmtTime(m.created_at);
  const updatedAt = fmtTime(m.updated_at);
  const missionTimeLine = (createdAt || updatedAt)
    ? `<p class="detail-time">${createdAt?`创建：${esc(createdAt)}`:''}${createdAt&&updatedAt?' · ':''}${updatedAt?`更新：${esc(updatedAt)}`:''}</p>`
    : '';
  const stopHint=hasStop?`<div class="stop-hint">
    <div>停止原因：<b>${esc(sr.zh)}</b></div>
    ${sr.fix?`<div class="fix">建议操作：${esc(sr.fix)}</div>`:''}
  </div>`:'';

  const rounds=(d.rounds||[]).map(r=>`<div class="round-row">
    <span>Round ${r.round}：<b>${esc(r.type)}</b>${fmtTime(r.recorded_at)?`<span class="round-time">${esc(fmtTime(r.recorded_at))}</span>`:''}</span>
    <span><span class="pill ${pc(r.status)}">${esc(r.status)}</span>
    ${r.stop_reason||r.reason?`<span class="muts">${esc(r.stop_reason||r.reason)}</span>`:''}</span>
  </div>`).join('');
  const live=d.live_logs||{};
  const liveBlock=live.latest_tail?`<h3>实时执行日志</h3><pre>${esc(live.latest_tail)}</pre>`:'';
  const finalReportHtml = d.final_report ? _renderMarkdown(d.final_report) : '<div class="empty">暂无报告</div>';
  const contractBlock = _requirementContractDetail(m);
  const evidenceBlock = (ev.worker_status||ev.verification_verdict||ev.backend||ev.log_path||ev.worktree||ev.verification_command)
    ? `<h3>执行证据</h3><div class="detail-evidence">
        <div class="evi"><span>Worker</span><code>${esc(ev.worker_status||'-')}${ev.worker_exit_code!==undefined&&ev.worker_exit_code!==null?` exit ${esc(ev.worker_exit_code)}`:''}</code></div>
        <div class="evi"><span>后端 / 模型</span><code>${esc(ev.backend||ev.agent||'-')} ${esc(ev.model||'')}</code></div>
        <div class="evi"><span>派工 / 推理</span><code>${esc(ev.dispatch_mode||m.dispatch_mode||'tracked')} · ${esc(ev.reasoning_effort||'inherit')}</code></div>
        <div class="evi"><span>验收结果</span><code>${esc(ev.verification_verdict||'-')}</code></div>
        <div class="evi"><span>验收命令</span><code>${esc(ev.verification_command||rv.command||'')}</code></div>
        <div class="evi wide"><span>Worktree</span><code>${esc(ev.worktree||'')}</code></div>
        <div class="evi wide"><span>日志 / 报告</span><code>${esc((ev.log_path||'') + (ev.final_report?(' | '+ev.final_report):''))}</code></div>
        ${ev.log_tail?`<div class="evi wide"><span>Worker 日志尾部</span><pre>${esc(ev.log_tail)}</pre></div>`:''}
      </div>`
    : '';

  document.getElementById('drawerBody').innerHTML=
    `<h2>${esc(m.objective||id)}</h2>
     <p>状态：<span class="pill ${pc(m.status)}">${esc(zh(m.status))}</span>
     ${d.merge_state?' · 合并：'+esc(d.merge_state):''}</p>
     ${missionTimeLine}
     ${stopHint}
     <div class="action-row">${mergeBtn}${clarifyBtn}${relayBtn}${retryBtn}${deleteBtn}</div>
     ${contractBlock}
     ${_efficiencyDetail(eff)}
     ${rv.verdict?`<p>验收命令：<code>${esc(rv.command||'工作流验收')}</code> · 结论：<span class="pill ${pc(rv.verdict)}">${esc(rv.verdict)}</span></p>`:''}
     ${evidenceBlock}
     ${diffSection(rv)}
     <h3>执行轮次</h3>${rounds||'<div class="empty">暂无轮次记录</div>'}
     ${liveBlock}
     <h3>最终报告</h3><div class="report-md">${finalReportHtml}</div>`;
  document.getElementById('drawer').style.display='block';
}

function _efficiencyDetail(eff){
  if(!eff) return '';
  const runs = Number(eff.mimo_runs || eff.worker_runs || 0);
  const taskSec = Number(eff.actual_task_seconds || 0);
  const workerSec = Number(eff.actual_worker_seconds || 0);
  const actualCost = eff.actual_cost_available ? ('$'+Number(eff.actual_cost_usd || 0).toFixed(4)) : '未回传';
  const savedUsd = Number(eff.saved_usd || 0);
  const savedMin = Number(eff.saved_minutes || 0);
  const quota = _quotaSavedPercent(eff);
  if(!runs && !taskSec && !workerSec && !savedUsd && !savedMin) return '';
  return `<h3>本任务实际消耗</h3><div class="metric-row">
    <span class="pill acc">后端运行 ${runs}</span>
    <span class="pill ok">套餐额度节省 ${quota.toFixed(1)}%</span>
    <span class="pill ${eff.actual_cost_available?'ok':'warn'}">实际额度 ${actualCost}</span>
    <span class="pill acc">任务耗时 ${_fmtSeconds(taskSec)}</span>
    <span class="pill acc">Worker耗时 ${_fmtSeconds(workerSec)}</span>
  </div><div class="muts">估算节省：$${savedUsd.toFixed(4)} / ${savedMin.toFixed(2)} 分钟 / 套餐额度 ${quota.toFixed(1)}%，仅作参考，不等同实际消耗。</div>`;
}

function _renderMarkdown(md){
  const lines = String(md||'').split(/\r?\n/);
  let html = '';
  let inList = false;
  const closeList = () => { if(inList){ html += '</ul>'; inList = false; } };
  for(const raw of lines){
    const line = raw.trim();
    if(!line){ closeList(); continue; }
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if(h){
      closeList();
      const level = Math.min(4, h[1].length + 1);
      html += `<h${level}>${_inlineMarkdown(h[2])}</h${level}>`;
      continue;
    }
    const li = line.match(/^[-*]\s+(.+)$/);
    if(li){
      if(!inList){ html += '<ul>'; inList = true; }
      html += `<li>${_inlineMarkdown(li[1])}</li>`;
      continue;
    }
    closeList();
    html += `<p>${_inlineMarkdown(line)}</p>`;
  }
  closeList();
  return html;
}

function _inlineMarkdown(text){
  let s = esc(String(text||''));
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  return s;
}

// ---------- 对话面板 ----------
let _chatHistory = [];
let _chatAgents = ['claude-code', 'codex'];

function openChat(){
  _intakeDraft = '';
  document.querySelector('#chatPanel .chat-hdr-title').textContent = 'AI 需求架构师';
  document.getElementById('chatPanel').style.display='block';
  document.getElementById('chatInput').focus();
}
function closeChat(){ document.getElementById('chatPanel').style.display='none'; }

// ---- Settings panel ----
async function openSettings(){
  document.getElementById('settingsPanel').style.display='block';
  try{
    const p = await (await fetch('/api/profile')).json();
    document.getElementById('cfgProfileEmail').value = p.email || '';
    document.getElementById('cfgProfileName').value = p.display_name || '';
    document.getElementById('cfgProfileOrg').value = p.organization || '';
    document.getElementById('profileCfgStatus').textContent = p.configured ? '已保存本地邮箱身份：'+(p.email||'') : '尚未保存邮箱身份';
    document.getElementById('profileCfgStatus').style.color = p.configured ? 'var(--ok)' : 'var(--mut)';
  }catch(e){
    document.getElementById('profileCfgStatus').textContent='加载邮箱身份失败：'+e.message;
    document.getElementById('profileCfgStatus').style.color='var(--fail)';
  }
  try{
    const c = await (await fetch('/api/commercial-config')).json();
    _syncCommercialConfig(c);
  }catch(e){
    const el=document.getElementById('commercialCfgStatus');
    if(el){ el.textContent='加载登录与付费失败：'+e.message; el.style.color='var(--fail)'; }
  }
  try{
    const m = await (await fetch('/api/model-config')).json();
    document.getElementById('cfgPreferSub2api').checked = m.enabled !== false;
    document.getElementById('cfgModelBaseUrl').value = m.base_url || 'http://174.138.75.136:8080/v1';
    document.getElementById('cfgModelApiKey').value = m.api_key || '';
    document.getElementById('cfgModelName').value = m.model || 'gpt-4o-mini';
    document.getElementById('cfgReasoningEffort').value = m.reasoning_effort || '';
    document.getElementById('cfgMonthlyBudget').value = _budgetInputValue(m.monthly_budget_usd);
    document.getElementById('cfgMissionBudget').value = _budgetInputValue(m.per_mission_budget_usd);
    document.getElementById('cfgQuotaThreshold').value = _budgetInputValue(m.auto_switch_quota_percent || 80);
    document.getElementById('modelCfgStatus').textContent = m.configured ? '已配置：'+(m.summary||'') : '尚未配置 sub2api 接口';
    document.getElementById('modelCfgStatus').style.color = m.configured ? 'var(--ok)' : 'var(--mut)';
    _syncRelayPanel(m, true);
  }catch(e){
    document.getElementById('modelCfgStatus').textContent='加载模型接口失败：'+e.message;
    document.getElementById('modelCfgStatus').style.color='var(--fail)';
  }
  try{
    const d = await (await fetch('/api/notifications/config')).json();
    document.getElementById('cfgSmtpHost').value = d.smtp_host||'';
    document.getElementById('cfgSmtpPort').value = d.smtp_port||587;
    document.getElementById('cfgUseTls').checked = d.use_tls!==false;
    document.getElementById('cfgUsername').value = d.username||'';
    document.getElementById('cfgPassword').value = d.password||'';
    document.getElementById('cfgSender').value = d.sender||'';
    document.getElementById('cfgRecipient').value = d.recipient||'';
    document.getElementById('cfgStatus').textContent = d.configured ? '已配置邮件通知' : '尚未配置';
    document.getElementById('cfgStatus').style.color = d.configured ? 'var(--ok)' : 'var(--mut)';
  }catch(e){ document.getElementById('cfgStatus').textContent='加载配置失败：'+e.message; }
}
function closeSettings(){ document.getElementById('settingsPanel').style.display='none'; }
function _budgetInputValue(value){
  const n = Number(value || 0);
  return Number.isFinite(n) && n > 0 ? String(n) : '';
}
function _numberFromInput(id, fallback=0){
  const el = document.getElementById(id);
  if(!el) return fallback;
  const raw = String(el.value || '').trim();
  if(raw === '') return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}
function _modelConfigBody(source='settings'){
  const relay = source === 'relay';
  return {
    enabled: relay ? true : document.getElementById('cfgPreferSub2api').checked,
    base_url: document.getElementById(relay ? 'relayBaseUrl' : 'cfgModelBaseUrl').value.trim(),
    api_key: document.getElementById(relay ? 'relayApiKey' : 'cfgModelApiKey').value.trim(),
    model: document.getElementById(relay ? 'relayModel' : 'cfgModelName').value.trim(),
    reasoning_effort: document.getElementById(relay ? 'relayReasoning' : 'cfgReasoningEffort').value,
    monthly_budget_usd: _numberFromInput(relay ? 'relayMonthlyBudget' : 'cfgMonthlyBudget', 0),
    per_mission_budget_usd: _numberFromInput(relay ? 'relayMissionBudget' : 'cfgMissionBudget', 0),
    auto_switch_quota_percent: _numberFromInput(relay ? 'relayQuotaThreshold' : 'cfgQuotaThreshold', 80)
  };
}
function _setValue(id, value){
  const el=document.getElementById(id);
  if(el) el.value = value || '';
}
function _setChecked(id, value){
  const el=document.getElementById(id);
  if(el) el.checked = !!value;
}
function _fieldValue(id){
  const el=document.getElementById(id);
  return el ? String(el.value || '').trim() : '';
}
function _syncCommercialConfig(c){
  _setValue('cfgSupabaseUrl', c.supabase_url || '');
  _setValue('cfgSupabaseAnonKey', c.supabase_anon_key || '');
  _setValue('cfgSupabaseServiceRoleKey', c.supabase_service_role_key || '');
  _setChecked('cfgGoogleOauthConfigured', c.google_oauth_configured);
  _setValue('cfgGoogleClientId', c.google_client_id || '');
  _setValue('cfgGoogleClientSecret', c.google_client_secret || '');
  _setValue('cfgStripePublishableKey', c.stripe_publishable_key || '');
  _setValue('cfgStripeSecretKey', c.stripe_secret_key || '');
  _setValue('cfgStripeWebhookSecret', c.stripe_webhook_secret || '');
  _setValue('cfgStripePriceId', c.stripe_price_id || '');
  _setValue('cfgStripePortalUrl', c.stripe_customer_portal_url || '');
  _setValue('cfgStripeUsageMeterEvent', c.stripe_usage_meter_event || 'pacer_managed_minutes');
  const el=document.getElementById('commercialCfgStatus');
  if(!el) return;
  const parts=[];
  parts.push(c.auth_configured ? 'Supabase/Google 登录已配置' : '登录未完整配置');
  parts.push(c.billing_configured ? 'Stripe Billing 已配置' : 'Stripe Billing 未完整配置');
  parts.push(c.portal_configured ? 'Customer Portal 已配置' : 'Customer Portal 未配置');
  el.textContent = parts.join('；');
  el.style.color = c.auth_configured && c.billing_configured && c.portal_configured ? 'var(--ok)' : 'var(--mut)';
}
function _commercialConfigBody(){
  return {
    auth_provider:'supabase',
    login_provider:'google',
    supabase_url:_fieldValue('cfgSupabaseUrl'),
    supabase_anon_key:_fieldValue('cfgSupabaseAnonKey'),
    supabase_service_role_key:_fieldValue('cfgSupabaseServiceRoleKey'),
    google_oauth_configured:!!document.getElementById('cfgGoogleOauthConfigured')?.checked,
    google_client_id:_fieldValue('cfgGoogleClientId'),
    google_client_secret:_fieldValue('cfgGoogleClientSecret'),
    billing_provider:'stripe',
    billing_mode:'subscriptions_with_portal',
    stripe_publishable_key:_fieldValue('cfgStripePublishableKey'),
    stripe_secret_key:_fieldValue('cfgStripeSecretKey'),
    stripe_webhook_secret:_fieldValue('cfgStripeWebhookSecret'),
    stripe_price_id:_fieldValue('cfgStripePriceId'),
    stripe_customer_portal_url:_fieldValue('cfgStripePortalUrl'),
    stripe_usage_meter_event:_fieldValue('cfgStripeUsageMeterEvent') || 'pacer_managed_minutes'
  };
}
function _syncRelayPanel(m, force=false){
  const ids = ['relayBaseUrl','relayApiKey','relayModel','relayReasoning','relayMonthlyBudget','relayMissionBudget','relayQuotaThreshold'];
  const active = document.activeElement && ids.includes(document.activeElement.id);
  if(active && !force) return;
  const base = document.getElementById('relayBaseUrl');
  if(!base) return;
  base.value = m.base_url || 'http://127.0.0.1:8788/v1';
  document.getElementById('relayApiKey').value = m.api_key || '';
  document.getElementById('relayModel').value = m.model || 'gpt-4o-mini';
  document.getElementById('relayReasoning').value = m.reasoning_effort || '';
  document.getElementById('relayMonthlyBudget').value = _budgetInputValue(m.monthly_budget_usd);
  document.getElementById('relayMissionBudget').value = _budgetInputValue(m.per_mission_budget_usd);
  document.getElementById('relayQuotaThreshold').value = _budgetInputValue(m.auto_switch_quota_percent || 80);
  _setRelayStatus(m.configured ? '已配置：'+(m.summary||'') : '未配置中转站', m.configured ? 'ok' : 'warn');
  _setRelayBudgetHint(m);
}
function _setRelayStatus(text, level='warn'){
  const status = document.getElementById('relayStatus');
  const badge = document.getElementById('relayConfigured');
  if(status){
    status.textContent = text || '';
    status.style.color = level === 'ok' ? 'var(--ok)' : (level === 'fail' ? 'var(--fail)' : 'var(--mut)');
  }
  if(badge){
    badge.className = 'ops-badge ' + (level === 'ok' ? 'ok' : (level === 'fail' ? 'fail' : 'warn'));
    badge.textContent = level === 'ok' ? '已配置' : (level === 'fail' ? '异常' : '未配置');
  }
}
function _setRelayBudgetHint(m){
  const el = document.getElementById('relayBudgetHint');
  if(!el) return;
  const monthly = Number(m.monthly_budget_usd || 0);
  const mission = Number(m.per_mission_budget_usd || 0);
  const threshold = Number(m.auto_switch_quota_percent || 80);
  if(monthly > 0 || mission > 0){
    el.textContent = `预算护栏：月预算 $${monthly.toFixed(2)}，单任务 $${mission.toFixed(2)}，订阅额度 ${threshold.toFixed(0)}% 后优先切换。`;
    el.style.color = 'var(--ok)';
  }else{
    el.textContent = '预算护栏未配置：推广前建议设置月预算和单任务上限。';
    el.style.color = 'var(--warn)';
  }
}
let _relayLastRefresh = 0;
async function refreshRelayPanel(force=false){
  const panel = document.getElementById('relayBaseUrl');
  if(!panel) return;
  const now = Date.now();
  if(!force && now - _relayLastRefresh < 15000) return;
  _relayLastRefresh = now;
  try{
    const m = await (await fetch('/api/model-config')).json();
    _syncRelayPanel(m, force);
  }catch(e){
    _setRelayStatus('读取中转站配置失败：'+e.message, 'fail');
  }
}
async function saveRelayConfig(){
  const el=document.getElementById('relayStatus');
  if(el){ el.textContent='保存中…'; el.style.color='var(--mut)'; }
  try{
    const r=await (await fetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_modelConfigBody('relay'))})).json();
    _setRelayStatus(r.ok ? '✓ '+r.message : '✗ '+r.error, r.ok ? 'ok' : 'fail');
    if(r.ok){
      toast('中转站已保存，低成本后端会优先进入候选');
      _relayLastRefresh = 0;
      await refreshRelayPanel(true);
      await load();
    }
  }catch(e){ _setRelayStatus('保存失败：'+e.message, 'fail'); }
}
async function testRelayConfig(){
  const el=document.getElementById('relayStatus');
  if(el){ el.textContent='测试 /v1/models 中…'; el.style.color='var(--mut)'; }
  try{
    const r=await (await fetch('/api/model-config/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_modelConfigBody('relay'))})).json();
    _setRelayStatus(r.ok ? '✓ 中转站可用：HTTP '+r.status : '✗ 中转站测试失败：'+(r.error||r.status||'unknown'), r.ok ? 'ok' : 'fail');
  }catch(e){ _setRelayStatus('测试失败：'+e.message, 'fail'); }
}
async function saveModelConfig(){
  const el=document.getElementById('modelCfgStatus');
  el.textContent='保存中…'; el.style.color='var(--mut)';
  try{
    const r=await (await fetch('/api/model-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_modelConfigBody())})).json();
    el.textContent = r.ok ? '✓ '+r.message : '✗ '+r.error;
    el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
    if(r.ok){
      toast('已优先使用 sub2api 额度');
      _relayLastRefresh = 0;
      await refreshRelayPanel(true);
      await load();
    }
  }catch(e){ el.textContent='保存失败：'+e.message; el.style.color='var(--fail)'; }
}
async function testModelConfig(){
  const el=document.getElementById('modelCfgStatus');
  el.textContent='测试 /v1/models 中…'; el.style.color='var(--mut)';
  try{
    const r=await (await fetch('/api/model-config/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_modelConfigBody())})).json();
    el.textContent = r.ok ? '✓ 接口可用：HTTP '+r.status : '✗ 接口测试失败：'+(r.error||r.status||'unknown');
    el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
  }catch(e){ el.textContent='测试失败：'+e.message; el.style.color='var(--fail)'; }
}
async function saveProfileConfig(){
  const el=document.getElementById('profileCfgStatus');
  const body={
    email:document.getElementById('cfgProfileEmail').value.trim(),
    display_name:document.getElementById('cfgProfileName').value.trim(),
    organization:document.getElementById('cfgProfileOrg').value.trim(),
  };
  el.textContent='保存中…'; el.style.color='var(--mut)';
  try{
    const r=await (await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    el.textContent = r.ok ? '✓ '+r.message : '✗ '+r.error;
    el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
    if(r.ok){
      toast('邮箱身份已保存');
      await load();
    }
  }catch(e){ el.textContent='保存失败：'+e.message; el.style.color='var(--fail)'; }
}
async function saveCommercialConfig(){
  const el=document.getElementById('commercialCfgStatus');
  if(el){ el.textContent='保存中…'; el.style.color='var(--mut)'; }
  try{
    const r=await (await fetch('/api/commercial-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_commercialConfigBody())})).json();
    if(r.ok) _syncCommercialConfig(r);
    if(el){
      el.textContent = r.ok ? '✓ '+r.message : '✗ '+r.error;
      el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
    }
    if(r.ok){
      toast('登录与付费配置已保存');
      await load();
    }
  }catch(e){
    if(el){ el.textContent='保存失败：'+e.message; el.style.color='var(--fail)'; }
  }
}
async function saveNotifyConfig(){
  const body={
    smtp_host:document.getElementById('cfgSmtpHost').value.trim(),
    smtp_port:parseInt(document.getElementById('cfgSmtpPort').value)||587,
    use_tls:document.getElementById('cfgUseTls').checked,
    username:document.getElementById('cfgUsername').value.trim(),
    password:document.getElementById('cfgPassword').value,
    sender:document.getElementById('cfgSender').value.trim(),
    recipient:document.getElementById('cfgRecipient').value.trim(),
  };
  const el=document.getElementById('cfgStatus');
  el.textContent='保存中…'; el.style.color='var(--mut)';
  try{
    const r=await (await fetch('/api/notifications/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    el.textContent = r.ok ? '✓ '+r.message : '✗ '+r.error;
    el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
  }catch(e){ el.textContent='保存失败：'+e.message; el.style.color='var(--fail)'; }
}
async function testNotify(){
  const el=document.getElementById('cfgStatus');
  el.textContent='发送中…'; el.style.color='var(--mut)';
  try{
    const r=await (await fetch('/api/notifications/test',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    el.textContent = r.ok ? '✓ 测试邮件已发送，请检查收件箱' : '✗ 发送失败：'+(r.error||JSON.stringify(r.result));
    el.style.color = r.ok ? 'var(--ok)' : 'var(--fail)';
  }catch(e){ el.textContent='发送失败：'+e.message; el.style.color='var(--fail)'; }
}
async function exportDiagnostic(){
  try{
    const d = await (await fetch('/api/diagnostic')).json();
    const blob = new Blob([JSON.stringify(d, null, 2)], {type:'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'pacer-diagnostic-' + new Date().toISOString().replace(/[:.]/g,'-').slice(0,19) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('诊断包已下载');
  }catch(e){ toast('导出失败：'+e.message, false); }
}

function _syncChatAgents(installedAgents, cheapBackend){
  const sel = document.getElementById('chatAgent');
  const cur = sel.value;
  const base = (installedAgents && installedAgents.length) ? installedAgents : _chatAgents;
  let opts = base.filter(a => a !== 'codex');
  if(cheapBackend){ opts = [cheapBackend, ...opts.filter(a=>a!==cheapBackend)]; }
  if(!opts.length) opts.push('claude-code');
  sel.innerHTML = opts.map(a=>`<option value="${esc(a)}"${a===cur?' selected':''}>${esc(_agentLabel(a))}</option>`).join('');
  if(!sel.value && opts.length) sel.value = opts[0];
}

function clearChat(){
  _chatHistory = [];
  document.getElementById('chatMsgs').innerHTML = '<div class="chat-empty" id="chatEmpty">对 AI 说点什么吧。<br>对话历史保存在本页面，刷新后清空。</div>';
}

function chatKeyDown(e){
  if(e.key==='Enter' && e.ctrlKey){ e.preventDefault(); sendChat(); }
}

function _renderBubble(role, content){
  const now = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  return `<div class="chat-msg ${role}">
    <div class="chat-bubble">${esc(content)}</div>
    <div class="chat-msg-time">${role==='user'?'你':'AI'} · ${now}</div>
  </div>`;
}

function _appendChat(role, content){
  const emptyEl = document.getElementById('chatEmpty');
  if(emptyEl) emptyEl.remove();
  const msgsEl = document.getElementById('chatMsgs');
  msgsEl.insertAdjacentHTML('beforeend', _renderBubble(role, content));
  _chatHistory.push({role, content});
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

async function sendChat(){
  const inp = document.getElementById('chatInput');
  const msg = inp.value.trim();
  if(!msg) return;
  const agent = document.getElementById('chatAgent').value || 'claude-code';

  _appendChat('user', msg);
  inp.value = '';

  const sendBtn = document.getElementById('chatSendBtn');
  const thinking = document.getElementById('chatThinking');
  sendBtn.disabled = true;
  thinking.style.display = 'inline';
  const msgsEl = document.getElementById('chatMsgs');
  msgsEl.scrollTop = msgsEl.scrollHeight;

  try{
    const r = await (await fetch('/api/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: msg, agent, history: _chatHistory.slice(-16)})
    })).json();
    const reply = r.ok ? (r.reply||'（无回复）') : ('错误：'+(r.error||'未知'));
    _appendChat('assistant', reply);
  }catch(e){
    _appendChat('assistant', '请求失败：'+e);
  }finally{
    sendBtn.disabled = false;
    thinking.style.display = 'none';
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }
}

initCollapsiblePanels();
initWorkbenchViews();
connectEventStream();
load();
setInterval(load, 10000);
