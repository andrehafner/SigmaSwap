// Renders downloaded token logos (SVG or PNG) to 192x192 transparent PNGs.
// Called by tools/fetch_icons.py - see there.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const SIZE = 192;
const UNKNOWN = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <circle cx="96" cy="96" r="88" fill="#3a3a3f"/>
  <circle cx="96" cy="96" r="88" fill="none" stroke="#6b6b73" stroke-width="6"/>
  <text x="96" y="130" font-family="Arial, Helvetica, sans-serif" font-size="104" font-weight="700"
        text-anchor="middle" fill="#c9c9cf">?</text></svg>`;

(async () => {
  const { out, jobs } = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: SIZE, height: SIZE }, deviceScaleFactor: 1 });
  const render = async (mime, bytes, file) => {
    const src = `data:${mime};base64,${bytes.toString('base64')}`;
    await page.setContent(
      `<body style="margin:0;background:transparent"><img id="i" src="${src}"
        style="width:${SIZE}px;height:${SIZE}px;object-fit:contain;display:block"></body>`);
    const ok = await page.evaluate(() => new Promise(res => {
      const i = document.getElementById('i');
      const done = () => res(i.naturalWidth > 0);
      if (i.complete) done(); else { i.onload = done; i.onerror = () => res(false); }
    }));
    if (!ok) return false;
    await page.screenshot({ path: file, omitBackground: true, clip: { x: 0, y: 0, width: SIZE, height: SIZE } });
    return true;
  };
  let n = 0;
  for (const j of jobs) {
    if (!/^[0-9a-f]{64}$/.test(j.id)) continue;
    if (await render(j.mime, fs.readFileSync(j.raw), path.join(out, j.id + '.png'))) n++;
    else console.error('could not render', j.id);
  }
  await render('image/svg+xml', Buffer.from(UNKNOWN), path.join(out, 'unknown.png'));
  await browser.close();
  console.log(`rendered ${n} logos to ${out}`);
})();
