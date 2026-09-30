const puppeteer = require('puppeteer-core');
const BASE = 'http://localhost:5173/#/';
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');

  async function locate(needle, exact) {
    return page.evaluate((t, ex) => {
      const els = [...document.querySelectorAll('uni-view,uni-button,uni-text,uni-input,input')];
      const m = els.filter(el => {
        const s = (el.innerText || el.value || el.placeholder || '').trim();
        return ex ? s === t : s.includes(t);
      });
      if (!m.length) return null;
      const el = m[m.length - 1];
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }, needle, exact);
  }
  async function tap(n, exact, wait) {
    const b = await locate(n, !!exact);
    if (!b) return false;
    await page.touchscreen.tap(b.x, b.y);
    await sleep(wait || 700);
    return true;
  }

  await page.goto(BASE + 'pages/pets/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  if (!await locate('皮蛋', true)) {
    await tap('添加', true, 1200);
    await tap('猫', true);
    const nameInput = await page.$('uni-input input');
    if (nameInput) { await nameInput.click(); await nameInput.type('皮蛋'); }
    await sleep(400);
    await tap('保存档案', false, 2500);
  }
  await page.goto(BASE + 'pages/index/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3500);
  await page.screenshot({ path: 'D:/visual-agent-latest/.agent-workspace/screenshots/home-redo.png', fullPage: true });
  console.log('OK');
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
