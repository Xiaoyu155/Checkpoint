(function(root) {
  const api = {
    baseUrl(raw) {
      const url = new URL(raw.trim());
      if (url.username || url.password || url.search || url.hash) throw new Error('网关地址不能包含账号、查询参数或片段');
      const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
      if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) throw new Error('远程网关必须使用 HTTPS');
      return url.href.replace(/\/+$/, '');
    },
    session(rows) {
      const main = rows.find(x => x?.name === '__Secure-1PSID');
      const ts = rows.find(x => x?.name === '__Secure-1PSIDTS');
      if (!main?.value || !ts?.value) throw new Error('请在此浏览器配置中登录 Gemini，再绑定');
      if (main.domain !== ts.domain || main.path !== ts.path || main.storeId !== ts.storeId) throw new Error('检测到不同作用域的凭据，已阻止混合账号');
      const cc = rows.find(x => x?.name === '__Secure-1PSIDCC' && x.domain === main.domain && x.path === main.path && x.storeId === main.storeId);
      return {psid: main.value, psidts: ts.value, psidcc: cc?.value || ''};
    },
    verified(data) {
      if (data.success !== true || data.test_result?.valid !== true || !data.account?.id || !data.binding_hash) throw new Error(data.test_result?.message || data.detail || '网关未确认登录和模型可用');
      return data;
    },
    async hash(value) {
      const bytes = new TextEncoder().encode(value);
      return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
    }
  };
  root.GeminiBinding = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
