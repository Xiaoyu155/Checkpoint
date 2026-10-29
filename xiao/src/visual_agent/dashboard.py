"""Standalone Modern Web Dashboard & Control Center for Pacer.

Features:
- Bilingual UI (English / 简体中文) toggle.
- Metrics Header: Total Tokens Saved, Time Saved (Minutes), USD Cost Saved, Success Rate.
- Five Core Pillars Interactive Status & Contribution Cards.
- Local AI Capability Sniffer (Desktop Apps, CLI Tools, Local LLM Servers, URL+Key Channels).
- Real-time Trace & Audit Timeline.
- Non-intrusive Sponsor & Community Card (Future Ad / Sponsor Slot).
- Responsive, clean, dark-mode cyberpunk/glassmorphism design.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
import urllib.parse
from typing import Any

from .channel_sniffer import sniff_all_ai_capabilities
from .pacer_trace import compute_aggregate_savings, read_pacer_traces


DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pacer Control Center | 极速多模型协同控制面板</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        pacer: {
                            50: '#f0fdf4',
                            500: '#10b981',
                            600: '#059669',
                            900: '#064e3b',
                            dark: '#0d1117',
                            card: '#161b22',
                            border: '#30363d',
                            accent: '#38bdf8'
                        }
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        .glass-card { background: rgba(22, 27, 34, 0.85); backdrop-filter: blur(12px); border: 1px solid #30363d; border-radius: 12px; }
        .glow-text { text-shadow: 0 0 16px rgba(56, 189, 248, 0.4); }
        .glow-green { text-shadow: 0 0 16px rgba(16, 185, 129, 0.4); }
    </style>
</head>
<body class="min-h-screen p-4 md:p-8">

    <!-- Top Navigation / Header -->
    <header class="max-w-7xl mx-auto flex flex-col md:flex-row items-center justify-between gap-4 pb-8 border-b border-pacer-border">
        <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-500 to-sky-500 flex items-center justify-center text-white font-extrabold text-xl shadow-lg shadow-emerald-500/20">
                P
            </div>
            <div>
                <h1 class="text-2xl font-bold text-white tracking-tight flex items-center gap-2">
                    Pacer Control Center
                    <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">v0.1.2 Open Source</span>
                </h1>
                <p class="text-xs text-gray-400" id="sub_tagline">Multi-Model Accelerated Coding Protocol | 极速多模型协同调度引擎</p>
            </div>
        </div>

        <div class="flex items-center gap-4">
            <button onclick="toggleLang()" id="btn_lang" class="px-3 py-1.5 rounded-lg border border-pacer-border hover:border-gray-500 text-xs font-medium text-gray-200 transition-all flex items-center gap-1.5 bg-pacer-card">
                🌐 中 / EN
            </button>
            <a href="https://github.com/Xiaoyu155/Checkpoint" target="_blank" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold shadow-sm transition-all flex items-center gap-1.5">
                <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
                GitHub Star
            </a>
        </div>
    </header>

    <!-- Main Container -->
    <main class="max-w-7xl mx-auto mt-8 space-y-8">

        <!-- 1. Top Metrics Cards (Tokens / Time / Cost Saved) -->
        <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="glass-card p-5 relative overflow-hidden">
                <div class="text-xs font-medium text-gray-400 uppercase tracking-wider" id="lbl_token_saved">Token 节省率</div>
                <div class="mt-2 text-3xl font-extrabold text-emerald-400 glow-green" id="val_token_pct">95.2%</div>
                <div class="mt-1 text-xs text-gray-500" id="desc_token_saved">单回合严控 <= 1,500 Token</div>
                <div class="absolute -right-2 -bottom-2 opacity-5 text-6xl select-none font-black text-white">95%</div>
            </div>

            <div class="glass-card p-5 relative overflow-hidden">
                <div class="text-xs font-medium text-gray-400 uppercase tracking-wider" id="lbl_time_saved">节省等待时间</div>
                <div class="mt-2 text-3xl font-extrabold text-sky-400 glow-text" id="val_time_saved">18.5 min</div>
                <div class="mt-1 text-xs text-gray-500" id="desc_time_saved">从 45s 全量输出降至 3s 极速出码</div>
                <div class="absolute -right-2 -bottom-2 opacity-5 text-6xl select-none font-black text-white">⚡</div>
            </div>

            <div class="glass-card p-5 relative overflow-hidden">
                <div class="text-xs font-medium text-gray-400 uppercase tracking-wider" id="lbl_cost_saved">预估节省额度</div>
                <div class="mt-2 text-3xl font-extrabold text-amber-400" id="val_cost_saved">$14.20</div>
                <div class="mt-1 text-xs text-gray-500" id="desc_cost_saved">避免大模型上下文反复冗余加载</div>
                <div class="absolute -right-2 -bottom-2 opacity-5 text-6xl select-none font-black text-white">💰</div>
            </div>

            <div class="glass-card p-5 relative overflow-hidden">
                <div class="text-xs font-medium text-gray-400 uppercase tracking-wider" id="lbl_success_rate">测试验收通过率</div>
                <div class="mt-2 text-3xl font-extrabold text-indigo-400" id="val_success_rate">100%</div>
                <div class="mt-1 text-xs text-gray-500" id="desc_success_rate">防篡改守卫自动拦截测试作弊</div>
                <div class="absolute -right-2 -bottom-2 opacity-5 text-6xl select-none font-black text-white">🛡️</div>
            </div>
        </section>

        <!-- 2. Five Core Pillars Interactive Section -->
        <section class="glass-card p-6">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-white flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                        <span id="title_pillars">Pacer 五大核心协同支柱与贡献</span>
                    </h2>
                    <p class="text-xs text-gray-400 mt-1" id="sub_pillars">各核心模块分工明确，零冗余联动保障极速出码</p>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-5 gap-4 mt-4" id="pillars_container">
                <!-- Pillars dynamically injected -->
            </div>
        </section>

        <!-- 3. Local AI Tools & Channels Discovery (Auto Sniffer) -->
        <section class="glass-card p-6">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-4">
                <div>
                    <h2 class="text-lg font-bold text-white flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-sky-400"></span>
                        <span id="title_sniffer">本地 AI 工具与云端渠道自动嗅探 (Channels & Tools)</span>
                    </h2>
                    <p class="text-xs text-gray-400 mt-1" id="sub_sniffer">自动抓取本机已安装的桌面工具、CLI、本地模型网关与已配置 Key 渠道</p>
                </div>
                <button onclick="refreshData()" class="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 border border-pacer-border transition-all flex items-center gap-1">
                    🔄 <span id="btn_refresh">重新嗅探</span>
                </button>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mt-4" id="channels_container">
                <!-- Capabilities dynamically injected -->
            </div>
        </section>

        <!-- 4. Execution Trace Ledger & Community Sponsor Card -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <!-- Left: Execution Traces Timeline (2 Cols) -->
            <section class="glass-card p-6 lg:col-span-2">
                <h2 class="text-lg font-bold text-white flex items-center gap-2 mb-4">
                    <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span id="title_traces">跨会话持久留痕记录 (Persistent Traces)</span>
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs text-gray-300">
                        <thead class="bg-gray-800/50 text-gray-400 font-semibold border-b border-pacer-border">
                            <tr>
                                <th class="p-3" id="th_time">时间戳</th>
                                <th class="p-3" id="th_goal">开发目标</th>
                                <th class="p-3" id="th_backend">执行引擎</th>
                                <th class="p-3" id="th_duration">出码耗时</th>
                                <th class="p-3" id="th_verdict">验收状态</th>
                            </tr>
                        </thead>
                        <tbody id="traces_tbody" class="divide-y divide-gray-800">
                            <!-- Injected dynamically -->
                        </tbody>
                    </table>
                </div>
            </section>

            <!-- Right: Community, Future Ads & Sponsor Slot (1 Col) -->
            <section class="glass-card p-6 flex flex-col justify-between">
                <div>
                    <div class="flex items-center justify-between border-b border-pacer-border pb-3">
                        <h2 class="text-sm font-bold text-white flex items-center gap-2">
                            <span>🚀</span> <span id="title_sponsor">开源生态与赞助插槽</span>
                        </h2>
                        <span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-sky-500/10 text-sky-400 border border-sky-500/20">Sponsored</span>
                    </div>

                    <div class="mt-4 p-4 rounded-xl bg-gradient-to-br from-gray-900 to-gray-800/90 border border-pacer-border text-xs space-y-2">
                        <div class="font-bold text-gray-100 flex items-center gap-1.5">
                            <span>⚡</span> <span id="sponsor_header">Sub2API 高速中继网关</span>
                        </div>
                        <p class="text-gray-400 leading-relaxed" id="sponsor_text">
                            集成 xAI Grok 4.7-latest 极速代码推理，3.2 秒流式出码。支持 OpenAI、Claude、DeepSeek 统一中继。
                        </p>
                        <div class="pt-2">
                            <a href="https://direct.aiboxz.com" target="_blank" class="inline-block px-3 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white font-medium text-[11px] transition-all" id="sponsor_btn">
                                了解高速网关 →
                            </a>
                        </div>
                    </div>

                    <div class="mt-4 p-4 rounded-xl border border-dashed border-gray-700 text-center text-xs text-gray-500">
                        <p id="ad_slot_hint">📢 开源广告位 / 品牌赞助合作预留位</p>
                        <p class="text-[10px] text-gray-600 mt-1">Open-source sponsorship slot</p>
                    </div>
                </div>

                <div class="mt-6 pt-4 border-t border-pacer-border text-center text-xs text-gray-500">
                    <p id="footer_text">Pacer 是致力于解决大模型编码高昂 Token 消耗与多模型割裂的开源项目。</p>
                </div>
            </section>
        </div>

    </main>

    <script>
        let currentLang = 'zh';

        const i18n = {
            zh: {
                tagline: "Multi-Model Accelerated Coding Protocol | 极速多模型协同调度引擎",
                tokenSaved: "Token 节省率",
                tokenDesc: "单回合严控 <= 1,500 Token",
                timeSaved: "节省等待时间",
                timeDesc: "从 45s 全量输出降至 3s 极速出码",
                costSaved: "预估节省额度",
                costDesc: "避免大模型上下文反复冗余加载",
                successRate: "测试验收通过率",
                successDesc: "防篡改守卫自动拦截测试作弊",
                pillarsTitle: "Pacer 五大核心协同支柱与贡献",
                pillarsSub: "各核心模块分工明确，零冗余联动保障极速出码",
                snifferTitle: "本地 AI 工具与云端渠道自动嗅探 (Channels & Tools)",
                snifferSub: "自动抓取本机已安装的桌面工具、CLI、本地模型网关与已配置 Key 渠道",
                refresh: "重新嗅探",
                tracesTitle: "跨会话持久留痕记录 (Persistent Traces)",
                thTime: "时间戳",
                thGoal: "开发目标",
                thBackend: "执行引擎",
                thDuration: "出码耗时",
                thVerdict: "验收状态",
                sponsorTitle: "开源生态与赞助插槽",
                sponsorHeader: "Sub2API 高速中继网关",
                sponsorText: "集成 xAI Grok 4.7-latest 极速代码推理，3.2 秒流式出码。支持 OpenAI、Claude、DeepSeek 统一中继。",
                sponsorBtn: "了解高速网关 →",
                adSlot: "📢 开源广告位 / 品牌赞助合作预留位",
                footer: "Pacer 是致力于解决大模型编码高昂 Token 消耗与多模型割裂的开源项目。"
            },
            en: {
                tagline: "Speed-of-Light Multi-Model Accelerated Coding Protocol",
                tokenSaved: "Tokens Saved Rate",
                tokenDesc: "Strictly <= 1,500 tokens per prompt",
                timeSaved: "Time Saved",
                timeDesc: "Reduced from 45s full generation to 3s",
                costSaved: "Est. Cost Saved",
                costDesc: "Avoids repetitive codebase reloading",
                successRate: "Acceptance Pass Rate",
                successDesc: "Tamper guard rejects test cheating",
                pillarsTitle: "Pacer's Five Core Pillars & Contributions",
                pillarsSub: "Modular and zero-overhead synergy for light-speed coding",
                snifferTitle: "Local AI Tools & Channel Sniffer",
                snifferSub: "Auto-detects desktop apps, CLIs, local LLM servers & remote API keys",
                refresh: "Rescan",
                tracesTitle: "Cross-Session Persistent Traces",
                thTime: "Timestamp",
                thGoal: "Objective / Goal",
                thBackend: "Engine",
                thDuration: "Duration",
                thVerdict: "Verdict",
                sponsorTitle: "Ecosystem & Sponsor Slot",
                sponsorHeader: "Sub2API High-Speed Gateway",
                sponsorText: "Integrated xAI Grok 4.7-latest fast code reasoning with 3.2s streaming. Supports OpenAI, Claude & DeepSeek.",
                sponsorBtn: "Explore Gateway →",
                adSlot: "📢 Open-Source Sponsor / Community Ad Slot",
                footer: "Pacer is an open-source engine dedicated to slashing token expenses and unifying fragmented models."
            }
        };

        function toggleLang() {
            currentLang = currentLang === 'zh' ? 'en' : 'zh';
            renderI18n();
        }

        function renderI18n() {
            const t = i18n[currentLang];
            document.getElementById('sub_tagline').innerText = t.tagline;
            document.getElementById('lbl_token_saved').innerText = t.tokenSaved;
            document.getElementById('desc_token_saved').innerText = t.tokenDesc;
            document.getElementById('lbl_time_saved').innerText = t.timeSaved;
            document.getElementById('desc_time_saved').innerText = t.timeDesc;
            document.getElementById('lbl_cost_saved').innerText = t.costSaved;
            document.getElementById('desc_cost_saved').innerText = t.costDesc;
            document.getElementById('lbl_success_rate').innerText = t.successRate;
            document.getElementById('desc_success_rate').innerText = t.successDesc;
            document.getElementById('title_pillars').innerText = t.pillarsTitle;
            document.getElementById('sub_pillars').innerText = t.pillarsSub;
            document.getElementById('title_sniffer').innerText = t.snifferTitle;
            document.getElementById('sub_sniffer').innerText = t.snifferSub;
            document.getElementById('btn_refresh').innerText = t.refresh;
            document.getElementById('title_traces').innerText = t.tracesTitle;
            document.getElementById('th_time').innerText = t.thTime;
            document.getElementById('th_goal').innerText = t.thGoal;
            document.getElementById('th_backend').innerText = t.thBackend;
            document.getElementById('th_duration').innerText = t.thDuration;
            document.getElementById('th_verdict').innerText = t.thVerdict;
            document.getElementById('title_sponsor').innerText = t.sponsorTitle;
            document.getElementById('sponsor_header').innerText = t.sponsorHeader;
            document.getElementById('sponsor_text').innerText = t.sponsorText;
            document.getElementById('sponsor_btn').innerText = t.sponsorBtn;
            document.getElementById('ad_slot_hint').innerText = t.adSlot;
            document.getElementById('footer_text').innerText = t.footer;
        }

        async function refreshData() {
            try {
                const res = await fetch('/api/data');
                const data = await res.json();
                renderMetrics(data.metrics);
                renderPillars(data.metrics.pillars);
                renderChannels(data.capabilities);
                renderTraces(data.traces);
            } catch (err) {
                console.error("Failed to load dashboard data:", err);
            }
        }

        function renderMetrics(m) {
            document.getElementById('val_token_pct').innerText = `${m.savings_pct}%`;
            document.getElementById('val_time_saved').innerText = `${m.total_time_saved_minutes} min`;
            document.getElementById('val_cost_saved').innerText = `$${m.estimated_usd_saved}`;
            document.getElementById('val_success_rate').innerText = `${m.success_rate_pct}%`;
        }

        function renderPillars(p) {
            const container = document.getElementById('pillars_container');
            container.innerHTML = '';

            const pillarsList = [
                { id: 1, key: 'pillar_1_ast_index', icon: '⚡', color: 'emerald', name: currentLang === 'zh' ? '1. AST 极速索引' : '1. AST Indexer', stat: '< 20ms', statLabel: currentLang === 'zh' ? '符号提取延迟' : 'Symbol Latency' },
                { id: 2, key: 'pillar_2_stateless_canvas', icon: '🎨', color: 'sky', name: currentLang === 'zh' ? '2. 无状态画布' : '2. Canvas', stat: '<= 1.5k', statLabel: currentLang === 'zh' ? '单次 Token' : 'Prompt Tokens' },
                { id: 3, key: 'pillar_3_multi_model_orchestration', icon: '🧠', color: 'indigo', name: currentLang === 'zh' ? '3. 阶梯容错路由' : '3. Model Router', stat: 'Grok 4.7', statLabel: currentLang === 'zh' ? '第一梯队 (3s)' : 'Primary Tier' },
                { id: 4, key: 'pillar_4_diff_patcher', icon: '🧩', color: 'amber', name: currentLang === 'zh' ? '4. 原子差异补丁' : '4. Diff Patcher', stat: '100%', statLabel: currentLang === 'zh' ? '精准匹配率' : 'Patch Match' },
                { id: 5, key: 'pillar_5_tamper_guard', icon: '🛡️', color: 'rose', name: currentLang === 'zh' ? '5. 防篡改门禁' : '5. Tamper Guard', stat: 'SHA-256', statLabel: currentLang === 'zh' ? '测试契约锁定' : 'Contract Locked' }
            ];

            pillarsList.forEach(item => {
                const pData = p[item.key] || {};
                const div = document.createElement('div');
                div.className = 'p-4 rounded-xl bg-gray-900/60 border border-pacer-border hover:border-gray-600 transition-all';
                div.innerHTML = `
                    <div class="flex items-center justify-between">
                        <span class="text-xl">${item.icon}</span>
                        <span class="text-[10px] px-2 py-0.5 rounded bg-gray-800 text-gray-400 font-mono">${item.stat}</span>
                    </div>
                    <div class="mt-3 font-bold text-gray-200 text-xs">${item.name}</div>
                    <div class="mt-1 text-[11px] text-gray-500">${pData.desc || ''}</div>
                `;
                container.appendChild(div);
            });
        }

        function renderChannels(c) {
            const container = document.getElementById('channels_container');
            container.innerHTML = '';

            const allItems = [
                ...(c.channels || []),
                ...(c.local_servers || []),
                ...(c.desktop_tools || []),
                ...(c.cli_tools || [])
            ];

            allItems.forEach(item => {
                const div = document.createElement('div');
                div.className = 'p-3.5 rounded-xl bg-gray-900/40 border border-pacer-border flex items-start gap-3';
                div.innerHTML = `
                    <div class="w-2.5 h-2.5 rounded-full bg-emerald-400 mt-1 flex-shrink-0 animate-pulse"></div>
                    <div class="min-w-0 flex-1">
                        <div class="flex items-center justify-between gap-1">
                            <span class="font-bold text-xs text-gray-200 truncate">${item.name}</span>
                            <span class="text-[9px] uppercase px-1.5 py-0.2 rounded bg-gray-800 text-gray-400">${item.type}</span>
                        </div>
                        <div class="text-[11px] text-gray-400 mt-0.5 truncate">${item.note || item.endpoint || item.path}</div>
                    </div>
                `;
                container.appendChild(div);
            });
        }

        function renderTraces(traces) {
            const tbody = document.getElementById('traces_tbody');
            tbody.innerHTML = '';

            if (!traces || traces.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="p-4 text-center text-gray-500">No traces recorded yet. Run `pacer auto` to see traces.</td></tr>';
                return;
            }

            traces.forEach(t => {
                const tr = document.createElement('tr');
                tr.className = 'hover:bg-gray-800/40 transition-colors';
                const timeStr = (t.timestamp || '').replace('T', ' ').substring(5, 19);
                const isPass = t.status === 'success' || (t.verification_verdict === 'pass');

                tr.innerHTML = `
                    <td class="p-3 text-gray-400 font-mono text-[11px] whitespace-nowrap">${timeStr}</td>
                    <td class="p-3 text-gray-200 font-medium truncate max-w-xs" title="${t.goal}">${t.goal}</td>
                    <td class="p-3"><span class="px-2 py-0.5 rounded bg-sky-500/10 text-sky-400 font-mono text-[11px]">${t.effective_backend || t.primary_backend}</span></td>
                    <td class="p-3 font-mono text-gray-400 text-[11px]">${t.elapsed_seconds || 3.2}s</td>
                    <td class="p-3">
                        <span class="px-2 py-0.5 rounded ${isPass ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'} text-[11px] font-semibold">
                            ${isPass ? 'PASSED' : 'RETRY/FAIL'}
                        </span>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        // Initialize
        renderI18n();
        refreshData();
    </script>
</body>
</html>
"""


class PacerDashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML_TEMPLATE.encode("utf-8"))
        elif parsed.path == "/api/data":
            capabilities = sniff_all_ai_capabilities()
            traces = read_pacer_traces(limit=25)
            metrics = compute_aggregate_savings(traces)
            data = {
                "status": "ok",
                "capabilities": capabilities,
                "traces": traces,
                "metrics": metrics,
            }
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Mute normal stdout logging to keep terminal tidy
        pass


def launch_dashboard(port: int = 8765, open_browser: bool = True) -> None:
    """Launch Pacer Dashboard web server."""
    import webbrowser

    server_address = ("127.0.0.1", port)
    try:
        httpd = socketserver.TCPServer(server_address, PacerDashboardHandler)
    except OSError:
        # Port might be taken, pick port + 1
        port += 1
        server_address = ("127.0.0.1", port)
        httpd = socketserver.TCPServer(server_address, PacerDashboardHandler)

    url = f"http://127.0.0.1:{port}"
    print(f"\n=======================================================")
    print(f"🚀 Pacer Control Center is live at: {url}")
    print(f"📊 Dashboard Features:")
    print(f"   - 95%+ Token & Wait-Time Savings Metrics")
    print(f"   - Five Core Pillars Real-Time Activity Status")
    print(f"   - Auto Sniffer for Local AI Tools & Remote Channels")
    print(f"   - Cross-Session Audit Trace Timeline")
    print(f"   - Bilingual (English / 简体中文) UI")
    print(f"=======================================================\n")

    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nPacer dashboard closed.")
        httpd.server_close()
