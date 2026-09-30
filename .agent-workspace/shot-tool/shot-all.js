const puppeteer = require('puppeteer-core');
const path = require('path');
const OUT = 'D:/visual-agent-latest/.agent-workspace/screenshots/audit';
require('fs').mkdirSync(OUT, { recursive: true });
const BASE = 'http://localhost:5173/#/';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const pages = [
  ['01-home', 'pages/index/index'],
  ['02-health', 'pages/diagnosis/index'],
  ['03-pets', 'pages/pets/index'],
  ['04-profile', 'pages/profile/index'],
  ['05-knowledge', 'pages/knowledge/index'],
  ['06-image', 'pages/image/index'],
  ['07-reminders', 'pages/tools/reminders'],
  ['08-timeline', 'pages/tools/timeline'],
  ['09-records', 'pages/records/index'],
  ['10-journal', 'pages/care/journal'],
];
(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');
  async function locate(t, ex) {
    return page.evaluate((t, ex) => {
      const els = [...document.querySelectorAll('uni-view,uni-button,uni-text,uni-input,input')];
      const m = els.filter(el => { const s = (el.innerText||el.value||el.placeholder||'').trim(); return ex ? s === t : s.includes(t); });
      if (!m.length) return null;
      const el = m[m.length-1]; el.scrollIntoView({block:'center'});
      const r = el.getBoundingClientRect();
      return r.width && r.height ? {x:r.left+r.width/2, y:r.top+r.height/2} : null;
    }, t, ex);
  }
  async function tap(t, ex, w) { const b = await locate(t, !!ex); if (!b) return false; await page.touchscreen.tap(b.x, b.y); await sleep(w||800); return true; }
  await page.goto(BASE + 'pages/pets/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  if (!await locate('皮蛋', true)) {
    await tap('添加', true, 1200); await tap('猫', true);
    const i = await page.$('uni-input input'); if (i) { await i.click(); await i.type('皮蛋'); }
    await sleep(400); await tap('保存档案', false, 2500);
  }
  for (const [name, route] of pages) {
    try {
      await page.goto(BASE + route, { waitUntil: 'networkidle2', timeout: 30000 });
      await sleep(2600);
      await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true });
      console.log('OK', name);
    } catch (e) { console.log('FAIL', name, e.message); }
  }
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
