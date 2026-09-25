// Drives the served page the way a reviewer does: the only check that catches a page rendered
// with no comment form. Usage: node drive.cjs <url> <out dir>   (needs playwright + chromium)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [url, out] = process.argv.slice(2);
const failures = [];
const expect = (label, ok) => { console.log(`  ${ok ? '✓' : '✗'} ${label}`); if (!ok) failures.push(label); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

  await page.goto(url);
  await page.waitForTimeout(800);
  expect('saved on the server', (await page.locator('#state-enreg').innerText()).includes('saved on the server'));
  // The view is a machine-wide pref (~/.config/localpr/prefs.json): a split table pairs a deleted
  // and an added line on one row, so the row count is only comparable to the model in unified.
  await page.click('#view-unified');
  await page.waitForTimeout(300);

  const model = JSON.parse(fs.readFileSync(path.join(out, 'diff.json'), 'utf8'));
  const renamedModel = model.files.find((f) => f.path === 'src/Domain/Commission/New.php');
  const renamedLines = renamedModel.hunks.reduce((n, h) => n + h.lines.length, 0);
  const renamed = page.locator('.file-diff[data-path="src/Domain/Commission/New.php"]');
  expect(`renamed+edited file renders its ${renamedLines} rows`,
    renamedLines > 0 && (await renamed.locator('tr.commentable').count()) === renamedLines);
  expect('renamed+edited file badged from its old name',
    (await renamed.locator('.file-diff-head .badge-outline').allInnerTexts()).join(' ') === 'renamed from Old.php');
  expect('pure rename keeps its note',
    (await page.locator('.file-diff[data-path="moved.txt"] .file-diff-body').innerText()).includes('rename detected'));
  expect('quoted names render', (await page.locator('.file-diff[data-path=\'we"ird.txt\'], .file-diff[data-path="back\\\\slash.txt"]').count()) === 2);

  // comment on a new-side line
  const keep = page.locator('.file-diff[data-path="keep.txt"]');
  const keepLine = keep.locator('.line-code[data-side="new"][data-line="3"]');
  await keepLine.click();
  await page.waitForTimeout(200);
  expect('clicking the line alone opens no form', (await page.locator('.form-row-inline').count()) === 0);
  await keepLine.hover();
  await page.locator('.add-comment-btn').click();
  await page.locator('.form-row-inline textarea').fill('Trailing newline missing');
  await page.locator('.form-row-inline label[data-sev="nitpick"]').click();
  await page.locator('.form-row-inline [data-ok]').click();
  await page.waitForTimeout(600);
  expect('thread shown under the line', (await keep.locator('.thread-row .thread').count()) === 1);
  expect('type label rendered', (await keep.locator('.thread .sev-tag').innerText()) === 'Follow-up');
  expect('tracker lists it', (await page.locator('#tracker-list .tracker-item').count()) === 1);

  await page.reload();
  await page.waitForTimeout(900);
  expect('thread still under its line after a refresh',
    (await keep.locator('.thread-row .thread').count()) === 1 &&
    (await keep.locator('tr.has-thread').count()) === 1);

  const vierge = await (await browser.newContext()).newPage();
  await vierge.goto(url);
  await vierge.waitForTimeout(900);
  expect('comment served to a browser with no localStorage',
    (await vierge.locator('.file-diff[data-path="keep.txt"] .thread-row .thread').count()) === 1);
  await vierge.context().close();

  // comment on a deleted line
  await renamed.locator('.line-code[data-side="old"][data-line="2"]').hover();
  await page.locator('.add-comment-btn').click();
  await page.locator('.form-row-inline textarea').fill('why uppercase?');
  await page.keyboard.press('Control+Enter');
  await page.waitForTimeout(600);
  expect('deleted-line badge', (await renamed.locator('.thread-row .badge-outline').allInnerTexts()).includes('deleted line'));

  // split view keeps the threads
  await page.click('#view-split');
  await page.waitForTimeout(500);
  expect('split tables built', (await page.locator('.diff-table.split').count()) > 0);
  expect('threads survive the split', (await page.locator('.thread-row').count()) === 2);

  // markdown is escaped
  await page.click('#global');
  await page.locator('.form-row-inline textarea').fill('**bold** and <b>xss?</b>');
  await page.locator('.form-row-inline [data-ok]').click();
  await page.waitForTimeout(500);
  expect('markdown escaped', (await page.locator('#globaux .comment-body').innerHTML()) === '<p><strong>bold</strong> and &lt;b&gt;xss?&lt;/b&gt;</p>');

  expect('send button counts what is not sent', (await page.locator('#envoyer').innerText()) === 'Send 3 comment(s)');
  await page.click('#envoyer');
  await page.waitForTimeout(800);
  expect('batch sent', (await page.locator('#state-enreg').innerText()).includes('batch 1 sent'));
  const batch = path.join(out, 'batch-1.md');
  expect('batch-1.md holds the three comments', fs.existsSync(batch) &&
    fs.readFileSync(batch, 'utf8').startsWith('# 3 comment(s) to handle'));
  expect('events.log announces the batch',
    fs.readFileSync(path.join(out, 'events.log'), 'utf8').includes(`batch 1: 3 comment(s) - to handle: ${batch}`));
  expect('server still up after a batch', fs.existsSync(path.join(out, 'server.json')) &&
    (await page.evaluate(() => fetch('/ping', { method: 'POST', headers: { 'X-Localpr-Token': window.LOCALPR.token } }).then((r) => r.status))) === 200);
  expect('a sent comment can no longer be deleted', (await page.locator('[data-sup]').count()) === 0);
  expect('nothing left to send', await page.locator('#envoyer').isDisabled());

  await page.evaluate(() => {
    const cle = 'localpr:' + window.LOCALPR.repo + ':' + window.LOCALPR.base;
    const st = JSON.parse(localStorage.getItem(cle));
    st.n += 1;
    st.comments.push({ id: 'C' + st.n, scope: 'global', type: 'fix', side: null, file: null,
      fichier_index: null, line: null, lineEnd: null, anchor: null, anchorOffset: null,
      fingerprint: null, body: 'kept while the server was unreachable', origin: null,
      state: 'open', deposeA: new Date().toISOString() });
    st.updated = new Date().toISOString();
    localStorage.setItem(cle, JSON.stringify(st));
  });
  await page.reload();
  await page.waitForTimeout(900);
  expect('a locally kept comment outlives a page served with an older stamp',
    (await page.locator('#tracker-list .tracker-item').count()) === 4);

  await page.click('#terminer');
  await page.waitForTimeout(1500);
  expect('review sent', (await page.locator('#state-enreg').innerText()).includes('review sent'));
  expect('TODO.md written', fs.existsSync(path.join(out, 'TODO.md')));
  const todo = fs.readFileSync(path.join(out, 'TODO.md'), 'utf8');
  expect('TODO.md repeats nothing already sent', todo.startsWith('# 1 comment(s) to handle') &&
    todo.includes('Already sent and not repeated here: batch-1.md.'));
  expect('a sent comment stays sent after a reload', (await page.locator('.badge-outline', { hasText: /^sent$/ }).count()) === 3);
  expect('done sentinel written', fs.existsSync(path.join(out, 'done')));
  expect('server.json removed on clean shutdown', !fs.existsSync(path.join(out, 'server.json')));
  expect('no JS error', errors.length === 0);
  if (errors.length) console.log(errors);

  await browser.close();
  console.log(failures.length ? `page: ${failures.length} failure(s)` : 'page: OK');
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
