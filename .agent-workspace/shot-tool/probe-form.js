const puppeteer = require('puppeteer-core');
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new' });
  const p = await b.newPage();
  await p.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await p.goto('http://localhost:5173/#/pages/pets/index', { waitUntil: 'networkidle2' });
  await sleep(2500);
  // 打开表单
  const add = await p.evaluate(() => {
    const el = [...document.querySelectorAll('uni-button')].find(e => e.innerText.trim() === '添加');
    if (!el) return null; const r = el.getBoundingClientRect();
    return { x: r.left + r.width/2, y: r.top + r.height/2, h: r.height };
  });
  console.log('添加按钮高度:', add ? Math.round(add.h) + 'px' : '未找到');
  if (add) { await p.touchscreen.tap(add.x, add.y); await sleep(1200); }

  const info = await p.evaluate(() => {
    const chips = [...document.querySelectorAll('uni-view')].filter(e => String(e.className).split(' ').includes('chip'));
    const sample = chips.slice(0, 4).map(e => {
      const cs = getComputedStyle(e);
      return { text: e.innerText.trim(), on: String(e.className).includes('chip-on'), bg: cs.backgroundColor, color: cs.color, h: Math.round(e.getBoundingClientRect().height) };
    });
    const inputs = [...document.querySelectorAll('uni-input input')].map(i => {
      const cs = getComputedStyle(i);
      return { ph: i.placeholder, value: i.value, color: cs.color, weight: cs.fontWeight };
    });
    const save = [...document.querySelectorAll('uni-button')].find(e => e.innerText.includes('保存档案'));
    const cs = save && getComputedStyle(save);
    return { sample, inputs: inputs.slice(0, 3), save: save ? { disabled: save.hasAttribute('disabled'), bg: cs.backgroundColor, color: cs.color, opacity: cs.opacity } : null };
  });
  console.log('\nchip 样本:'); info.sample.forEach(c => console.log(`  ${c.text.padEnd(6)} selected=${c.on} h=${c.h}px bg=${c.bg} color=${c.color}`));
  console.log('\n输入框 placeholder 样式:'); info.inputs.forEach(i => console.log(`  "${i.ph}" value="${i.value}" color=${i.color} weight=${i.weight}`));
  console.log('\n保存按钮:', JSON.stringify(info.save));
  await b.close();
})();
