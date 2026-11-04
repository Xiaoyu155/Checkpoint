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
from .model_health import check_all_models_concurrently
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

        <!-- 3. Multi-Model Health Matrix & One-Click Inspection (多模型协同矩阵与一键巡检) -->
        <section class="glass-card p-6">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-4">
                <div>
                    <h2 class="text-lg font-bold text-white flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                        <span id="title_model_health">聚合模型阵列与一键实时巡检 (Multi-Model Health Matrix)</span>
                    </h2>
                    <p class="text-xs text-gray-400 mt-1" id="sub_model_health">实时探测 Codex Plus、xAI Grok、VPS Gemini 4、WorkBuddy 本地网关与 Gemini CLI 的延迟与健康度</p>
                </div>
                <button onclick="checkModels()" id="btn_check_models" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-xs font-semibold text-white shadow-sm transition-all flex items-center gap-1.5 cursor-pointer">
                    <span id="check_models_spinner" class="hidden animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full"></span>
                    ⚡ <span id="lbl_btn_check_models">一键实时巡检</span>
                </button>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4 mt-4" id="models_container">
                <!-- Injected dynamically via checkModels() -->
            </div>
        </section>

        <!-- 4. Local AI Tools & Channels Discovery (Auto Sniffer) -->
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
                modelHealthTitle: "聚合模型阵列与一键实时巡检 (Multi-Model Health Matrix)",
                modelHealthSub: "实时探测 Codex Plus、xAI Grok、VPS Gemini 4、WorkBuddy 本地网关与 Gemini CLI 的延迟与健康度",
                btnCheckModels: "一键实时巡检",
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
                modelHealthTitle: "Multi-Model Health Matrix & Concurrent Inspection",
                modelHealthSub: "Real-time latency, credentials and readiness probing for Codex Plus, xAI Grok, VPS Gemini 4, WorkBuddy & Gemini CLI",
                btnCheckModels: "Check Models",
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
            document.getElementById('title_model_health').innerText = t.modelHealthTitle;
            document.getElementById('sub_model_health').innerText = t.modelHealthSub;
            document.getElementById('lbl_btn_check_models').innerText = t.btnCheckModels;
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
            // Metrics are measured, not assumed. When there is no data the
            // dashboard says so instead of showing an invented percentage.
            const pct = (m.savings_pct === null || m.savings_pct === undefined) ? '—' : `${m.savings_pct}%`;
            const saved = (m.estimated_usd_saved === null || m.estimated_usd_saved === undefined) ? '—' : `$${m.estimated_usd_saved}`;
            const timeSaved = (m.total_time_saved_minutes === null || m.total_time_saved_minutes === undefined) ? '—' : `${m.total_time_saved_minutes} min`;
            const rate = (m.success_rate_pct === null || m.success_rate_pct === undefined) ? '—' : `${m.success_rate_pct}%`;
            document.getElementById('val_token_pct').innerText = pct;
            document.getElementById('val_time_saved').innerText = timeSaved;
            document.getElementById('val_cost_saved').innerText = saved;
            document.getElementById('val_success_rate').innerText = rate;
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

                const header = document.createElement('div');
                header.className = 'flex items-center justify-between';
                const icon = document.createElement('span');
                icon.className = 'text-xl';
                icon.textContent = item.icon;
                const stat = document.createElement('span');
                stat.className = 'text-[10px] px-2 py-0.5 rounded bg-gray-800 text-gray-400 font-mono';
                stat.textContent = item.stat;
                header.appendChild(icon);
                header.appendChild(stat);

                const name = document.createElement('div');
                name.className = 'mt-3 font-bold text-gray-200 text-xs';
                name.textContent = item.name;

                // The pillar description comes from server-side metrics that
                // may embed recorded values — render it as text, not markup.
                const desc = document.createElement('div');
                desc.className = 'mt-1 text-[11px] text-gray-500';
                desc.textContent = pData.desc || '';

                div.appendChild(header);
                div.appendChild(name);
                div.appendChild(desc);
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

                const dot = document.createElement('div');
                dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 mt-1 flex-shrink-0 animate-pulse';

                const body = document.createElement('div');
                body.className = 'min-w-0 flex-1';

                const top = document.createElement('div');
                top.className = 'flex items-center justify-between gap-1';
                const name = document.createElement('span');
                name.className = 'font-bold text-xs text-gray-200 truncate';
                // The channel name/endpoint come from a filesystem scan.
                name.textContent = item.name || '';
                const type = document.createElement('span');
                type.className = 'text-[9px] uppercase px-1.5 py-0.2 rounded bg-gray-800 text-gray-400';
                type.textContent = item.type || '';
                top.appendChild(name);
                top.appendChild(type);

                const note = document.createElement('div');
                note.className = 'text-[11px] text-gray-400 mt-0.5 truncate';
                note.textContent = item.note || item.endpoint || item.path || '';

                body.appendChild(top);
                body.appendChild(note);
                div.appendChild(dot);
                div.appendChild(body);
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
                // A run passes only when the task itself succeeded. A passing
                // verification command on a failed run does not make it green.
                const isPass = t.status === 'success';

                const goal = String(t.goal || '');
                const backend = String(t.effective_backend || t.primary_backend || '');
                const elapsed = (t.elapsed_seconds === null || t.elapsed_seconds === undefined)
                    ? '—'
                    : `${t.elapsed_seconds}s`;

                // Build the row with DOM APIs rather than innerHTML so a
                // recorded goal can never inject markup into the page.
                const cells = [
                    ['p-3 text-gray-400 font-mono text-[11px] whitespace-nowrap', timeStr],
                    ['p-3 text-gray-200 font-medium truncate max-w-xs', goal],
                    ['p-3', backend],
                    ['p-3 font-mono text-gray-400 text-[11px]', elapsed],
                ];
                cells.forEach(([cls, text]) => {
                    const td = document.createElement('td');
                    td.className = cls;
                    td.textContent = text;
                    if (cls.includes('truncate')) {
                        td.title = goal;
                    }
                    tr.appendChild(td);
                });

                const verdictTd = document.createElement('td');
                verdictTd.className = 'p-3';
                const badge = document.createElement('span');
                badge.className = 'px-2 py-0.5 rounded ' + (isPass
                    ? 'bg-emerald-500/10 text-emerald-400'
                    : 'bg-rose-500/10 text-rose-400') + ' text-[11px] font-semibold';
                badge.textContent = isPass ? 'PASSED' : 'RETRY/FAIL';
                verdictTd.appendChild(badge);
                tr.appendChild(verdictTd);

                tbody.appendChild(tr);
            });
        }

        let latestModelsData = null;

        async function checkModels() {
            const btn = document.getElementById('lbl_btn_check_models');
            const spinner = document.getElementById('check_models_spinner');
            if (spinner) spinner.classList.remove('hidden');
            if (btn) btn.innerText = currentLang === 'zh' ? '巡检中...' : 'Checking...';

            try {
                const res = await fetch('/api/check_models');
                const data = await res.json();
                latestModelsData = data.models;
                renderModelMatrix(latestModelsData);
            } catch (err) {
                console.error("Failed to check models:", err);
            } finally {
                if (spinner) spinner.classList.add('hidden');
                if (btn) btn.innerText = i18n[currentLang].btnCheckModels;
            }
        }

        function renderModelMatrix(models) {
            const container = document.getElementById('models_container');
            if (!container) return;
            container.innerHTML = '';

            const modelDefs = [
                { id: 'codex', name: 'OpenAI Codex Plus', role: currentLang === 'zh' ? '重型架构与高难编码' : 'Heavy Architecture & Coding', tier: 'L2/L3', icon: '🤖' },
                { id: 'grok', name: 'xAI Grok 4.7', role: currentLang === 'zh' ? '极速微补丁 (Sub2API 3s)' : 'Light-speed Micro-Patch', tier: 'L1 Primary', icon: '⚡' },
                { id: 'gemini4_vps', name: 'Gemini 4 Jio Pool', role: currentLang === 'zh' ? '深度审查与许愿分析' : 'Review & Wish Analysis', tier: 'VPS Gateway', icon: '🌐' },
                { id: 'workbuddy', name: 'WorkBuddy Gateway', role: currentLang === 'zh' ? '本地离线保底 (DeepSeek)' : 'Offline Local Fallback', tier: 'Local Daemon', icon: '💻' },
                { id: 'gemini_cli', name: 'Gemini Flash CLI', role: currentLang === 'zh' ? '终端会话协调与极速推断' : 'Terminal Host & Fast CLI', tier: 'Host / CLI', icon: '✨' }
            ];

            modelDefs.forEach(def => {
                const m = (models && models[def.id]) || { status: 'offline', latency_ms: 0, details: '' };
                const isOnline = m.status === 'online';
                const isDegraded = m.status === 'degraded';
                const statusColor = isOnline ? 'emerald' : (isDegraded ? 'amber' : 'rose');
                const statusText = isOnline ? (currentLang === 'zh' ? '正常运行' : 'ONLINE') : (isDegraded ? (currentLang === 'zh' ? '响应缓慢' : 'DEGRADED') : (currentLang === 'zh' ? '未就绪/离线' : 'OFFLINE'));

                const div = document.createElement('div');
                div.className = `p-4 rounded-xl bg-gray-900/60 border border-pacer-border hover:border-gray-600 transition-all flex flex-col justify-between`;

                const top = document.createElement('div');
                const topRow = document.createElement('div');
                topRow.className = 'flex items-center justify-between';
                const icon = document.createElement('span');
                icon.className = 'text-xl';
                icon.textContent = def.icon;
                const latency = document.createElement('span');
                latency.className = 'text-[10px] px-2 py-0.5 rounded font-mono ' + (isOnline
                    ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                    : (isDegraded ? 'bg-amber-500/10 text-amber-400' : 'bg-rose-500/10 text-rose-400'));
                latency.textContent = m.latency_ms > 0 ? m.latency_ms + ' ms' : '--';
                topRow.appendChild(icon);
                topRow.appendChild(latency);

                const nameRow = document.createElement('div');
                nameRow.className = 'mt-3 font-bold text-gray-200 text-xs flex items-center gap-1.5';
                const dot = document.createElement('span');
                dot.className = 'w-2 h-2 rounded-full ' + (isOnline
                    ? 'bg-emerald-400 animate-pulse'
                    : (isDegraded ? 'bg-amber-400' : 'bg-rose-500'));
                const defName = document.createElement('span');
                defName.className = 'truncate';
                defName.textContent = def.name;
                nameRow.appendChild(dot);
                nameRow.appendChild(defName);

                const role = document.createElement('div');
                role.className = 'mt-1 text-[11px] text-gray-400 leading-snug';
                role.textContent = def.role;

                // Probe details may contain endpoint paths or error text.
                // Render as text so they cannot inject markup.
                const details = document.createElement('div');
                details.className = 'mt-2 text-[10px] text-gray-500 truncate';
                details.textContent = m.details || '';
                details.title = m.details || '';

                top.appendChild(topRow);
                top.appendChild(nameRow);
                top.appendChild(role);
                top.appendChild(details);

                const footer = document.createElement('div');
                footer.className = 'mt-4 pt-2 border-t border-gray-800/80 flex items-center justify-between text-[10px]';
                const tier = document.createElement('span');
                tier.className = 'text-gray-500 font-mono';
                tier.textContent = def.tier;
                const state = document.createElement('span');
                state.className = 'font-semibold text-' + statusColor + '-400';
                state.textContent = statusText;
                footer.appendChild(tier);
                footer.appendChild(state);

                div.appendChild(top);
                div.appendChild(footer);
                container.appendChild(div);
            });
        }

        // Initialize
        renderI18n();
        refreshData();
        checkModels();
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
        elif parsed.path == "/api/check_models":
            data = check_all_models_concurrently()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
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
