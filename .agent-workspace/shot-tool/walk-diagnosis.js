// 真正走一遍问诊流程：用坐标级真实触摸，记录每一步的交互反馈。
// uni-app H5 的 @tap 依赖真实 touch 事件，DOM 的 el.click() 触发不了。
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

  // 找到含该文字、且是最内层可点元素的中心坐标；必要时先滚动进视口
  async function locate(needle, exact) {
    return page.evaluate((t, ex) => {
      // uni-app H5 渲染成 <uni-view>/<uni-button>/<uni-text> 自定义元素
      const els = [...document.querySelectorAll('uni-view,uni-button,uni-text,uni-label,button,view,text')];
      const matches = els.filter(el => {
        const s = (el.innerText || el.value || '').trim();
        return ex ? s === t : s.includes(t);
      });
      if (!matches.length) return null;
      // 取最深（最内层）的那个，避免点到包裹容器
      let el = matches[matches.length - 1];
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return null;
      // 触摸目标尺寸要量真正可点的那一层：内层 text 节点只有文字高度，
      // 量它会得到假的"点击区过小"。向上找带交互语义的祖先。
      let target = el;
      for (let i = 0; i < 4 && target.parentElement; i++) {
        const cls = String(target.className || '');
        const isControl = /chip|btn|button|option|card|row|tile|item|segment/i.test(cls)
          || target.tagName === 'UNI-BUTTON'
          || getComputedStyle(target).cursor === 'pointer';
        if (isControl) break;
        target = target.parentElement;
      }
      const tr = target.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: tr.width, h: tr.height };
    }, needle, exact);
  }

  async function tap(needle, opts = {}) {
    let box = await locate(needle, opts.exact || false);
    if (box) { await sleep(150); box = await locate(needle, opts.exact || false); }
    if (!box) { problems.push(`点不到：「${needle}」`); return false; }
    if (box.h < 44) problems.push(`点击区偏小(${Math.round(box.h)}px<44px)：「${needle}」`);
    await page.touchscreen.tap(box.x, box.y);
    await sleep(opts.wait || 650);
    return true;
  }

  console.log('=== 问诊流程走查 ===');
  await page.goto(BASE + 'pages/diagnosis/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3000);
  await shot('01-entry');

  // 步骤1
  await tap('以上情况都没有');
  let t = await text();
  if (!t.includes('已确认')) problems.push('步骤1：确认后没有状态反馈');
  await shot('02-screened');

  await tap('下一步', { wait: 1000 });
  t = await text();
  await shot('03-step2');
  if (!/物种|宠物和体况/.test(t)) { problems.push('步骤1→2 未推进'); }

  // 步骤2
  await tap('猫', { exact: true });
  await shot('04-species');
  t = await text();
  // 年龄段按钮文案未知，先打印可选项
  const step2Options = await page.evaluate(() => {
    const chips = [...document.querySelectorAll('uni-view,uni-text')]
      .map(e => e.innerText?.trim()).filter(s => s && s.length <= 8);
    return [...new Set(chips)].slice(0, 30);
  });
  console.log('步骤2 可选项：', step2Options.join(' / '));

  for (const age of ['1-3岁', '3-7岁', '< 1岁', '7-10岁']) {
    if (await locate(age, true)) { await tap(age, { exact: true }); break; }
  }
  await shot('05-age');

  await tap('下一步', { wait: 1000 });
  t = await text();
  await shot('06-step3');
  if (!/部位|不适/.test(t)) problems.push('步骤2→3 未推进');

  // 步骤3
  await tap('眼睛', { exact: true });
  await shot('07-location');
  await tap('下一步', { wait: 1000 });
  await shot('08-step4');

  // 步骤4
  await tap('分泌物增多');
  await tap('畏光流泪');
  const durations = await page.evaluate(() => {
    const all = [...document.querySelectorAll('uni-view,uni-text')].map(e => e.innerText?.trim());
    return [...new Set(all.filter(s => s && /天|小时|周|以上/.test(s) && s.length <= 8))].slice(0, 10);
  });
  console.log('步骤4 时长选项：', durations.join(' / '));
  if (durations.length) await tap(durations[0], { exact: true });
  await shot('09-symptoms');

  await tap('下一步', { wait: 1000 });
  await shot('10-step5');
  t = await text();

  // 提交
  const btn = (await locate('生成照护草稿', false)) ? '生成照护草稿' : '整理急诊交接草稿';
  await tap(btn, { wait: 800 });
  await shot('11-submitting');
  t = await text();
  if (!/分析中|生成中|整理中|正在/.test(t)) problems.push('提交后没有进行中反馈');

  for (let i = 0; i < 45; i++) {
    await sleep(1500);
    const now = await text();
    if (/紧急程度|现在先做/.test(now)) break;
  }
  await shot('12-report');
  t = await text();
  console.log('\n=== 报告页 ===');
  t.split('\n').filter(Boolean).slice(0, 20).forEach(l => console.log('  ' + l));

  console.log('\n=== 交互问题 ===');
  if (problems.length) [...new Set(problems)].forEach(p => console.log('  · ' + p));
  else console.log('  （无）');
  await browser.close();
})();
