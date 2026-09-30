const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const OUT = process.env.SHOT_OUT || 'D:/visual-agent-latest/.agent-workspace/screenshots/petcare';
const BASE = 'http://localhost:8899/#/';

// 当前主线页面（对齐 pages.json，不含已下线功能）
const pages = [
  ['01-home', 'pages/index/index'],
  ['02-diagnosis', 'pages/diagnosis/index'],
  ['03-pets', 'pages/pets/index'],
  ['04-profile', 'pages/profile/index'],
  ['05-image', 'pages/image/index'],
  ['06-reminders', 'pages/tools/reminders'],
  ['07-timeline', 'pages/tools/timeline'],
  ['08-records', 'pages/records/index'],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');

  const consoleErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 160)); });
  page.on('pageerror', e => consoleErrors.push('PAGEERROR ' + String(e.message).slice(0, 160)));

  for (const [name, route] of pages) {
    try {
      await page.goto(BASE + route, { waitUntil: 'networkidle2', timeout: 30000 });
      await new Promise(r => setTimeout(r, 2200));
      // 首屏（真机可见范围）与整页各存一张：设计问题多半出在首屏
      await page.screenshot({ path: path.join(OUT, name + '-fold.png') });
      await page.screenshot({ path: path.join(OUT, name + '-full.png'), fullPage: true });
      console.log('OK  ' + name);
    } catch (e) {
      console.log('FAIL ' + name + ' ' + e.message);
    }
  }
  if (consoleErrors.length) {
    console.log('\n控制台错误 (' + consoleErrors.length + '):');
    [...new Set(consoleErrors)].slice(0, 12).forEach(e => console.log('  ' + e));
  } else {
    console.log('\n无控制台错误');
  }
  await browser.close();
})();
