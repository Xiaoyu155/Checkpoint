const puppeteer = require('puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await page.setUserAgent('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1');

  const errs = [];
  page.on('pageerror', e => errs.push(String(e.stack || e.message).split('\n').slice(0, 3).join(' | ')));

  await page.goto('http://localhost:8899/#/pages/index/index', { waitUntil: 'networkidle2', timeout: 30000 });
  await new Promise(r => setTimeout(r, 12000)); // 给足失败与重试的时间

  const state = await page.evaluate(() => {
    const txt = document.body.innerText;
    return {
      骨架屏还在: !!document.querySelector('.skeleton, [class*="skeleton"]'),
      有错误横幅: txt.includes('无法加载') || txt.includes('重试'),
      首屏文字: txt.split('\n').filter(Boolean).slice(0, 14),
    };
  });
  console.log('骨架屏还在:', state.骨架屏还在);
  console.log('有错误横幅:', state.有错误横幅);
  console.log('页面文字:');
  state.首屏文字.forEach(t => console.log('   ' + t));
  if (errs.length) {
    console.log('\nJS 异常:');
    [...new Set(errs)].slice(0, 5).forEach(e => console.log('   ' + e));
  }
  await page.screenshot({ path: 'D:/visual-agent-latest/.agent-workspace/screenshots/petcare/01-home-settled.png' });
  await browser.close();
})();
