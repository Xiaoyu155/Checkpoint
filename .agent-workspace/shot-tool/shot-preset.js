const puppeteer = require('puppeteer-core');
const BASE = 'http://localhost:5173/#/';
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');
  async function locate(t, ex) {
    return page.evaluate((t, ex) => {
      const els = [...document.querySelectorAll('uni-view,uni-button,uni-text,uni-input,input')];
      const m = els.filter(el => { const s = (el.innerText || el.value || el.placeholder || '').trim(); return ex ? s === t : s.includes(t); });
      if (!m.length) return null;
      const el = m[m.length - 1]; el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      return r.width && r.height ? { x: r.left + r.width/2, y: r.top + r.height/2, h: r.height } : null;
    }, t, ex);
  }
  async function tap(t, ex, w) { const b = await locate(t, !!ex); if (!b) { console.log('MISS:', t); return false; } await page.touchscreen.tap(b.x, b.y); await sleep(w||800); return true; }

  await page.goto(BASE + 'pages/pets/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  if (!await locate('皮蛋', true)) {
    await tap('添加', true, 1200); await tap('猫', true);
    const i = await page.$('uni-input input'); if (i) { await i.click(); await i.type('皮蛋'); }
    await sleep(400); await tap('保存档案', false, 2500);
  }
  await page.goto(BASE + 'pages/index/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3200);
  // 量快捷问题的触摸目标
  const chip = await locate('每天喂多少', true);
  console.log('chip height:', chip && Math.round(chip.h));
  await tap('每天喂多少', true, 3000);
  const t = await page.evaluate(() => document.body.innerText);
  console.log('落地页文本首几行：', t.split('\n').filter(Boolean).slice(0, 6).join(' | '));
  const val = await page.evaluate(() => { const el = document.querySelector('uni-textarea textarea, uni-input input, textarea'); return el ? el.value : '(no input)'; });
  console.log('输入框内容：', val);
  await page.screenshot({ path: 'D:/visual-agent-latest/.agent-workspace/screenshots/preset-landing.png' });
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
