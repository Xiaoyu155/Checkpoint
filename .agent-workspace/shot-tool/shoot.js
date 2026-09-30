const puppeteer = require('puppeteer-core');
const path = require('path');

const OUT = 'D:/visual-agent-latest/.agent-workspace/screenshots/h5';
const BASE = 'http://localhost:8899/#/';

const pages = [
  ['01-home', 'pages/index/index'],
  ['02-diagnosis', 'pages/diagnosis/index'],
  ['03-pets', 'pages/pets/index'],
  ['04-profile', 'pages/profile/index'],
  ['05-knowledge', 'pages/knowledge/index'],
  ['06-image', 'pages/image/index'],
  ['07-virtualpet-list', 'pages/virtual-pet/list'],
  ['08-reminders', 'pages/tools/reminders'],
  ['09-timeline', 'pages/tools/timeline'],
  ['10-translate', 'pages/translate/index'],
  ['11-estimate', 'pages/tools/estimate'],
  ['12-hospitals', 'pages/tools/hospitals'],
  ['13-records', 'pages/records/index'],
  ['14-lost', 'pages/lost/index'],
  ['15-community', 'pages/tools/community'],
  ['16-video', 'pages/tools/video'],
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');

  for (const [name, route] of pages) {
    try {
      await page.goto(BASE + route, { waitUntil: 'networkidle2', timeout: 30000 });
      await new Promise(r => setTimeout(r, 2500));
      await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true });
      console.log('OK', name);
    } catch (e) {
      console.log('FAIL', name, e.message);
    }
  }
  await browser.close();
})();
