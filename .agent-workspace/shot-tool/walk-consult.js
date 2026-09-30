// 走查「即时咨询」对话流程：输入、发送、加载反馈、排版渲染、追问点击。
const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const OUT = 'D:/visual-agent-latest/.agent-workspace/screenshots/walk';
const BASE = 'http://localhost:5173/#/';
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');

  const problems = [];
  page.on('pageerror', e => problems.push('JS异常: ' + String(e.message).slice(0, 120)));

  const shot = n => page.screenshot({ path: path.join(OUT, n + '.png') });
  const text = () => page.evaluate(() => document.body.innerText);

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
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, h: r.height };
    }, needle, exact);
  }
  async function tap(n, o = {}) {
    const b = await locate(n, o.exact || false);
    if (!b) { problems.push(`点不到：「${n}」`); return false; }
    await page.touchscreen.tap(b.x, b.y);
    await sleep(o.wait || 700);
    return true;
  }

  console.log('=== 即时咨询走查 ===');
  await page.goto(BASE + 'pages/diagnosis/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3000);

  await tap('即时咨询', { wait: 900 });
  await shot('c01-tab');
  let t = await text();
  if (!/输入|咨询|问题/.test(t)) problems.push('切到即时咨询后没有可用界面');

  // 建议问题是否可点
  const suggestions = await page.evaluate(() => {
    const els = [...document.querySelectorAll('uni-view')];
    return els.filter(e => e.className && String(e.className).includes('suggestion'))
      .map(e => e.innerText.trim().split('\n')[0]).slice(0, 5);
  });
  console.log('预置问题：', suggestions.join(' / ') || '(无)');

  // 输入并发送
  const input = await page.$('input');
  if (!input) { problems.push('找不到输入框'); }
  else {
    await input.click();
    await input.type('我家猫这两天有点软便，需要注意什么');
    await shot('c02-typed');
    const sendOk = await tap('发送', { wait: 600 });
    if (!sendOk) problems.push('发送按钮点不到');
    await shot('c03-sending');
    t = await text();
    if (!/分析中|思考|正在/.test(t)) problems.push('发送后没有等待反馈（用户不知道有没有发出去）');
    // 等回答
    for (let i = 0; i < 40; i++) {
      await sleep(1500);
      const now = await text();
      if (now.includes('仅供参考，建议咨询执业兽医')) break;
    }
    await shot('c04-answered');
    t = await text();
    console.log('\n=== 对话内容 ===');
    t.split('\n').filter(Boolean).slice(-24).forEach(l => console.log('  ' + l));

    // 排版是否生效：加粗/列表标记不应以原始 ** 出现
    if (/\*\*/.test(t)) problems.push('回答里出现未渲染的 ** 标记');
    // 追问是否可点
    const fq = await page.evaluate(() => {
      const els = [...document.querySelectorAll('uni-view')];
      const box = els.find(e => e.innerText?.includes('可以继续补充'));
      return box ? box.innerText.split('\n').slice(1, 3) : [];
    });
    console.log('\n追问项：', fq.join(' / ') || '(无)');
    if (!fq.length) problems.push('没有展示追问建议');
  }

  console.log('\n=== 交互问题 ===');
  if (problems.length) [...new Set(problems)].forEach(p => console.log('  · ' + p));
  else console.log('  （无）');
  await browser.close();
})();
