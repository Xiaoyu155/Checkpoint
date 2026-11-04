"""Local management page. Its derived token cannot authenticate model API calls."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import zipfile


def management_token(config):
    return hmac.new(config["auth_token"].encode(), b"pacer-model-management-v1", hashlib.sha256).hexdigest()


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pacer · 模型与工作流</title>
<style>:root{font:15px/1.6 system-ui;color:#dce6f7;background:#0b1321;color-scheme:dark}*{box-sizing:border-box}body{max-width:1120px;margin:0 auto;padding:36px 24px}header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #25344b;padding-bottom:22px}.eyebrow{font-size:11px;letter-spacing:3px;color:#79ddba}h1{font-size:32px;letter-spacing:-1px;margin:4px 0}p{color:#95a8c2;margin:0}.pill{background:#183c33;color:#8ee6c3;padding:6px 12px;border-radius:40px;font-size:12px}.layout{display:grid;grid-template-columns:1.2fr 1fr;gap:24px;margin-top:24px}section{padding:24px;border:1px solid #26364e;border-radius:14px;background:#101c2e}h2{font-size:18px;margin:0 0 18px}label{display:block;margin:14px 0 5px;font-size:13px;color:#b1c0d8}input,select{width:100%;padding:11px 12px;border:1px solid #354862;background:#0c1728;color:inherit;border-radius:8px}button{background:#76d9b5;color:#09281e;border:0;border-radius:8px;padding:11px 15px;font-weight:700;cursor:pointer}.secondary{background:#24364f;color:#d3e2f5}button:disabled{opacity:.45;cursor:wait}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.note{font-size:12px;color:#91a6c0;margin-top:12px}.status{margin-top:20px;padding:14px;border-left:3px solid #74d9b4;background:#162a31;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}td,th{text-align:left;border-bottom:1px solid #26364c;padding:12px 6px;vertical-align:top}th{color:#86a2bf;font-size:11px}.role{color:#97b0cb}#workflowHint{padding:14px;border:1px solid #2c435d;border-radius:8px;font-size:13px;color:#b6cbe1;margin-top:14px}.full{margin-top:24px}.login{max-width:500px;margin:25px auto}a{color:#7bdcbc}@media(max-width:800px){.layout{grid-template-columns:1fr}header{display:block}.pill{display:inline-block;margin-top:10px}}</style>
<header><div><span class="eyebrow">PACER CONTROL ROOM</span><h1>模型与工作流</h1><p>把网页 Gemini 4 接入开发，明确每一步由谁完成。</p></div><span class="pill">本机管理 · 配置即时生效</span></header>
<div id="login" class="login" hidden><p>请通过桌面的“Pacer 模型管理”或命令 <code>pacer model manage</code> 打开此页。</p></div>
<main id="main" hidden><div class="layout"><section><h2>Gemini 4 网页反代</h2><label>接口基础地址</label><input id="base" placeholder="https://你的网关/gemini4"><label>模型 ID</label><input id="model" value="gemini-4"><label>接口密钥</label><input id="key" type="password" autocomplete="off" placeholder="留空保留现有密钥"><div id="keyState" class="note"></div><label><input id="enabled" type="checkbox" style="width:auto"> 启用网页 Gemini 4</label><div class="actions"><button id="save">保存配置</button><button id="probe" class="secondary">实际生成测试</button><button id="extension" class="secondary">下载入池插件 v2</button></div><p class="note">填写你的 Web 反代，沿用网页订阅会话。生成测试会消耗一次小请求；HTTP 在线状态与模型生成成功分别记录。</p></section>
<section><h2>编码工作流</h2><label>工作模式</label><select id="workflow"><option value="fast">快速实现：Grok → 本地验收 → Codex 修复</option><option value="balanced">按复杂度协作：Gemini 4 规划与审查 / Codex 实现</option><option value="gemini4">Gemini 4 编程：直接交给网页模型 → 本地验收</option></select><div id="workflowHint"></div><p class="note">也可以在 Code 中直接说“使用 Gemini 4 实现……”，对该次任务指定网页模型。入口始终是 Pacer。</p><div id="probeState" class="status">尚未运行生成测试</div></section></div>
<section class="full"><h2>本机模型清单与分工</h2><table><thead><tr><th>模型 / 通道</th><th>观测状态</th><th>工作分工</th></tr></thead><tbody id="rows"></tbody></table><p class="note">已安装、已配置、网关在线都不等于已成功生成。当前状态按各通道实际可验证的范围展示。</p></section><div id="status" class="status">正在读取配置…</div></main>
<script>
const $=id=>document.getElementById(id);const fragment=new URLSearchParams(location.hash.slice(1));let token=fragment.get('token')||sessionStorage.getItem('pacerManagementToken');if(token){sessionStorage.setItem('pacerManagementToken',token);history.replaceState(null,'',location.pathname);}else{$('login').hidden=false;}
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error?.message||d.detail||'请求失败');return d;}
function hint(){const v=$('workflow').value;$('workflowHint').textContent=v==='gemini4'?'本地索引 → 网页 Gemini 4 生成补丁 → pytest / 变异验收 → 失败时 Codex 修复。':v==='balanced'?'小任务交 Grok；架构任务由 Gemini 4 提供计划，Codex 实现，本地测试后由 Gemini 4 审查。':'本地索引 → Grok 快速实现 → 本地测试；失败升级 Codex。不会为每个小任务叠加多轮模型。';}
async function load(){const d=await api('/api/model-config');$('main').hidden=false;const p=d.profiles.providers.gemini4;$('base').value=p.base_url;$('model').value=p.model;$('enabled').checked=p.enabled;$('workflow').value=d.profiles.workflow;$('keyState').textContent=p.key_configured?'密钥已配置，留空即可保留':'请填写接口密钥';$('rows').replaceChildren();for(const m of d.models){const tr=document.createElement('tr');for(const val of[m.name,m.status,m.role]){const td=document.createElement('td');td.textContent=val;tr.append(td);}$('rows').append(tr);}const c=d.profiles.checks?.gemini4;if(c)$('probeState').textContent=(c.ok?'生成测试通过':'生成测试失败')+' · '+c.elapsed_seconds+' 秒\n'+(c.response||c.error||'')+'\n'+c.checked_at;$('status').textContent='配置已载入；更改后点击保存。';hint();}
$('workflow').onchange=hint;$('save').onclick=async()=>{try{await api('/api/model-config',{workflow:$('workflow').value,providers:{gemini4:{base_url:$('base').value.trim(),model:$('model').value.trim(),api_key:$('key').value.trim(),enabled:$('enabled').checked}}});$('key').value='';await load();$('status').textContent='配置已保存，下一次调用生效。';}catch(e){$('status').textContent=e.message;}};
$('probe').onclick=async()=>{$('probe').disabled=true;$('probeState').textContent='正在请求已保存的反代配置…';try{const r=await api('/api/models/probe',{provider:'gemini4'});$('probeState').textContent=(r.ok?'生成成功':'生成失败')+' · '+r.elapsed_seconds+' 秒\n'+(r.response||r.error||'');}catch(e){$('probeState').textContent=e.message;}finally{$('probe').disabled=false;}};
$('extension').onclick=async()=>{try{const r=await fetch('/api/extension',{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw Error('插件下载失败');const u=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=u;a.download='gemini_binder_extension_v2.zip';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}catch(e){$('status').textContent=e.message;}};
if(token)load().catch(e=>{$('login').hidden=false;$('login').textContent=e.message;});
</script></html>'''


def handle(handler, method):
    path = urlsplit(handler.path).path
    if path == "/settings" and method == "GET":
        data = HTML.encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        handler.wfile.write(data)
        return True
    if path not in ("/api/model-config", "/api/models/probe", "/api/extension"):
        return False
    supplied = handler.headers.get("Authorization", "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied, management_token(handler.server.config)):
        handler._error(401, "Open model management through pacer model manage")
        return True
    from .model_catalog import load_profiles, save_profiles, public_profiles, inventory, profiles_path
    try:
        if path == "/api/model-config":
            if method == "POST":
                length = int(handler.headers.get("Content-Length", 0))
                if not 0 < length <= 16384:
                    raise ValueError("Invalid configuration length")
                save_profiles(json.loads(handler.rfile.read(length)))
            elif method != "GET":
                raise ValueError("Unsupported method")
            result = public_profiles()
            from .gemini4_vps_client import gemini4_settings
            result["providers"]["gemini4"]["key_configured"] = bool(gemini4_settings()["api_key"])
            handler._json(200, {"profiles": result, "models": inventory()})
        elif path == "/api/models/probe" and method == "POST":
            length = int(handler.headers.get("Content-Length", 0))
            if not 0 < length <= 1024:
                raise ValueError("Invalid probe request")
            handler.rfile.read(length)
            from .gemini4_vps_client import invoke_gemini4_vps
            from .pacer_model import write_json
            start = time.monotonic()
            response = invoke_gemini4_vps("Reply exactly PACER_GEMINI4_READY.", timeout_seconds=45)
            record = {"ok": response.success and "PACER_GEMINI4_READY" in response.content,
                      "elapsed_seconds": round(time.monotonic()-start, 2), "response": response.content[:150],
                      "error": response.error, "model": response.model, "checked_at": datetime.now(timezone.utc).isoformat()}
            profiles = load_profiles()
            profiles.setdefault("checks", {})["gemini4"] = record
            write_json(profiles_path(), profiles)
            handler._json(200, record)
        elif path == "/api/extension" and method == "GET":
            folder = Path(__file__).resolve().parent / "gemini_binder_extension"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(folder.iterdir()):
                    if file.suffix in (".js", ".json", ".html", ".css"):
                        archive.writestr(file.name, file.read_bytes())
            data = buffer.getvalue()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/zip")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        else:
            raise ValueError("Unsupported management method")
    except (ValueError, OSError) as exc:
        handler._error(400, str(exc))
    return True
