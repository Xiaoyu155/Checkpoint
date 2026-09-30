const puppeteer = require('puppeteer-core');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto('http://localhost:5173/#/pages/diagnosis/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3000);
  console.log(JSON.stringify(await p.evaluate(() => {
    const el = document.querySelector('.health-chat');
    const cs = getComputedStyle(el);
    const tb = document.querySelector('uni-tabbar');
    const tcs = tb ? getComputedStyle(tb) : null;
    const inner = tb ? tb.querySelector('.uni-tabbar') : null;
    return {
      chatHeight: cs.height,
      tabbarPos: tcs && tcs.position,
      tabbarRect: tb && JSON.stringify(tb.getBoundingClientRect()),
      innerRect: inner && JSON.stringify(inner.getBoundingClientRect()),
      scrollY: window.scrollY,
      bodyH: document.body.scrollHeight,
    };
  }), null, 1));
  await b.close();
})().catch(e => { console.error(e.message); process.exit(1); });
