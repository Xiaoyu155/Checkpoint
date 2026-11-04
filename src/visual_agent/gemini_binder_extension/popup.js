const el=id=>document.getElementById(id);
const VERSION='2.0.3';
let submitting=false, busyTimer;
function show(value){
 value=value||{ok:false,message:'插件后台没有回复，请点击“重新加载插件”后重试',needsReload:true};
 el('status').textContent=value.message||'等待绑定';
 el('status').className='notice '+(value.ok?'ok':value.pending?'pending':'bad');
 if(value.needsReload)el('reloadExtension').hidden=false;
}
function busy(on){
 el('bind').disabled=on;el('bind').textContent=on?'正在实际调用 Gemini 4 验证…':'验证 Gemini 4 并绑定';
 clearInterval(busyTimer);
 if(on){let seconds=0;busyTimer=setInterval(()=>{seconds++;el('bind').textContent='正在验证… '+seconds+' 秒';},1000);}
}
async function send(message,timeout=8000){
 let timer;
 try{return await Promise.race([chrome.runtime.sendMessage(message),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('插件后台未响应，请点击“重新加载插件”后重试')),timeout);})]);}
 finally{clearTimeout(timer);}
}
async function handshake(){
 try{
  const hello=await send({action:'hello'});
  if(hello?.version!==VERSION||hello?.manifestVersion!==VERSION)throw Error('插件文件已更新，但浏览器后台仍是旧版本');
  el('workerVersion').textContent='后台 v'+hello.version;return true;
 }catch(error){el('workerVersion').textContent='后台需要重新加载';show({ok:false,message:error.message+'。请点击下方“重新加载插件”，然后重新打开插件。',needsReload:true});return false;}
}
function details(binding){
 if(!binding)return;
 el('accountId').value=binding.id;el('bound').textContent='当前绑定：'+binding.id;
 el('model').textContent='已验证模型：'+(binding.model||'未知');
 el('verifiedAt').textContent='最近生成验证：'+new Date(binding.verifiedAt).toLocaleString();
}
async function load(){
 const data=await chrome.storage.local.get(['gatewayUrl','adminKey','autoSyncEnabled','bindingV2','bindingStatus','accId']);
 el('gatewayUrl').value=data.gatewayUrl||'';el('adminKey').value=data.adminKey||'';
 el('autoSync').checked=data.autoSyncEnabled!==false;el('accountId').value=data.accId||'';details(data.bindingV2);
 if(!(await handshake()))return;
 const check=await send({action:'inspect'});
 if(!check?.ready)show({ok:false,message:check?.message||'请先在此配置中登录 Gemini'});
 else show(data.bindingStatus||{ok:true,message:'登录凭据已找到，点击按钮进行实际模型验证'});
}
el('autoSync').addEventListener('change',()=>chrome.storage.local.set({autoSyncEnabled:el('autoSync').checked}));
el('bindingForm').addEventListener('submit',async event=>{
 event.preventDefault();if(submitting)return;
 try{
  const raw=el('gatewayUrl').value.trim();
  if(!raw){show({ok:false,message:'请填写反代网关地址'});el('gatewayUrl').focus();return;}
  const base=GeminiBinding.baseUrl(raw),key=el('adminKey').value.trim();
  if(!key){show({ok:false,message:'请填写网关管理密钥，再点击绑定。这个密钥属于反代网关，不是 Google 密码。'});el('adminKey').focus();return;}
  // Request permissions directly in the click gesture, before any await.
  const permission=chrome.permissions.request({origins:[new URL(base).origin+'/*']});
  submitting=true;busy(true);show({pending:true,message:'正在检查网关权限和插件后台…'});
  if(!(await permission))throw Error('未获得访问此反代网关的权限，请允许插件访问该地址后重试');
  if(!(await handshake()))return;
  show({pending:true,message:'正在读取当前浏览器的 Gemini 登录并请求网关验证。通常需要 20–40 秒；可以在独立页面中等待。'});
  const result=await send({action:'bind',gatewayUrl:base,adminKey:key,accountId:el('accountId').value.trim(),autoSync:el('autoSync').checked,replaceExisting:el('replaceExisting').checked},100000);
  show(result);
  if(result?.ok){const data=await chrome.storage.local.get(['bindingV2']);details(data.bindingV2);}
 }catch(error){show({ok:false,message:error.message,needsReload:/后台|Receiving end|message port|context invalidated/i.test(error.message)});}
 finally{submitting=false;busy(false);}
});
el('reloadExtension').addEventListener('click',async()=>{
 try{
  await chrome.storage.local.set({gatewayUrl:el('gatewayUrl').value.trim(),adminKey:el('adminKey').value.trim(),accId:el('accountId').value.trim()});
  chrome.runtime.reload();
 }catch(error){show({ok:false,message:'请到 chrome://extensions 点击本插件的重新加载：'+error.message});}
});
el('openPanel').addEventListener('click',async()=>{
 await chrome.storage.local.set({gatewayUrl:el('gatewayUrl').value.trim(),adminKey:el('adminKey').value.trim(),accId:el('accountId').value.trim()});
 await chrome.tabs.create({url:chrome.runtime.getURL('popup.html')});
});
chrome.storage.onChanged.addListener((changes,area)=>{
 if(area!=='local')return;
 if(changes.bindingV2)details(changes.bindingV2.newValue);
 if(changes.bindingStatus)show(changes.bindingStatus.newValue);
});
load().catch(error=>show({ok:false,message:error.message,needsReload:true}));
