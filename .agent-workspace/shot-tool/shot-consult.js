const puppeteer = require('puppeteer-core');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');
  await p.goto('http://localhost:5173/#/pages/diagnosis/index', { waitUntil: 'networkidle2' });
  await sleep(3000);
  // 切到即时咨询
  const seg = await p.evaluate(() => {
    const el = [...document.querySelectorAll('uni-view')].find(e => e.innerText.trim() === '即时咨询');
    if (!el) return null; const r = el.getBoundingClientRect();
    return { x: r.left + r.width/2, y: r.top + r.height/2 };
  });
  if (seg) { await p.touchscreen.tap(seg.x, seg.y); await sleep(1500); }
  await p.screenshot({ path: 'D:/宠物小程序/frontend/.uxwalk/consult-entry.png', fullPage: true });
  console.log((await p.evaluate(() => document.body.innerText)).split('\n').filter(Boolean).slice(0, 22).join('\n'));
  await b.close();
})();
