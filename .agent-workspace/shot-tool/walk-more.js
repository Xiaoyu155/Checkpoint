// 走查其余交互路径：建档表单、报告页动作、错误恢复、返回行为。
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
      const els = [...document.querySelectorAll('uni-view,uni-button,uni-text,uni-input,input,uni-switch')];
      const m = els.filter(el => {
        const s = (el.innerText || el.value || el.placeholder || '').trim();
        return ex ? s === t : s.includes(t);
      });
      if (!m.length) return null;
      const el = m[m.length - 1];
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      let target = el;
      for (let i = 0; i < 4 && target.parentElement; i++) {
        const cls = String(target.className || '');
        if (/chip|btn|button|option|card|row|tile|item|segment/i.test(cls)
            || target.tagName === 'UNI-BUTTON'
            || getComputedStyle(target).cursor === 'pointer') break;
        target = target.parentElement;
      }
      const tr = target.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, h: tr.height };
    }, needle, exact);
  }
  async function tap(n, o = {}) {
    const b = await locate(n, o.exact || false);
    if (!b) { if (!o.optional) problems.push(`点不到：「${n}」`); return false; }
    if (b.h < 44 && !o.skipSize) problems.push(`点击区偏小(${Math.round(b.h)}px)：「${n}」`);
    await page.touchscreen.tap(b.x, b.y);
    await sleep(o.wait || 700);
    return true;
  }

  // ── 建档表单 ──────────────────────────────────
  console.log('=== 建档流程 ===');
  await page.goto(BASE + 'pages/pets/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  await shot('p01-archive');
  await tap('添加', { wait: 1200 });
  await shot('p02-form');
  let t = await text();
  if (!/添加宠物档案|物种/.test(t)) problems.push('建档：点添加后没有打开表单');

  // 不填必填项直接保存，看是否有明确提示
  const saveBtn = (await locate('保存', false)) ? '保存' : '完成';
  await tap(saveBtn, { wait: 900 });
  await shot('p03-validate');
  t = await text();
  if (!/请|必填|填写|名字/.test(t)) problems.push('建档：必填校验没有可见提示');
  console.log('空表单保存后的提示：', (t.match(/请[^\n]{0,20}/) || ['(无)'])[0]);

  // ── 报告页动作 ────────────────────────────────
  console.log('\n=== 报告页动作 ===');
  await page.goto(BASE + 'pages/diagnosis/result', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  await shot('p04-result-direct');
  t = await text();
  // 直接进报告页（无数据）应有明确空态而不是白屏
  if (t.trim().length < 40) problems.push('报告页无数据时接近白屏，没有空态引导');
  console.log('无数据直达报告页显示：', t.split('\n').filter(Boolean).slice(0, 6).join(' | ') || '(空)');

  // ── 图片观察 ──────────────────────────────────
  console.log('\n=== 图片观察 ===');
  await page.goto(BASE + 'pages/image/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(2500);
  await shot('p05-image');
  t = await text();
  // 未选图时主按钮应为禁用且说明原因
  const btnState = await page.evaluate(() => {
    const b = [...document.querySelectorAll('uni-button')].find(e => /开始整理|开始分析/.test(e.innerText));
    if (!b) return null;
    return { text: b.innerText.trim(), disabled: b.hasAttribute('disabled') || b.className.includes('disabled') };
  });
  console.log('未选图时主按钮：', JSON.stringify(btnState));
  if (btnState && !btnState.disabled) problems.push('图片页：没选图片时主按钮仍可点');

  console.log('\n=== 交互问题 ===');
  if (problems.length) [...new Set(problems)].forEach(p => console.log('  · ' + p));
  else console.log('  （无）');
  await browser.close();
})();
