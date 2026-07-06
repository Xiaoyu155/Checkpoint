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
  quota_exhausted:     { zh:'AI 额度已用完', fix:'等额度恢复（Codex 每周 / Claude 每 5 小时），或切换编码 AI' },
  needs_clarification: { zh:'目标不够具体无法执行', fix:'把目标改写为：做什么 + 改哪里 + 什么叫完成' },
  verification_failed: { zh:'验收命令仍然失败', fix:'打开最终报告看具体报错，重新创建更精确的任务' },
  command_timeout:     { zh:'验收命令超时', fix:'加快测试速度，或先跑部分测试验证思路' },
  command_launch_error:{ zh:'验收命令无法启动', fix:'先手动运行一次验收命令确认它能跑' },
  test_command_invalid:{ zh:'验收命令有误', fix:'检查测试命令是否正确，先手动运行确认' },
  inspection_only:     { zh:'仅检查验收（无交互）', fix:'加 --run-profile supervised 做真实交互验收' },
};
function srInfo(code){ return STOP_REASON[code] || { zh: code||'', fix:'' }; }

const STATUS_ZH = {
  created:'待执行', running:'执行中', preview_running:'预览中', preview:'预览完成',
  verified:'验收通过', merged:'已合并', archived:'已删除', stopped:'已停止', failed:'失败',
};
const zh = s => STATUS_ZH[s] || s;
const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const jsq = s => String(s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'');

function stClass(s){
  const t=(s||'').toLowerCase();
  if(t==='verified') return 'st-verified';
  if(t==='running'||t==='preview_running') return 'st-running';
  if(t==='stopped'||t==='failed') return 'st-stopped';
  if(t==='merged') return 'st-merged';
  if(t==='preview') return 'st-preview';
  return '';
}
function pc(s){
  const t=(s||'').toLowerCase();
  if(t.includes('pass')||t.includes('verified')||t.includes('merged')) return 'ok';
  if(t.includes('fail')||t.includes('error')||t.includes('conflict')) return 'fail';
  if(t.includes('clarif')||t.includes('coverage')||t.includes('budget')||
     t.includes('stopped')||t.includes('exhausted')||t.includes('repeated')) return 'warn';
  if(t.includes('running')||t.includes('preview')) return 'acc';
  return 'mut';
}

// ---------- Toast ----------
let _toastTimer;
function toast(msg, ok=true){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.style.borderColor=ok?'rgba(63,185,80,.4)':'rgba(248,81,73,.4)';
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>el.classList.remove('show'),3200);
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
  _workerRunning = !!(w&&w.running);
  const dot = document.getElementById('wDot');
  const lbl = document.getElementById('wLabel');
  const btn = document.getElementById('btnWorker');
  dot.className = 'wdot' + (_workerRunning?' on':'');
  lbl.textContent = _workerRunning ? 'Worker 运行中' : 'Worker 已停止';
  btn.textContent = _workerRunning ? '停止' : '启动 Worker';
  btn.className = _workerRunning ? 'danger' : 'ghost';
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
  const goal = document.getElementById('fGoal').value.trim();
  const msg = document.getElementById('launchMsg');
  if(!goal){ msg.textContent='请先填写目标。'; return; }
  const repoRoot = document.getElementById('fRepo').value.trim() || '';
  const testCommand = document.getElementById('fTest').value.trim();
  const agent = document.getElementById('fAgent').value;
  const body = {
    goal,
    repo_root: repoRoot,
    test_command: testCommand,
    agent,
    merge_policy: document.getElementById('fAutoMerge').checked ? 'auto' : 'manual',
    execute: !!execute,
    spec: {
      schema_version: 1,
      goal,
      scope: [{repo_root: repoRoot || '.', agent: agent || 'default', mode: execute ? 'execute' : 'preview'}],
      plan: [goal],
      test: [testCommand || 'auto-detect verification command'],
      risk: ['Workbench-generated spec; human review is required before merge.'],
      rollback: ['Keep merge policy manual unless explicitly approved.']
    }
  };
  document.getElementById('btnPreview').disabled = true;
  document.getElementById('btnRun').disabled = true;
  msg.textContent = execute ? '派发中…' : '生成预览中…';
  try{
    const r = await (await fetch('/api/mission/start',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(r.ok){
      msg.textContent = execute ? '✓ 已派发' : '✓ 预览生成中';
      document.getElementById('fGoal').value = '';
      toast(execute ? '✓ 任务已派发，在「执行中」列查看' : '✓ 预览生成中，在「待办」列查看');
    } else {
      msg.textContent = '错误：'+esc(r.error||'未知');
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

function openTaskIntake(seedGoal){
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

function mainChatKeyDown(e){
  if(e.key==='Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); sendMainChat(); }
}

function _resetMainChat(){
  const el = document.getElementById('mainChatMsgs');
  if(!el) return;
  el.innerHTML = '<div class="main-chat-empty">直接描述你要做的事。Pacer 会追问目标、项目、命令和完成标准，确认后再创建 mission。</div>';
  document.getElementById('mainIntakeActions').style.display='none';
}

function _appendMainChat(role, content){
  const emptyEl = document.querySelector('#mainChatMsgs .main-chat-empty');
  if(emptyEl) emptyEl.remove();
  const el = document.getElementById('mainChatMsgs');
  el.insertAdjacentHTML('beforeend', _renderBubble(role, content));
  el.scrollTop = el.scrollHeight;
}

function sendMainChat(){
  const input = document.getElementById('fGoal');
  const msg = input.value.trim();
  if(!msg){ toast('先写一句你要 Pacer 做什么', false); return; }
  const seed = {
    goal: msg,
    repo: document.getElementById('fRepo').value.trim(),
    test: document.getElementById('fTest').value.trim(),
    agent: document.getElementById('fAgent').value || 'codex'
  };
  _appendMainChat('user', msg);
  const questions = _intakeQuestions(seed.goal, seed.repo, seed.test);
  _intakeDraft = _buildIntakeDraft(seed, _answerTemplate(seed.goal));
  _intakeSeed = seed;
  _appendMainChat('assistant', [
    '我会先把任务收口成可执行合同。需要确认的点：',
    '',
    ...questions.map((q,i)=>`${i+1}. ${q}`),
    '',
    '我按默认假设生成了一版执行稿，你可以直接采用，也可以继续改输入再确认。',
    '',
    _intakeDraft
  ].join('\n'));
  document.getElementById('mainIntakeActions').style.display='flex';
}

function applyMainDraftToMission(){
  if(!_intakeDraft) return toast('还没有执行稿，请先点“确认任务”', false);
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
  const btn = ev.target; btn.disabled=true; btn.textContent='重试中…';
  try{
    const r = await (await fetch('/api/mission/retry',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mission_id:id})})).json();
    if(r.ok){ closeDrawer(); toast('✓ 已重新派发'); }
    else{ toast('重试失败：'+(r.error||'未知'), false); btn.disabled=false; btn.textContent='↺ 重试'; }
  }catch(e){ toast('重试失败：'+e, false); btn.disabled=false; btn.textContent='↺ 重试'; }
  load();
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
    btn.textContent = '全部删除';
  }
  load();
}

// ---------- 看板 ----------
function missionCard(m){
  const sr = srInfo(m.stop_reason);
  const isStopped = m.status==='stopped'||m.status==='failed';
  const canDelete = !['running','preview_running','starting','verified','merged','archived'].includes(String(m.status||''));
  const hasStop = m.stop_reason && m.stop_reason!=='verified';
  const mergeBtn = m.can_merge
    ? `<button class="act-merge" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并</button>` : '';
  const retryBtn = isStopped && hasStop
    ? `<button class="act-retry" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 重试</button>` : '';
  const deleteBtn = canDelete
    ? `<button class="act-delete" onclick="deleteMission(event,'${esc(m.mission_id)}')">删除</button>` : '';
  const clarifyBtn = (m.stop_reason==='needs_clarification' || m.stop_reason==='coverage_gap')
    ? `<button class="act-retry" onclick="event.stopPropagation();openTaskIntake('${jsq(m.objective||'')}')">继续确认</button>` : '';
  const stopTag = hasStop && !m.can_merge
    ? `<span class="stop-tag">${esc(sr.zh)}</span>` : '';
  const agent = m.agent ? `<span class="pill mut" style="font-size:10px;">${esc(m.agent)}</span>` : '';
  return `<div class="tcard ${stClass(m.status)}" onclick="detail('${esc(m.mission_id)}')">
    <div class="tcard-goal">${esc(m.objective||'（无目标）')}</div>
    <div class="tcard-foot">
      <span class="pill ${pc(m.status)}">${esc(zh(m.status))}</span>
      ${agent}${stopTag}
      <span class="tcard-actions">${mergeBtn}${clarifyBtn}${retryBtn}${deleteBtn}</span>
    </div>
    <div class="tcard-meta">${esc((m.mission_id||'').slice(0,17))}</div>
  </div>`;
}
function launchCard(l){
  const p = l.state==='error'?'fail':'acc';
  const txt = l.state==='error'?'启动失败':(l.execute?'派发中…':'预览中…');
  const extra = l.state==='error'?`<div class="tcard-meta" style="color:var(--fail)">${esc(l.error||'')}</div>`:'';
  return `<div class="tcard st-running"><div class="tcard-goal">${esc(l.goal)}</div>
    <div class="tcard-foot"><span class="pill ${p}">${txt}</span></div>${extra}</div>`;
}
function renderBoard(d){
  const cols={todo:[],in_progress:[],in_review:[],done:[]};
  const missions = d.missions||[];
  const runObjs = new Set(missions.filter(m=>m.board_column==='in_progress').map(m=>m.objective));
  (d.launches||[]).forEach(l=>{
    if(l.state==='starting'&&!runObjs.has(l.goal)) cols.in_progress.push(launchCard(l));
    else if(l.state==='error') cols.in_review.push(launchCard(l));
  });
  missions.forEach(m=>{ (cols[m.board_column]||cols.in_review).push(missionCard(m)); });
  const emp=t=>`<div class="empty">${t}</div>`;
  document.getElementById('colTodo').innerHTML=cols.todo.join('')||emp('暂无待办');
  document.getElementById('colRun').innerHTML=cols.in_progress.join('')||emp('无执行中的任务');
  document.getElementById('colReview').innerHTML=cols.in_review.join('')||emp('没有等待合并的任务');
  document.getElementById('colDone').innerHTML=cols.done.join('')||emp('还没有完成的任务');
  document.getElementById('cTodo').textContent=cols.todo.length;
  document.getElementById('cRun').textContent=cols.in_progress.length;
  document.getElementById('cReview').textContent=cols.in_review.length;
  document.getElementById('cDone').textContent=cols.done.length;
  document.getElementById('sRunning').textContent=cols.in_progress.length;

  const worker=d.worker||{};
  const status=d.status||{};
  const meta=document.getElementById('pageMeta');
  const agents=(d.installed_agents||[]).slice(0,4);
  meta.innerHTML=[
    `<span class="pill ${worker.running?'ok':'mut'}">${worker.running?'Worker 运行中':'Worker 空闲'}</span>`,
    `<span class="pill acc">${esc(status.state||'unknown')}</span>`,
    agents.length?`<span class="pill mut">${agents.map(esc).join(' · ')}</span>`:''
  ].filter(Boolean).join('');

  const totalTokens=((d.value?.input_tokens||0)+(d.value?.output_tokens||0));
  document.getElementById('kVerified').textContent=String(d.value?.verified||0);
  document.getElementById('kRunning').textContent=String(cols.in_progress.length);
  document.getElementById('kQueue').textContent=String((d.queue||[]).length);
  document.getElementById('kSavedUsd').textContent='$'+Number(d.value?.mimo_efficiency?.saved_usd||0).toFixed(2);
  document.getElementById('kSavedMin').textContent=Number(d.value?.mimo_efficiency?.saved_minutes||0).toFixed(1)+' 分钟';
  document.getElementById('kEfficiency').textContent=String(d.value?.mimo_efficiency?.efficiency_gain_percent||0)+'%';
  document.getElementById('kTokens').textContent='累计 Tokens '+totalTokens.toLocaleString();
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
    document.getElementById('sVerified').textContent=v.verified||0;
    const totalTok=(v.input_tokens||0)+(v.output_tokens||0);
    if(totalTok>0){
      document.getElementById('sTokens').textContent=totalTok.toLocaleString();
      document.getElementById('tokenStat').style.display='flex';
    }

    // MiMo efficiency metrics
    const me=v.mimo_efficiency||{};
    document.getElementById('mSavedUsd').textContent='$'+(me.saved_usd||0).toFixed(2);
    document.getElementById('mSavedMin').textContent=(me.saved_minutes||0).toFixed(1)+' 分钟';
    document.getElementById('mEfficiency').textContent=(me.efficiency_gain_percent||0)+'%';
    document.getElementById('mCapScore').textContent=(me.capability_score||0);
    document.getElementById('mMimoRuns').textContent=me.mimo_runs||0;

    renderBoard(d);

    // 队列
    const qEl=document.getElementById('queue');
    const qLen=(d.queue||[]).length;
    document.getElementById('sQueue').textContent=qLen;
    document.getElementById('cQueueSec').textContent=qLen;
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
  const sr=srInfo(m.stop_reason);
  const isStopped=m.status==='stopped'||m.status==='failed';
  const hasStop=m.stop_reason&&m.stop_reason!=='verified';
  const canRetry=isStopped&&hasStop;
  const canDelete=!['running','preview_running','starting','verified','merged','archived'].includes(String(m.status||''));

  const mergeBtn=d.can_merge?`<button class="act-merge" style="padding:6px 14px;font-size:12px;" onclick="mergeMission(event,'${esc(m.mission_id)}')">✓ 合并到主分支</button>`:'';
  const clarifyBtn=(m.stop_reason==='needs_clarification'||m.stop_reason==='coverage_gap')?`<button class="act-retry" style="padding:6px 14px;font-size:12px;" onclick="openTaskIntake('${jsq(m.objective||'')}')">继续确认任务</button>`:'';
  const retryBtn=canRetry?`<button class="act-retry" style="padding:6px 14px;font-size:12px;" onclick="retryMission(event,'${esc(m.mission_id)}')">↺ 用相同目标重试</button>`:'';
  const deleteBtn=canDelete?`<button class="act-delete" style="padding:6px 14px;font-size:12px;" onclick="deleteMission(event,'${esc(m.mission_id)}')">删除任务</button>`:'';
  const stopHint=hasStop?`<div class="stop-hint">
    <div>停止原因：<b>${esc(sr.zh)}</b></div>
    ${sr.fix?`<div class="fix">建议操作：${esc(sr.fix)}</div>`:''}
  </div>`:'';

  const rounds=(d.rounds||[]).map(r=>`<div class="round-row">
    <span>Round ${r.round}：<b>${esc(r.type)}</b></span>
    <span><span class="pill ${pc(r.status)}">${esc(r.status)}</span>
    ${r.stop_reason||r.reason?`<span class="muts">${esc(r.stop_reason||r.reason)}</span>`:''}</span>
  </div>`).join('');
  const live=d.live_logs||{};
  const liveBlock=live.latest_tail?`<h3>实时执行日志</h3><pre>${esc(live.latest_tail)}</pre>`:'';

  document.getElementById('drawerBody').innerHTML=
    `<h2>${esc(m.objective||id)}</h2>
     <p>状态：<span class="pill ${pc(m.status)}">${esc(zh(m.status))}</span>
     ${d.merge_state?' · 合并：'+esc(d.merge_state):''}</p>
     ${stopHint}
     <div class="action-row">${mergeBtn}${clarifyBtn}${retryBtn}${deleteBtn}</div>
     ${rv.verdict?`<p>验收命令：<code>${esc(rv.command||'工作流验收')}</code> · 结论：<span class="pill ${pc(rv.verdict)}">${esc(rv.verdict)}</span></p>`:''}
     ${diffSection(rv)}
     <h3>执行轮次</h3>${rounds||'<div class="empty">暂无轮次记录</div>'}
     ${liveBlock}
     <h3>最终报告</h3><pre>${esc(d.final_report)||'（暂无报告）'}</pre>`;
  document.getElementById('drawer').style.display='block';
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

load();
setInterval(load, 4000);
