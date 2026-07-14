const PAGE_SIZE = 50;

const state = {
  token: "",
  summary: null,
  plans: [],
  tenants: [],
  keys: [],
  upstreams: [],
  prices: [],
  requests: [],
  requestTotal: 0,
  requestOffset: 0,
  ledger: [],
  ledgerTotal: 0,
  ledgerOffset: 0,
  subscriptions: [],
  timer: null,
  refreshing: false,
  refreshPending: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

document.addEventListener("DOMContentLoaded", () => {
  bindTabs();
  bindModals();
  bindForms();
  bindActions();
  openModal("authModal");
});

function bindTabs() {
  $$(".tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      $$(".tabs button").forEach((item) => item.classList.toggle("active", item === button));
      $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${button.dataset.tab}`));
    });
  });
}

function bindModals() {
  $("#authButton").addEventListener("click", () => openModal("authModal"));
  $$('[data-close]').forEach((button) => button.addEventListener("click", () => closeModal(button.dataset.close)));
  $$(".modal").forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal(modal.id);
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const active = $$(".modal").find((modal) => !modal.hidden);
    if (active) closeModal(active.id);
  });
  $("#connectButton").addEventListener("click", async () => {
    const token = $("#adminToken").value.trim();
    if (!token) return showToast("请输入管理员凭证", true);
    clearTimeout(state.timer);
    state.token = token;
    $("#adminToken").value = "";
    clearDashboardData();
    closeModal("authModal");
    await refreshAll();
  });
  $("#clearTokenButton").addEventListener("click", () => {
    state.token = "";
    clearTimeout(state.timer);
    $("#adminToken").value = "";
    clearDashboardData();
    setConnection("未连接", "");
  });
  $("#copySecretButton").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#secretValue").textContent || "");
      showToast("API Key 已复制");
    } catch (_) {
      showToast("复制失败，请手动选择 Key", true);
    }
  });
  $("#refreshButton").addEventListener("click", () => refreshAll());
}

function bindForms() {
  $("#tenantForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = formObject(event.currentTarget);
    await mutate("/api/gateway/admin/tenants", {
      name: data.name,
      plan_id: data.plan_id,
      initial_credit_microusd: toMicroUsd(data.initial_credit),
    }, "客户已创建");
    event.currentTarget.reset();
  });

  $("#planForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = formObject(event.currentTarget);
    await mutate("/api/gateway/admin/plans", {
      name: data.name,
      monthly_fee_microusd: toMicroUsd(data.monthly_fee),
      included_credit_microusd: toMicroUsd(data.included_credit),
      rpm: number(data.rpm, 60),
      concurrency: number(data.concurrency, 2),
    }, "套餐已创建");
    event.currentTarget.reset();
  });

  $("#upstreamForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = formObject(event.currentTarget);
    await mutate("/api/gateway/admin/upstreams", {
      name: data.name,
      base_url: data.base_url,
      secret_env: data.secret_env,
      models: splitList(data.models),
      routing_contract: data.routing_contract,
      priority: number(data.priority, 100),
      weight: 1,
      max_concurrency: number(data.max_concurrency, 20),
      timeout_seconds: 120,
    }, "上游线路已接入");
    event.currentTarget.reset();
  });

  $("#priceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = formObject(event.currentTarget);
    await mutate("/api/gateway/admin/prices", {
      model: data.model,
      upstream_model: data.upstream_model || data.model,
      input_price_microusd_per_million: toMicroUsd(data.input_price),
      cached_input_price_microusd_per_million: toMicroUsd(data.cached_price),
      output_price_microusd_per_million: toMicroUsd(data.output_price),
      upstream_input_cost_microusd_per_million: toMicroUsd(data.upstream_input_cost),
      upstream_output_cost_microusd_per_million: toMicroUsd(data.upstream_output_cost),
      max_output_tokens: number(data.max_output_tokens, 4096),
      enabled: data.enabled === "true",
    }, "模型价格已保存");
  });

  $("#keyForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const data = formObject(form);
    const allowedModels = $$("#keyModelOptions input[name='allowed_model']:checked").map((item) => item.value);
    if (!allowedModels.length) return showToast("至少选择一个允许模型", true);
    const body = {
      tenant_id: data.tenant_id,
      name: data.name,
      allowed_models: allowedModels,
    };
    if (data.expiry_days === "never") {
      body.never_expires = true;
    } else {
      body.expires_at = Math.floor(Date.now() / 1000) + number(data.expiry_days, 90) * 86400;
    }
    submit.disabled = true;
    try {
      const result = await api("/api/gateway/admin/api-keys", { method: "POST", body });
      closeModal("keyModal");
      $("#secretValue").textContent = result.api_key.token;
      openModal("secretModal");
      showToast("客户 Key 已签发");
      await refreshAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  $("#billingForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const data = formObject(form);
    const dollars = Number(data.amount);
    const reference = String(data.reference || "").trim();
    if (!Number.isFinite(dollars) || dollars < 0 || (data.action === "credit" && dollars <= 0)) {
      return showToast("请输入有效金额", true);
    }
    if (!reference) return showToast("外部交易号不能为空", true);
    submit.disabled = true;
    try {
      if (data.action === "credit") {
        await api(`/api/gateway/admin/tenants/${encodeURIComponent(data.tenant_id)}/balance`, {
          method: "POST",
          headers: { "Idempotency-Key": reference },
          body: { amount_microusd: toMicroUsd(dollars), note: `Manual recharge ${reference}` },
        });
        showToast("余额已入账");
      } else {
        await api(`/api/gateway/admin/tenants/${encodeURIComponent(data.tenant_id)}/subscription`, {
          method: "POST",
          headers: { "Idempotency-Key": reference },
          body: { amount_paid_microusd: toMicroUsd(dollars), period_days: 30 },
        });
        showToast("套餐续费与包含额度已入账");
      }
      closeModal("billingModal");
      form.reset();
      await refreshAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });
}

function bindActions() {
  $("#tenantRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const tenantId = button.dataset.tenant;
    button.disabled = true;
    try {
      if (button.dataset.action === "issue-key") {
        openKeyModal(tenantId);
      }
      if (button.dataset.action === "revoke-key") {
        if (!window.confirm(`撤销 ${button.dataset.prefix}？`)) return;
        await api(`/api/gateway/admin/api-keys/${encodeURIComponent(button.dataset.key)}/revoke`, { method: "POST", body: {} });
        showToast("客户 Key 已撤销");
        await refreshAll();
      }
      if (button.dataset.action === "credit") {
        openBillingModal(tenantId, "credit");
      }
      if (button.dataset.action === "renew-plan") {
        openBillingModal(tenantId, "renew");
      }
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#upstreamRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    button.disabled = true;
    try {
      if (button.dataset.action === "toggle-upstream") {
        await api(`/api/gateway/admin/upstreams/${encodeURIComponent(button.dataset.upstream)}/enabled`, {
          method: "POST",
          body: { enabled: button.dataset.enabled !== "true" },
        });
        showToast("线路状态已更新");
      } else if (button.dataset.action === "test-upstream") {
        const result = await api(`/api/gateway/admin/upstreams/${encodeURIComponent(button.dataset.upstream)}/test`, { method: "POST", body: {} });
        showToast(result.ok ? `线路可用 · ${Number(result.latency_ms).toFixed(0)} ms` : `线路失败 · HTTP ${result.status}`, !result.ok);
      }
      await refreshAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#requestRows").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action='reconcile']");
    if (!button) return;
    const action = button.dataset.reconcile;
    if (!window.confirm(`${action === "capture" ? "确认扣除预留" : "确认释放预留"}？`)) return;
    button.disabled = true;
    try {
      await api(`/api/gateway/admin/requests/${encodeURIComponent(button.dataset.request)}/reconcile`, {
        method: "POST",
        body: { action },
      });
      showToast("待定请求已对账");
      await refreshAll();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#priceRows").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action='edit-price']");
    if (!button) return;
    const item = state.prices.find((price) => price.model === button.dataset.model);
    if (!item) return;
    const form = $("#priceForm");
    form.elements.model.value = item.model;
    form.elements.upstream_model.value = item.upstream_model;
    form.elements.input_price.value = fromMicroUsd(item.input_price_microusd_per_million);
    form.elements.cached_price.value = fromMicroUsd(item.cached_input_price_microusd_per_million);
    form.elements.output_price.value = fromMicroUsd(item.output_price_microusd_per_million);
    form.elements.upstream_input_cost.value = fromMicroUsd(item.upstream_input_cost_microusd_per_million);
    form.elements.upstream_output_cost.value = fromMicroUsd(item.upstream_output_cost_microusd_per_million);
    form.elements.max_output_tokens.value = item.max_output_tokens;
    form.elements.enabled.value = String(Boolean(item.enabled));
    form.elements.model.focus();
  });

  $("#requestPrev").addEventListener("click", () => changePage("request", -1));
  $("#requestNext").addEventListener("click", () => changePage("request", 1));
  $("#ledgerPrev").addEventListener("click", () => changePage("ledger", -1));
  $("#ledgerNext").addEventListener("click", () => changePage("ledger", 1));
}

async function refreshAll({ silent = false } = {}) {
  if (!state.token) {
    openModal("authModal");
    return;
  }
  if (state.refreshing) {
    state.refreshPending = true;
    return;
  }
  clearTimeout(state.timer);
  state.refreshing = true;
  const tokenAtStart = state.token;
  setConnection("同步中", "");
  renderPagination("request", state.requestOffset, state.requests.length, state.requestTotal, true);
  renderPagination("ledger", state.ledgerOffset, state.ledger.length, state.ledgerTotal, true);
  try {
    const loaders = [
      { load: () => api("/api/gateway/admin/summary"), apply: (value) => { state.summary = value; } },
      { load: () => api("/api/gateway/admin/plans"), apply: (value) => { state.plans = value.plans || []; } },
      { load: () => api("/api/gateway/admin/tenants"), apply: (value) => { state.tenants = value.tenants || []; } },
      { load: () => api("/api/gateway/admin/api-keys"), apply: (value) => { state.keys = value.api_keys || []; } },
      { load: () => api("/api/gateway/admin/upstreams"), apply: (value) => { state.upstreams = value.upstreams || []; } },
      { load: () => api("/api/gateway/admin/prices"), apply: (value) => { state.prices = value.prices || []; } },
      { load: () => api(pageUrl("requests", state.requestOffset)), apply: applyRequests },
      { load: () => api(pageUrl("ledger", state.ledgerOffset)), apply: applyLedger },
      { load: () => api("/api/gateway/admin/subscriptions?limit=200"), apply: (value) => { state.subscriptions = value.items || []; } },
    ];
    const results = await Promise.allSettled(loaders.map((item) => item.load()));
    if (state.token !== tokenAtStart) return;
    const failures = [];
    results.forEach((result, index) => {
      if (result.status === "fulfilled") loaders[index].apply(result.value);
      else failures.push(result.reason);
    });
    renderAll();
    if (!failures.length) {
      setConnection("已连接", "online");
    } else {
      const authError = failures.find((error) => error.status === 401 || error.status === 503);
      setConnection(authError?.status === 401 ? "凭证无效" : "部分数据未同步", "error");
      if (!silent) showToast(failures[0].message || "部分数据同步失败", true);
      if (authError) openModal("authModal");
    }
  } finally {
    state.refreshing = false;
    if (state.refreshPending) {
      state.refreshPending = false;
      if (state.token) void refreshAll();
    } else if (state.token) {
      scheduleRefresh();
    }
  }
}

function renderAll() {
  renderSummary();
  renderPlans();
  renderTenants();
  renderUpstreams();
  renderPrices();
  renderRequests();
  renderLedger();
  renderSubscriptions();
}

function renderSummary() {
  const summary = state.summary?.summary || {};
  const setup = state.summary?.setup || { ready: false, checks: {} };
  $("#metricRequests").textContent = compact(summary.requests || 0);
  $("#metricFailures").textContent = `${compact(summary.failed_requests || 0)} 失败 · ${compact(summary.indeterminate_requests || 0)} 待对账`;
  $("#metricRevenue").textContent = usd(summary.revenue_microusd || 0, 6);
  $("#metricCash").textContent = usd(summary.confirmed_cash_microusd || 0);
  $("#metricCost").textContent = usd(summary.upstream_cost_microusd || 0, 6);
  $("#metricMargin").textContent = usd(summary.gross_margin_microusd || 0, 6);
  const marginRate = summary.revenue_microusd ? (summary.gross_margin_microusd / summary.revenue_microusd) * 100 : 0;
  $("#metricMarginRate").textContent = `${marginRate.toFixed(1)}% 毛利率`;
  $("#metricTokens").textContent = compact(summary.tokens || 0);
  $("#metricActive").textContent = `${compact(summary.active_requests || 0)} 个在途请求`;
  $("#readyState").textContent = setup.ready ? "可接受客户请求" : "仍有配置缺口";
  const labels = { tenant: "客户", price: "定价", upstream: "可用上游", customer_key: "客户 Key" };
  $("#setupChecks").innerHTML = Object.entries(labels).map(([key, label]) =>
    `<span class="check ${setup.checks?.[key] ? "ok" : ""}">${escapeHtml(label)}</span>`
  ).join("");
}

function renderPlans() {
  $("#tenantPlan").innerHTML = state.plans.filter((item) => item.enabled).map((item) =>
    `<option value="${escapeAttr(item.id)}">${escapeHtml(item.name)} · ${item.rpm} RPM / ${item.concurrency} 并发</option>`
  ).join("");
}

function renderTenants() {
  $("#tenantCount").textContent = `${state.tenants.length} 个`;
  $("#tenantRows").innerHTML = state.tenants.length ? state.tenants.map((item) => {
    const keys = state.keys.filter((key) => key.tenant_id === item.id);
    const plan = state.plans.find((entry) => entry.id === item.plan_id);
    const keyMarkup = keys.length ? keys.map((key) => {
      const display = keyDisplay(key);
      const scope = (key.allowed_models || []).length ? key.allowed_models.join(", ") : "全部模型";
      return `<span class="key-line"><span class="stack"><b class="mono">${escapeHtml(key.key_prefix)}</b><small><span class="status ${display.className}">${escapeHtml(display.label)}</span> · ${key.expires_at ? dateTime(key.expires_at) : "无期限"}</small><small>权限：${escapeHtml(scope)}</small><small>最近使用：${key.last_used_at ? dateTime(key.last_used_at) : "从未"}</small></span>${key.status === "active" ? `<button class="button small danger" data-action="revoke-key" data-tenant="${escapeAttr(item.id)}" data-key="${escapeAttr(key.id)}" data-prefix="${escapeAttr(key.key_prefix)}">撤销</button>` : ""}</span>`;
    }).join("") : "-";
    return `<tr>
      <td data-label="客户"><span class="stack"><b>${escapeHtml(item.name)}</b><small class="mono">${escapeHtml(item.id)}</small></span></td>
      <td data-label="套餐"><span class="stack"><b>${escapeHtml(item.plan_name || "-")}</b><small>${usd(plan?.monthly_fee_microusd || 0)} / 月 · 含 ${usd(plan?.included_credit_microusd || 0)}</small><small>${item.plan_rpm || 0} RPM / ${item.plan_concurrency || 0} 并发 · ${item.subscription_expires_at ? `至 ${dateTime(item.subscription_expires_at)}` : "未续费"}</small></span></td>
      <td data-label="余额"><b>${usd(item.balance_microusd || 0, 4)}</b></td>
      <td data-label="Key"><div class="key-list">${keyMarkup}</div></td>
      <td data-label="状态"><span class="status ${item.status === "active" ? "good" : "bad"}">${escapeHtml(item.status)}</span></td>
      <td data-label="操作"><span class="actions"><button class="button small secondary" data-action="credit" data-tenant="${escapeAttr(item.id)}">充值</button><button class="button small secondary" data-action="renew-plan" data-tenant="${escapeAttr(item.id)}">续费</button><button class="button small primary" data-action="issue-key" data-tenant="${escapeAttr(item.id)}">发 Key</button></span></td>
    </tr>`;
  }).join("") : emptyRow(6, "暂无客户");
}

function renderUpstreams() {
  $("#upstreamCount").textContent = `${state.upstreams.length} 条`;
  $("#upstreamRows").innerHTML = state.upstreams.length ? state.upstreams.map((item) => {
    const circuit = Number(item.circuit_open_until || 0) * 1000 > Date.now();
    return `<tr>
      <td data-label="线路"><span class="stack"><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.base_url)}</small></span></td>
      <td data-label="Provider">${escapeHtml(item.provider)}</td>
      <td data-label="模型">${escapeHtml((item.models || []).join(", "))}</td>
      <td data-label="路由合同">${escapeHtml(item.routing_contract || "单线路")}</td>
      <td data-label="并发">${item.active_requests || 0} / ${item.max_concurrency}</td>
      <td data-label="延迟">${Number(item.last_latency_ms || 0).toFixed(0)} ms</td>
      <td data-label="熔断"><span class="status ${circuit ? "bad" : "good"}">${circuit ? "OPEN" : "CLOSED"}</span></td>
      <td data-label="密钥"><span class="stack"><span class="status ${item.secret_configured ? "good" : "warn"}">${item.secret_configured ? "已配置" : "缺失"}</span><small class="mono">${escapeHtml(item.secret_env)}</small></span></td>
      <td data-label="操作"><span class="actions"><button class="button small secondary" data-action="test-upstream" data-upstream="${escapeAttr(item.id)}">测试</button><button class="button small ${item.enabled ? "secondary" : "primary"}" data-action="toggle-upstream" data-upstream="${escapeAttr(item.id)}" data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button></span></td>
    </tr>`;
  }).join("") : emptyRow(9, "暂无上游线路");
}

function renderPrices() {
  $("#priceCount").textContent = `${state.prices.length} 个`;
  $("#priceRows").innerHTML = state.prices.length ? state.prices.map((item) =>
    `<tr>
      <td data-label="公开模型"><span class="stack"><b>${escapeHtml(item.model)}</b><small>${item.enabled ? "在售" : "停用"}</small></span></td>
      <td data-label="上游模型">${escapeHtml(item.upstream_model)}</td>
      <td data-label="输入">${usdPerMillion(item.input_price_microusd_per_million)}</td>
      <td data-label="缓存输入">${usdPerMillion(item.cached_input_price_microusd_per_million)}</td>
      <td data-label="输出">${usdPerMillion(item.output_price_microusd_per_million)}</td>
      <td data-label="上游成本">${usdPerMillion(item.upstream_input_cost_microusd_per_million)} / ${usdPerMillion(item.upstream_output_cost_microusd_per_million)}</td>
      <td data-label="版本"><span class="actions">v${item.version}<button class="button small secondary" data-action="edit-price" data-model="${escapeAttr(item.model)}">编辑</button></span></td>
    </tr>`
  ).join("") : emptyRow(7, "暂无模型价格");
}

function renderRequests() {
  $("#requestCount").textContent = `${state.requests.length} / ${state.requestTotal} 条`;
  $("#requestRows").innerHTML = state.requests.length ? state.requests.map((item) => {
    const statusClass = item.status === "settled" ? "good" : item.status === "failed" ? "bad" : "warn";
    const reconcile = item.status === "indeterminate" ? `<span class="actions"><button class="button small primary" data-action="reconcile" data-reconcile="capture" data-request="${escapeAttr(item.id)}">扣除</button><button class="button small danger" data-action="reconcile" data-reconcile="release" data-request="${escapeAttr(item.id)}">释放</button></span>` : "";
    return `<tr>
      <td data-label="时间 / ID"><span class="stack"><span>${dateTime(item.created_at)}</span><small class="mono">${escapeHtml(item.id)}</small></span></td>
      <td data-label="客户" class="mono">${escapeHtml(item.tenant_id)}</td>
      <td data-label="模型">${escapeHtml(item.model)}</td>
      <td data-label="上游" class="mono">${escapeHtml(item.upstream_id)}</td>
      <td data-label="状态"><span class="status ${statusClass}">${escapeHtml(item.status)}</span>${item.error_code ? `<br><small>${escapeHtml(item.error_code)}</small>` : ""}${reconcile}</td>
      <td data-label="Tokens">${compact((item.input_tokens || 0) + (item.output_tokens || 0))}<br><small>${compact(item.cached_input_tokens || 0)} cached</small></td>
      <td data-label="计费额">${usd(item.actual_microusd || 0, 6)}</td>
      <td data-label="成本">${usd(item.upstream_cost_microusd || 0, 6)}</td>
      <td data-label="延迟">${Number(item.latency_ms || 0).toFixed(0)} ms</td>
      <td data-label="Usage">${escapeHtml(item.usage_source || "-")}<br><small>${item.streaming ? "stream" : "json"}</small></td>
    </tr>`;
  }).join("") : emptyRow(10, "暂无请求记录");
  renderPagination("request", state.requestOffset, state.requests.length, state.requestTotal);
}

function renderLedger() {
  $("#ledgerCount").textContent = `${state.ledger.length} / ${state.ledgerTotal} 条`;
  $("#ledgerRows").innerHTML = state.ledger.length ? state.ledger.map((item) =>
    `<tr>
      <td data-label="时间">${dateTime(item.created_at)}</td>
      <td data-label="客户" class="mono">${escapeHtml(item.tenant_id)}</td>
      <td data-label="类型">${escapeHtml(item.kind)}</td>
      <td data-label="变动" class="money ${item.amount_microusd >= 0 ? "positive" : "negative"}">${item.amount_microusd >= 0 ? "+" : ""}${usd(item.amount_microusd, 6)}</td>
      <td data-label="余额">${usd(item.balance_after_microusd, 6)}</td>
      <td data-label="Request ID" class="mono">${escapeHtml(item.request_id || "-")}</td>
      <td data-label="备注">${escapeHtml(item.note || "-")}</td>
    </tr>`
  ).join("") : emptyRow(7, "暂无账本记录");
  renderPagination("ledger", state.ledgerOffset, state.ledger.length, state.ledgerTotal);
}

function renderSubscriptions() {
  $("#subscriptionCount").textContent = `${state.subscriptions.length} 条`;
  $("#subscriptionRows").innerHTML = state.subscriptions.length ? state.subscriptions.map((item) => {
    const tenant = state.tenants.find((entry) => entry.id === item.tenant_id);
    const plan = state.plans.find((entry) => entry.id === item.plan_id);
    return `<tr>
      <td data-label="收款时间">${dateTime(item.created_at)}</td>
      <td data-label="客户"><span class="stack"><b>${escapeHtml(tenant?.name || item.tenant_id)}</b><small class="mono">${escapeHtml(item.tenant_id)}</small></span></td>
      <td data-label="套餐">${escapeHtml(plan?.name || item.plan_id)}</td>
      <td data-label="实收"><b>${usd(item.amount_paid_microusd || 0, 2)}</b></td>
      <td data-label="发放额度">${usd(item.credit_granted_microusd || 0, 2)}</td>
      <td data-label="服务期间"><span class="stack"><span>${dateTime(item.period_start)}</span><small>至 ${dateTime(item.period_end)}</small></span></td>
      <td data-label="外部交易号" class="mono">${escapeHtml(item.external_reference || "-")}</td>
    </tr>`;
  }).join("") : emptyRow(7, "暂无订阅收款记录");
}

function openKeyModal(tenantId) {
  const tenant = state.tenants.find((item) => item.id === tenantId);
  const models = state.summary?.setup?.serviceable_models || [];
  if (!tenant) return showToast("客户不存在", true);
  if (!models.length) return showToast("请先配置可路由且已定价的模型", true);
  const form = $("#keyForm");
  form.reset();
  form.elements.tenant_id.value = tenantId;
  $("#keyTenant").textContent = `${tenant.name} · ${tenantId}`;
  $("#keyModelOptions").innerHTML = models.map((model) =>
    `<label class="check-option"><input type="checkbox" name="allowed_model" value="${escapeAttr(model)}" checked><span>${escapeHtml(model)}</span></label>`
  ).join("");
  openModal("keyModal");
}

function openBillingModal(tenantId, action) {
  const tenant = state.tenants.find((item) => item.id === tenantId);
  const plan = state.plans.find((item) => item.id === tenant?.plan_id);
  if (!tenant) return showToast("客户不存在", true);
  if (action === "renew" && !plan) return showToast("客户套餐不可用", true);
  const form = $("#billingForm");
  form.reset();
  form.elements.tenant_id.value = tenantId;
  form.elements.action.value = action;
  form.elements.reference.value = "";
  const amount = form.elements.amount;
  if (action === "renew") {
    $("#billingTitle").textContent = "确认套餐续费";
    $("#billingContext").textContent = `${tenant.name} · ${plan.name} · 月费 ${usd(plan.monthly_fee_microusd || 0)} · 发放额度 ${usd(plan.included_credit_microusd || 0)}`;
    $("#billingSubmit").textContent = "确认续费";
    amount.value = fromMicroUsd(plan.monthly_fee_microusd || 0);
    amount.min = "0";
    amount.readOnly = true;
  } else {
    $("#billingTitle").textContent = "客户余额充值";
    $("#billingContext").textContent = `${tenant.name} · 当前余额 ${usd(tenant.balance_microusd || 0, 4)}`;
    $("#billingSubmit").textContent = "确认入账";
    amount.value = "10";
    amount.min = "0.01";
    amount.readOnly = false;
  }
  openModal("billingModal");
}

async function changePage(kind, direction) {
  if (state.refreshing) return;
  clearTimeout(state.timer);
  const offsetKey = `${kind}Offset`;
  const totalKey = `${kind}Total`;
  const nextOffset = Math.max(0, state[offsetKey] + direction * PAGE_SIZE);
  if (nextOffset === state[offsetKey] || (direction > 0 && nextOffset >= state[totalKey])) return;
  state[offsetKey] = nextOffset;
  renderPagination(kind, state[offsetKey], state[kind === "request" ? "requests" : "ledger"].length, state[totalKey], true);
  try {
    const payload = await api(pageUrl(kind === "request" ? "requests" : "ledger", nextOffset));
    if (kind === "request") {
      applyRequests(payload);
      renderRequests();
    } else {
      applyLedger(payload);
      renderLedger();
    }
  } catch (error) {
    state[offsetKey] = Math.max(0, nextOffset - direction * PAGE_SIZE);
    showToast(error.message, true);
    if (kind === "request") renderRequests();
    else renderLedger();
  } finally {
    if (state.token) scheduleRefresh();
  }
}

function pageUrl(resource, offset) {
  return `/api/gateway/admin/${resource}?limit=${PAGE_SIZE}&offset=${Math.max(0, offset)}`;
}

function applyRequests(payload) {
  state.requests = payload.items || [];
  state.requestTotal = payload.total || 0;
}

function applyLedger(payload) {
  state.ledger = payload.items || [];
  state.ledgerTotal = payload.total || 0;
}

function renderPagination(kind, offset, count, total, loading = false) {
  const start = count ? offset + 1 : 0;
  const end = count ? offset + count : 0;
  $(`#${kind}Page`).textContent = `${start}-${end} / ${total}`;
  $(`#${kind}Prev`).disabled = loading || offset <= 0;
  $(`#${kind}Next`).disabled = loading || offset + count >= total;
}

function keyDisplay(key) {
  if (key.status !== "active") return { label: "已撤销", className: "bad" };
  if (key.expires_at && Number(key.expires_at) <= Date.now() / 1000) {
    return { label: "已过期", className: "warn" };
  }
  return { label: "有效", className: "good" };
}

function clearDashboardData() {
  state.summary = null;
  state.plans = [];
  state.tenants = [];
  state.keys = [];
  state.upstreams = [];
  state.prices = [];
  state.requests = [];
  state.requestTotal = 0;
  state.requestOffset = 0;
  state.ledger = [];
  state.ledgerTotal = 0;
  state.ledgerOffset = 0;
  state.subscriptions = [];
  state.refreshPending = false;
  renderAll();
}

async function mutate(path, body, message) {
  try {
    await api(path, { method: "POST", body });
    showToast(message);
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
    throw error;
  }
}

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${state.token}`, ...(options.headers || {}) };
  const init = { method: options.method || "GET", headers };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || payload?.detail || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = payload?.error?.code || "http_error";
    throw error;
  }
  return payload;
}

function scheduleRefresh() {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => refreshAll({ silent: true }), 10000);
}

function setConnection(text, className) {
  $("#connectionText").textContent = text;
  const node = $("#connectionText").parentElement;
  node.classList.remove("online", "error");
  if (className) node.classList.add(className);
}

function openModal(id) {
  const modal = $("#" + id);
  modal.hidden = false;
  requestAnimationFrame(() => modal.querySelector('input:not([type="hidden"]), select, button')?.focus());
}
function closeModal(id) {
  $("#" + id).hidden = true;
  if (id === "secretModal") $("#secretValue").textContent = "";
}

let toastTimer = null;
function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
}

function formObject(form) { return Object.fromEntries(new FormData(form).entries()); }
function splitList(value) { return String(value || "").split(",").map((item) => item.trim()).filter(Boolean); }
function number(value, fallback = 0) { const result = Number(value); return Number.isFinite(result) ? result : fallback; }
function toMicroUsd(value) { return Math.round(number(value) * 1_000_000); }
function fromMicroUsd(value) { return number(value) / 1_000_000; }
function usdPerMillion(value) { return `$${fromMicroUsd(value).toFixed(4)}/1M`; }
function usd(value, digits = 2) { return `${number(value) < 0 ? "-" : ""}$${Math.abs(fromMicroUsd(value)).toFixed(digits)}`; }
function compact(value) { return Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(number(value)); }
function dateTime(value) { return value ? new Date(number(value) * 1000).toLocaleString("zh-CN", { hour12: false }) : "-"; }
function emptyRow(columns, label) { return `<tr><td class="empty" colspan="${columns}">${escapeHtml(label)}</td></tr>`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttr(value) { return escapeHtml(value); }
