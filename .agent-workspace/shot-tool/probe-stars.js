const puppeteer = require('puppeteer-core');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto('http://localhost:5173/#/pages/diagnosis/index', { waitUntil: 'networkidle2' });
  await sleep(3000);
  const t = await p.evaluate(() => document.body.innerText);
  const lines = t.split('\n');
  console.log('=== 含 ** 的行 ===');
  lines.forEach((l, i) => { if (l.includes('**')) console.log(`  [${i}] ${l.slice(0,120)}`); });
  console.log('\n=== 预置问题元素 ===');
  const sug = await p.evaluate(() => [...document.querySelectorAll('uni-view')]
    .filter(e => String(e.className).includes('suggestion'))
    .map(e => ({ cls: String(e.className), text: e.innerText.trim().slice(0,40) })));
  sug.forEach(s => console.log(`  ${s.cls} -> ${s.text}`));
  await b.close();
})();
