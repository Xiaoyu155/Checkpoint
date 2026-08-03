(() => {
  "use strict";

  const STORAGE_KEY = "pacer.billing.api_key";
  const ACCOUNT_SESSION_MARKER = "pacer.billing.account_session";
  const state = {
    apiKey: "",
    packages: [],
    paymentReady: false,
    activeOrder: null,
    pollTimer: null,
    clockTimer: null,
    accountRegistering: false,
  };

  const el = Object.fromEntries(
    [
      "connectionState", "topBalance", "disconnectButton", "authView", "authForm",
      "apiKey", "accountEmail", "accountPassword", "accountCode", "emailAuthPanel", "apiKeyAuthPanel",
      "emailModeButton", "apiKeyModeButton", "emailLoginButton", "emailRegisterButton", "requestCodeButton", "registerCodePanel", "authError", "billingView", "accountTitle", "accountBalance",
      "accountPlan", "providerState", "packageList", "orderStatus", "orderEmpty",
      "orderActive", "orderAmount", "orderCredit", "qrFrame", "paymentQr",
      "qrPlaceholder", "orderCountdown", "orderNumber", "closeOrderButton",
      "orderResult", "resultTitle", "resultDetail", "refreshButton", "orderRows", "toast",
    ].map((id) => [id, document.getElementById(id)])
  );

  const statusLabels = {
    creating: "创建中",
    pending: "待支付",
    paid: "已到账",
    closed: "已取消",
    expired: "已过期",
    failed: "失败",
  };

  const errorLabels = {
    invalid_api_key: "API Key 无效或已失效。",
    expired_api_key: "API Key 已过期。",
    payment_order_pending: "已有一笔订单正在处理。",
    payment_package_not_found: "所选额度包已下架。",
    wechat_not_configured: "微信支付尚未配置。",
    invalid_payment_packages: "充值套餐配置无效。",
    wechat_network_error: "暂时无法连接微信支付。",
    wechat_api_error: "微信支付暂时未能处理该订单。",
    payment_amount_mismatch: "支付金额校验失败，订单未入账。",
  };

  function money(microusd) {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4,
    }).format(Number(microusd || 0) / 1_000_000);
  }

  function yuan(fen) {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency", currency: "CNY", minimumFractionDigits: 2,
    }).format(Number(fen || 0) / 100);
  }

  function localTime(timestamp) {
    if (!timestamp) return "-";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(new Date(Number(timestamp) * 1000));
  }

  function errorMessage(error) {
    return errorLabels[error.code] || error.message || "请求未完成，请稍后重试。";
  }

  function setAuthMode(mode) {
    const email = mode === "email";
    el.emailAuthPanel.hidden = !email;
    el.apiKeyAuthPanel.hidden = email;
    el.emailLoginButton.type = email ? "submit" : "button";
    el.emailModeButton.classList.toggle("active", email);
    el.apiKeyModeButton.classList.toggle("active", !email);
  }

  async function request(path, options = {}, authenticated = true) {
    const headers = new Headers(options.headers || {});
    if (authenticated) headers.set("Authorization", `Bearer ${state.apiKey}`);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : {};
    if (!response.ok) {
      const detail = payload.error || payload.detail || {};
      const error = new Error(typeof detail === "string" ? detail : detail.message || response.statusText);
      error.code = typeof detail === "object" ? detail.code : "";
      error.orderId = typeof detail === "object" ? detail.order_id : "";
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function setConnection(mode, label) {
    el.connectionState.className = `connection ${mode || ""}`.trim();
    el.connectionState.querySelector("span").textContent = label;
  }

  function setAccount(tenant) {
    el.accountTitle.textContent = tenant.name || tenant.tenant_name || "Pacer 客户";
    el.accountBalance.textContent = money(tenant.balance_microusd);
    el.accountPlan.textContent = tenant.plan_name || "Starter";
    el.topBalance.querySelector("strong").textContent = money(tenant.balance_microusd);
  }

  function showToast(message, isError = false) {
    el.toast.textContent = message;
    el.toast.className = `toast${isError ? " error" : ""}`;
    el.toast.hidden = false;
    window.setTimeout(() => { el.toast.hidden = true; }, 3200);
  }

  function makePackage(packageItem) {
    const article = document.createElement("article");
    article.className = "package-item";

    const title = document.createElement("h3");
    title.textContent = packageItem.name;
    const description = document.createElement("p");
    description.textContent = packageItem.description;

    const value = document.createElement("div");
    value.className = "package-value";
    const credit = document.createElement("div");
    const creditLabel = document.createElement("span");
    creditLabel.textContent = "到账额度";
    const creditAmount = document.createElement("strong");
    creditAmount.textContent = money(packageItem.credit_microusd);
    credit.append(creditLabel, creditAmount);

    const action = document.createElement("div");
    const price = document.createElement("strong");
    price.className = "price";
    price.textContent = yuan(packageItem.amount_fen);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button primary";
    button.textContent = "生成支付码";
    button.disabled = !state.paymentReady;
    button.dataset.packageId = packageItem.id;
    action.append(price, button);
    value.append(credit, action);
    article.append(title, description, value);
    return article;
  }

  function renderPackages() {
    el.packageList.replaceChildren();
    if (!state.packages.length) {
      const empty = document.createElement("div");
      empty.className = "empty-packages";
      empty.textContent = "暂无可购买的额度包";
      el.packageList.append(empty);
      return;
    }
    state.packages.forEach((item) => el.packageList.append(makePackage(item)));
  }

  async function loadPackages() {
    try {
      const payload = await request("/api/gateway/billing/packages", {}, false);
      state.packages = payload.packages || [];
      state.paymentReady = Boolean(payload.payment && payload.payment.ready);
      el.providerState.textContent = state.paymentReady ? "可支付" : "暂不可用";
      el.providerState.className = `provider-state ${state.paymentReady ? "ready" : "error"}`;
    } catch (error) {
      state.packages = [];
      state.paymentReady = false;
      el.providerState.textContent = "检查失败";
      el.providerState.className = "provider-state error";
    }
    renderPackages();
  }

  function clearTimers() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    if (state.clockTimer) window.clearInterval(state.clockTimer);
    state.pollTimer = null;
    state.clockTimer = null;
  }

  function renderCountdown() {
    const order = state.activeOrder;
    if (!order || !order.expires_at || order.status !== "pending") {
      el.orderCountdown.textContent = "--:--";
      return;
    }
    const seconds = Math.max(0, Math.ceil(Number(order.expires_at) - Date.now() / 1000));
    const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
    const remainder = String(seconds % 60).padStart(2, "0");
    el.orderCountdown.textContent = `${minutes}:${remainder}`;
  }

  function setOrderStatus(status) {
    el.orderStatus.textContent = statusLabels[status] || "暂无订单";
    const className = status === "paid" ? "paid" : status === "pending" || status === "creating" ? "pending" : status ? "failed" : "neutral";
    el.orderStatus.className = `status ${className}`;
  }

  function showOrder(payload) {
    const order = payload.order;
    state.activeOrder = order;
    clearTimers();
    setOrderStatus(order.status);
    el.orderEmpty.hidden = true;
    el.orderAmount.textContent = yuan(order.amount_fen);
    el.orderCredit.textContent = money(order.credit_microusd);
    el.orderNumber.textContent = order.out_trade_no;

    if (order.status === "pending" || order.status === "creating") {
      el.orderResult.hidden = true;
      el.orderActive.hidden = false;
      const qr = payload.qr_png_data_url || "";
      el.paymentQr.hidden = !qr;
      el.paymentQr.src = qr;
      el.qrPlaceholder.hidden = Boolean(qr);
      el.qrPlaceholder.textContent = order.status === "creating" ? "订单处理中" : "支付码待恢复";
      el.closeOrderButton.disabled = order.status !== "pending";
      renderCountdown();
      state.clockTimer = window.setInterval(renderCountdown, 1000);
      state.pollTimer = window.setInterval(() => refreshActiveOrder(false), 3000);
      return;
    }

    el.orderActive.hidden = true;
    el.orderResult.hidden = false;
    const paid = order.status === "paid";
    el.resultTitle.textContent = paid ? "额度已到账" : statusLabels[order.status] || "订单已结束";
    el.resultDetail.textContent = paid
      ? `${money(order.credit_microusd)} 已写入 Pacer 余额。`
      : `订单状态：${statusLabels[order.status] || order.status}`;
  }

  async function refreshActiveOrder(notify = false) {
    if (!state.activeOrder) return;
    try {
      const previous = state.activeOrder.status;
      const payload = await request(`/api/gateway/billing/wechat/orders/${encodeURIComponent(state.activeOrder.id)}`);
      if (payload.order.status !== previous || payload.order.status === "paid") {
        await Promise.all([loadAccount(), loadOrders()]);
        showOrder(payload);
        if (payload.order.status === "paid") showToast("支付成功，额度已到账。", false);
      } else if (notify) {
        showOrder(payload);
        showToast("订单状态已刷新。", false);
      } else {
        showOrder(payload);
      }
    } catch (error) {
      if (notify) showToast(errorMessage(error), true);
    }
  }

  function renderOrders(items) {
    el.orderRows.replaceChildren();
    if (!items.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "table-empty";
      cell.textContent = "暂无订单记录";
      row.append(cell);
      el.orderRows.append(row);
      return;
    }
    items.forEach((order) => {
      const row = document.createElement("tr");
      const cells = [
        ["创建时间", localTime(order.created_at)],
        ["额度包", order.package_name],
        ["金额", yuan(order.amount_fen)],
        ["状态", statusLabels[order.status] || order.status],
        ["订单号", order.out_trade_no],
      ];
      cells.forEach(([label, value], index) => {
        const cell = document.createElement("td");
        cell.dataset.label = label;
        if (index === 4) {
          const code = document.createElement("code");
          code.textContent = value;
          cell.append(code);
        } else {
          cell.textContent = value;
        }
        row.append(cell);
      });
      const actionCell = document.createElement("td");
      actionCell.dataset.label = "操作";
      if (["creating", "pending"].includes(order.status)) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "row-action";
        button.textContent = "继续支付";
        button.dataset.orderId = order.id;
        actionCell.append(button);
      }
      row.append(actionCell);
      el.orderRows.append(row);
    });
  }

  async function loadAccount() {
    const tenant = await request("/api/gateway/billing/me");
    setAccount(tenant);
    return tenant;
  }

  async function loadOrders() {
    const payload = await request("/api/gateway/billing/wechat/orders?limit=20");
    renderOrders(payload.items || []);
    return payload.items || [];
  }

  async function openOrder(orderId) {
    const payload = await request(`/api/gateway/billing/wechat/orders/${encodeURIComponent(orderId)}`);
    showOrder(payload);
    el.paymentQr.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function createOrder(packageId, button) {
    button.disabled = true;
    try {
      const payload = await request("/api/gateway/billing/wechat/orders", {
        method: "POST", body: JSON.stringify({ package_id: packageId }),
      });
      showOrder(payload);
      await loadOrders();
      el.paymentQr.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      if (error.code === "payment_order_pending" && error.orderId) {
        await openOrder(error.orderId);
      } else {
        showToast(errorMessage(error), true);
      }
    } finally {
      button.disabled = !state.paymentReady;
    }
  }

  async function connect(apiKey) {
    state.apiKey = apiKey.trim();
    setConnection("", "连接中");
    try {
      const [tenant, orders] = await Promise.all([loadAccount(), loadOrders(), loadPackages()]);
      sessionStorage.setItem(STORAGE_KEY, state.apiKey);
      el.authView.hidden = true;
      el.billingView.hidden = false;
      el.topBalance.hidden = false;
      el.disconnectButton.hidden = false;
      el.authError.hidden = true;
      setAccount(tenant);
      setConnection("online", "已连接");
      const resumable = orders.find((item) => ["creating", "pending"].includes(item.status));
      if (resumable) await openOrder(resumable.id);
    } catch (error) {
      state.apiKey = "";
      sessionStorage.removeItem(STORAGE_KEY);
      setConnection("error", "连接失败");
      el.authError.textContent = errorMessage(error);
      el.authError.hidden = false;
      throw error;
    }
  }

  async function connectSession() {
    state.apiKey = "";
    setConnection("", "连接中");
    try {
      const [tenant, orders] = await Promise.all([loadAccount(), loadOrders(), loadPackages()]);
      el.authView.hidden = true;
      el.billingView.hidden = false;
      el.topBalance.hidden = false;
      el.disconnectButton.hidden = false;
      el.authError.hidden = true;
      setAccount(tenant);
      setConnection("online", "已连接");
      const resumable = orders.find((item) => ["creating", "pending"].includes(item.status));
      if (resumable) await openOrder(resumable.id);
    } catch (error) {
      setConnection("error", "连接失败");
      el.authError.textContent = errorMessage(error);
      el.authError.hidden = false;
      throw error;
    }
  }

  function disconnect() {
    clearTimers();
    state.apiKey = "";
    state.activeOrder = null;
    sessionStorage.removeItem(STORAGE_KEY);
    el.billingView.hidden = true;
    el.topBalance.hidden = true;
    el.disconnectButton.hidden = true;
    el.authView.hidden = false;
    el.apiKey.value = "";
    el.accountPassword.value = "";
    el.accountCode.value = "";
    state.accountRegistering = false;
    el.registerCodePanel.hidden = true;
    el.emailRegisterButton.textContent = "注册";
    el.orderEmpty.hidden = false;
    el.orderActive.hidden = true;
    el.orderResult.hidden = true;
    setOrderStatus("");
    setConnection("", "未连接");
    localStorage.removeItem(ACCOUNT_SESSION_MARKER);
  }

  el.authForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = (el.emailAuthPanel.hidden ? el.apiKeyAuthPanel : el.emailAuthPanel).querySelector("button[type='submit']");
    button.disabled = true;
    try {
      if (el.emailAuthPanel.hidden) {
        await connect(el.apiKey.value);
      } else if (state.accountRegistering) {
        const payload = await request("/api/account/register", { method: "POST", body: JSON.stringify({ email: el.accountEmail.value, password: el.accountPassword.value, verification_code: el.accountCode.value }) }, false);
        await connectSession();
        localStorage.setItem(ACCOUNT_SESSION_MARKER, "1");
        showToast("账户已创建，登录成功。", false);
      } else {
        await request("/api/account/login", { method: "POST", body: JSON.stringify({ email: el.accountEmail.value, password: el.accountPassword.value }) }, false);
        await connectSession();
        localStorage.setItem(ACCOUNT_SESSION_MARKER, "1");
      }
    } catch (_error) { (el.emailAuthPanel.hidden ? el.apiKey : el.accountEmail).focus(); }
    finally { button.disabled = false; }
  });

  el.emailModeButton.addEventListener("click", () => setAuthMode("email"));
  el.apiKeyModeButton.addEventListener("click", () => setAuthMode("api"));
  el.emailRegisterButton.addEventListener("click", () => {
    state.accountRegistering = !state.accountRegistering;
    el.registerCodePanel.hidden = !state.accountRegistering;
    el.emailRegisterButton.textContent = state.accountRegistering ? "取消注册" : "注册";
    el.emailAuthPanel.querySelector("button[type='submit']").textContent = state.accountRegistering ? "完成注册" : "登录";
  });
  el.requestCodeButton.addEventListener("click", async () => {
    el.requestCodeButton.disabled = true;
    try {
      const payload = await request("/api/account/verification-codes", { method: "POST", body: JSON.stringify({ email: el.accountEmail.value, purpose: "register" }) }, false);
      showToast(payload.dev_code ? `验证码：${payload.dev_code}` : "验证码已发送，请查收邮箱。", false);
    } catch (error) { el.authError.textContent = errorMessage(error); el.authError.hidden = false; }
    finally { window.setTimeout(() => { el.requestCodeButton.disabled = false; }, 30000); }
  });

  el.disconnectButton.addEventListener("click", disconnect);
  el.packageList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-package-id]");
    if (button) createOrder(button.dataset.packageId, button);
  });
  el.orderRows.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-order-id]");
    if (button) openOrder(button.dataset.orderId).catch((error) => showToast(errorMessage(error), true));
  });
  el.refreshButton.addEventListener("click", async () => {
    el.refreshButton.disabled = true;
    try {
      await Promise.all([loadAccount(), loadOrders(), refreshActiveOrder(false)]);
      showToast("订单与余额已刷新。", false);
    } catch (error) {
      showToast(errorMessage(error), true);
    } finally {
      el.refreshButton.disabled = false;
    }
  });
  el.closeOrderButton.addEventListener("click", async () => {
    if (!state.activeOrder) return;
    el.closeOrderButton.disabled = true;
    try {
      const payload = await request(`/api/gateway/billing/wechat/orders/${encodeURIComponent(state.activeOrder.id)}/close`, { method: "POST" });
      showOrder(payload);
      await loadOrders();
      showToast("订单已取消。", false);
    } catch (error) {
      showToast(errorMessage(error), true);
      await refreshActiveOrder(false);
    } finally {
      el.closeOrderButton.disabled = false;
    }
  });

  loadPackages();
  if (localStorage.getItem(ACCOUNT_SESSION_MARKER) === "1") {
    fetch("/api/account/me", { credentials: "same-origin", cache: "no-store" }).then((response) => {
      if (response.ok) return connectSession();
      throw new Error("no-session");
    }).catch(() => { localStorage.removeItem(ACCOUNT_SESSION_MARKER); });
  }
  const savedKey = sessionStorage.getItem(STORAGE_KEY);
  if (savedKey) {
    el.apiKey.value = savedKey;
    connect(savedKey).catch(() => {});
  }
})();
