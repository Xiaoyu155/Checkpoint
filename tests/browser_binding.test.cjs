const test=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),vm=require('vm');
const crypto=require('node:crypto').webcrypto;
const folder=path.resolve(__dirname,'../vps_gemini4_gateway/gemini_binder_extension');
const VERSION=JSON.parse(fs.readFileSync(path.join(folder,'manifest.json'),'utf8')).version;
const core=require(path.join(folder,'core.js'));
function cookie(name,value,extra={}){return {name,value,domain:'.google.com',path:'/',storeId:'0',...extra};}
async function harness(config,values,response){
 const saved={...config},requests=[],listeners=[],updates=[];
 const noop={addListener(){}};
 const chrome={runtime:{onInstalled:noop,onStartup:noop,onMessage:{addListener(fn){listeners.push(fn)}},getManifest(){return {version:VERSION}}},alarms:{async get(){return {}},async create(){},onAlarm:noop},
  tabs:{async query(){return [{id:1}]}},cookies:{async getAllCookieStores(){return [{id:'0',tabIds:[1]}]},async get({name}){return values.find(x=>x.name===name)||null},onChanged:noop},
  storage:{local:{async get(){return {...saved}},async set(data){updates.push(data);Object.assign(saved,data)}}},action:{async setBadgeText(){},async setBadgeBackgroundColor(){}}};
 const context=vm.createContext({chrome,crypto,TextEncoder,URL,Date,console,AbortController,setTimeout,clearTimeout,fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>response}},importScripts(){}});
 vm.runInContext(fs.readFileSync(path.join(folder,'core.js'),'utf8'),context);
 vm.runInContext(fs.readFileSync(path.join(folder,'background.js'),'utf8'),context);
 return {saved,requests,updates,run:(m)=>vm.runInContext('synchronise('+JSON.stringify(m)+')',context),message:(m)=>new Promise(resolve=>listeners[0](m,{},resolve))};
}
test('remote HTTP and credential-bearing URLs rejected',()=>{
 assert.throws(()=>core.baseUrl('http://remote.example'));
 assert.throws(()=>core.baseUrl('https://user:password@example.com'));
 assert.equal(core.baseUrl('https://example.com/gemini4/'),'https://example.com/gemini4');
});
test('mixed cookie scopes rejected',()=>assert.throws(()=>core.session([cookie('__Secure-1PSID','a'),cookie('__Secure-1PSIDTS','b',{domain:'gemini.google.com'})])));
test('validation failure cannot be recorded as binding success',async()=>{
 const h=await harness({},[cookie('__Secure-1PSID','account-a'),cookie('__Secure-1PSIDTS','ts')],{success:true,account:{id:'a'},test_result:{valid:false,message:'expired'}});
 const result=await h.run({action:'bind',gatewayUrl:'https://example.com',adminKey:'test'});
 assert.equal(result.ok,false);assert.equal(h.saved.bindingV2,undefined);
});
test('account switch stops before overwriting the saved account',async()=>{
 const oldHash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('old-account'));
 const hash=Buffer.from(oldHash).toString('hex');
 const h=await harness({gatewayUrl:'https://example.com',adminKey:'test',bindingV2:{id:'original',psidHash:hash,storeId:'0'}},[cookie('__Secure-1PSID','different-account'),cookie('__Secure-1PSIDTS','ts')],{});
 const r=await h.run({action:'sync'});assert.equal(r.needsRebind,true);assert.equal(h.requests.length,0);
});
test('manual binding uses Authorization header and only commits a verified response',async()=>{
 const h=await harness({},[cookie('__Secure-1PSID','account-a'),cookie('__Secure-1PSIDTS','ts')],{success:true,binding_hash:'server-hash',account:{id:'a'},test_result:{valid:true,verified_model:'4 Argon'}});
 const r=await h.run({action:'bind',gatewayUrl:'https://example.com/gemini4',adminKey:'test-key',accountId:'a'});
 assert.equal(r.ok,true);assert.equal(h.saved.bindingV2.id,'a');assert.equal(h.requests[0].url,'https://example.com/gemini4/admin/bindings');
 assert.ok(h.updates.some(update=>update.bindingStatus?.pending===true));
 assert.equal(h.requests[0].options.headers.Authorization,'Bearer test-key');assert.ok(!h.requests[0].url.includes('test-key'));
});

test('popup can detect whether its background worker was reloaded',async()=>{
 const h=await harness({},[],{}), reply=await h.message({action:'hello'});
 assert.equal(reply.version,VERSION);assert.equal(reply.manifestVersion,VERSION);
});

test('unchanged browser credentials still trigger an overdue session-only check',async()=>{
 const values=[cookie('__Secure-1PSID','account-a'),cookie('__Secure-1PSIDTS','ts')];
 const binding={id:'a',psidHash:await core.hash('account-a'),cookieDigest:await core.hash(JSON.stringify(core.session(values))),storeId:'0',syncedAt:Date.now()-31*60*1000,verifiedAt:123,model:'4 Argon'};
 const h=await harness({gatewayUrl:'https://example.com',adminKey:'test',bindingV2:binding},values,{success:true,binding_hash:'server-hash',account:{id:'a'},test_result:{valid:true,verified_model:'4 Argon'}});
 const result=await h.run({action:'sync'});
 assert.equal(result.ok,true);
 assert.equal(h.requests.length,1);
 assert.equal(h.requests[0].url,'https://example.com/admin/bindings/sync');
 assert.equal(h.saved.bindingV2.verifiedAt,123);
 assert.ok(h.saved.bindingV2.syncedAt>binding.syncedAt);
});

test('recent successful session checks do not cause repeated requests',async()=>{
 const values=[cookie('__Secure-1PSID','account-a'),cookie('__Secure-1PSIDTS','ts')];
 const binding={id:'a',psidHash:await core.hash('account-a'),cookieDigest:await core.hash(JSON.stringify(core.session(values))),storeId:'0',syncedAt:Date.now()};
 const h=await harness({gatewayUrl:'https://example.com',adminKey:'test',bindingV2:binding},values,{});
 assert.equal((await h.run({action:'sync'})).ok,true);
 assert.equal(h.requests.length,0);
});

async function recoveryPage(boundId,version=VERSION){
 const output={textContent:''},updates=[],messages=[];let reloads=0;
 const chrome={storage:{local:{async get(){return {bindingV2:{id:boundId},autoSyncEnabled:false}},async set(value){updates.push(value)}}},runtime:{getManifest(){return {version}},reload(){reloads++},async sendMessage(value){messages.push(value);return {ok:true,message:'synced'}}}};
 const document={title:'',getElementById(){return output}};
 const context=vm.createContext({chrome,document,location:{href:'chrome-extension://test/recover.html?account=expected'},URL,Date});
 await vm.runInContext(fs.readFileSync(path.join(folder,'recover.js'),'utf8'),context);
 return {output,updates,messages,reloads,document};
}
test('recovery entry stops on an unexpected account before changing preferences',async()=>{
 const result=await recoveryPage('different-account');
 assert.equal(result.updates.length,0);assert.equal(result.messages.length,0);assert.equal(result.reloads,0);
 assert.ok(result.output.textContent.includes('不一致'));
});
test('recovery uses session sync for the existing binding and reloads only an old extension',async()=>{
 const current=await recoveryPage('expected');
 assert.equal(current.messages.length,1);assert.equal(current.messages[0].action,'sync');assert.equal(current.reloads,0);
 assert.equal(current.updates[0].pacerRecovery.accountId,'expected');
 const old=await recoveryPage('expected','2.0.1');
 assert.equal(old.messages.length,0);assert.equal(old.reloads,1);
});
