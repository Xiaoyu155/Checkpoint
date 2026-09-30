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
      const m = els.filter(el => { const s=(el.innerText||el.value||el.placeholder||'').trim(); return ex ? s===t : s.includes(t); });
      if (!m.length) return null;
      const el = m[m.length-1]; el.scrollIntoView({block:'center'});
      const r = el.getBoundingClientRect();
      return r.width && r.height ? {x:r.left+r.width/2,y:r.top+r.height/2} : null;
    }, t, ex);
  }
  async function tap(t, ex, w) { const b = await locate(t, !!ex); if (!b) { console.log('MISS', t); return false; } await page.touchscreen.tap(b.x, b.y); await sleep(w||800); return true; }
  await page.goto(BASE + 'pages/diagnosis/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2600);
  await tap('即时咨询', true, 1500);
  await page.screenshot({ path: 'D:/visual-agent-latest/.agent-workspace/screenshots/audit/02b-consult.png', fullPage: true });
  console.log('OK consult');
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
