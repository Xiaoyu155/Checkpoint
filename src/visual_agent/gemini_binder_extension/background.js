/* Bind one browser profile to one account; synchronise an existing login. */
importScripts('core.js');
const ALARM = 'gemini_binding_sync_v2';
const SESSION_CHECK_INTERVAL_MS = 30 * 60 * 1000;
let active = null, debounce;
async function ensureAlarm() {
  if (!(await chrome.alarms.get(ALARM))) await chrome.alarms.create(ALARM, {periodInMinutes: 10});
}
async function state(value) {
  await chrome.storage.local.set({bindingStatus: {...value, checkedAt: Date.now()}});
  await chrome.action.setBadgeText({text: value.pending ? '…' : value.ok ? '' : '!'});
  if (!value.ok) await chrome.action.setBadgeBackgroundColor({color: value.pending ? '#2563a3' : '#b45309'});
  return value;
}
async function cookieStore() {
  const tabs = await chrome.tabs.query({active: true, currentWindow: true});
  const stores = await chrome.cookies.getAllCookieStores();
  return stores.find(s => s.tabIds.includes(tabs[0]?.id))?.id || stores.find(s => s.id === '0')?.id || stores[0]?.id;
}
async function readSession(storeId) {
  const names = ['__Secure-1PSID', '__Secure-1PSIDTS', '__Secure-1PSIDCC'];
  return GeminiBinding.session(await Promise.all(names.map(name => chrome.cookies.get({url: 'https://gemini.google.com/', name, storeId}))));
}
async function request(url, adminKey, payload) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + adminKey}, body: JSON.stringify(payload), signal: controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '网关返回 HTTP ' + response.status);
    return GeminiBinding.verified(data);
  } finally { clearTimeout(timer); }
}
async function run(message = {}) {
  const config = await chrome.storage.local.get(['gatewayUrl','adminKey','bindingV2','autoSyncEnabled','lastAttemptAt']);
  const manual = message.action === 'bind';
  if (!manual && config.autoSyncEnabled === false) return state({ok:true,message:'自动同步已关闭'});
  if (!manual && !config.bindingV2) return state({ok:false,message:'请先绑定当前浏览器配置'});
  if (!manual && Date.now()-(config.lastAttemptAt||0)<120000) return config.bindingV2;
  const base=GeminiBinding.baseUrl(message.gatewayUrl||config.gatewayUrl||''), key=message.adminKey||config.adminKey;
  if (!key) throw new Error('请输入网关管理密钥');
  const storeId=manual?await cookieStore():config.bindingV2.storeId;
  const cookies=await readSession(storeId), identity=await GeminiBinding.hash(cookies.psid), digest=await GeminiBinding.hash(JSON.stringify(cookies));
  const binding=config.bindingV2;
  if (!manual && identity!==binding.psidHash) return state({ok:false,needsRebind:true,message:'浏览器会话已变化，已暂停同步。请确认当前账号后重新绑定。'});
  if (!manual && digest===binding.cookieDigest && Date.now()-(binding.syncedAt||0)<SESSION_CHECK_INTERVAL_MS) return state({ok:true,message:'会话凭据未变化，尚未到会话复查时间',model:binding.model});
  await chrome.storage.local.set({lastAttemptAt:Date.now()});
  const id=manual?(message.accountId||'').trim():binding.id;
  await state({ok:false,pending:true,message:manual?'正在请求网关验证 Gemini 4，请稍候；关闭弹窗后可以重新打开查看结果。':'正在同步登录会话…'});
  const data=await request(base+(manual?'/admin/bindings':'/admin/bindings/sync'),key,
    {id,...cookies,expected_psid_hash:binding?.id===id?binding.serverHash:'',replace_existing:manual&&message.replaceExisting===true});
  const next={id:data.account.id,psidHash:identity,serverHash:data.binding_hash,cookieDigest:digest,storeId,
    model:data.test_result.verified_model||binding?.model||'',verifiedAt:manual?Date.now():binding.verifiedAt,syncedAt:Date.now()};
  await chrome.storage.local.set({gatewayUrl:base,adminKey:key,bindingV2:next,autoSyncEnabled:manual?message.autoSync!==false:config.autoSyncEnabled});
  return state({ok:true,message:manual?'已验证 Gemini 4 并保存绑定':'登录会话已同步；未重复消耗生成额度',model:next.model,accountId:next.id});
}
function synchronise(message={}) {
  if (active) return message.action==='bind'?Promise.resolve({ok:false,message:'已有同步正在运行，请稍后再绑定'}):active;
  active=run(message).catch(error=>state({ok:false,message:error.name==='AbortError'?'网关验证超时，请稍后重试':error.message})).finally(()=>{active=null;});
  return active;
}
function startSessionSync() {
  ensureAlarm().then(()=>synchronise()).catch(()=>{});
}
chrome.runtime.onInstalled.addListener(startSessionSync);
chrome.runtime.onStartup.addListener(startSessionSync);
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name===ALARM)synchronise();});
chrome.cookies.onChanged.addListener(({cookie})=>{
  if(!['google.com','.google.com'].includes(cookie.domain)||!['__Secure-1PSID','__Secure-1PSIDTS','__Secure-1PSIDCC'].includes(cookie.name))return;
  clearTimeout(debounce);debounce=setTimeout(()=>synchronise(),5000);
});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message.action==='hello'){reply({version:'2.0.3',manifestVersion:chrome.runtime.getManifest().version});return false;}
  if(['bind','sync'].includes(message.action)){synchronise(message).then(reply);return true;}
  if(message.action==='inspect'){
    (async()=>{const id=await cookieStore(),data=await readSession(id);return {ready:Boolean(data.psid&&data.psidts),storeId:id};})().then(reply).catch(error=>reply({ready:false,message:error.message}));return true;
  }
});
ensureAlarm().catch(()=>{});
