const puppeteer = require('puppeteer-core');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto('http://localhost:5173/#/pages/diagnosis/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3000);
  console.log(JSON.stringify(await p.evaluate(() => {
    const out = { vh: window.innerHeight };
    const chat = document.querySelector('.health-chat');
    out.chat = chat ? { h: chat.getBoundingClientRect().height, cls: chat.className } : null;
    const row = document.querySelector('.ask-input-row');
    if (row) { const r = row.getBoundingClientRect(); out.inputRow = { top: Math.round(r.top), bottom: Math.round(r.bottom) }; }
    const tb = document.querySelector('uni-tabbar, .uni-tabbar');
    if (tb) { const r = tb.getBoundingClientRect(); out.tabbar = { top: Math.round(r.top), h: Math.round(r.height) }; }
    return out;
  }), null, 1));
  await b.close();
})().catch(e => { console.error(e.message); process.exit(1); });
