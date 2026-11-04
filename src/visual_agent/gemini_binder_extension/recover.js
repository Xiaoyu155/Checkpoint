// One-time local repair entry. It only uses the existing binding and gateway.
(async()=>{
 const output=document.getElementById('status');
 try {
  const expected=new URL(location.href).searchParams.get('account');
  const data=await chrome.storage.local.get(['bindingV2','autoSyncEnabled']);
  if(!expected||data.bindingV2?.id!==expected)throw new Error('当前浏览器绑定与待恢复账号不一致，已停止。');
  document.title='Gemini account recovery - '+expected;
  await chrome.storage.local.set({autoSyncEnabled:true,lastAttemptAt:0,
    pacerRecovery:{accountId:expected,requestedAt:Date.now(),previousAutoSync:data.autoSyncEnabled}});
  if(chrome.runtime.getManifest().version!=='2.0.3'){
   output.textContent='账号绑定已核对，正在加载会话恢复更新…';
   chrome.runtime.reload();return;
  }
  const result=await chrome.runtime.sendMessage({action:'sync'});
  output.textContent=result?.message||'同步正在后台运行，可稍后查看。';
 } catch(error) {output.textContent=error.message;}
})();
