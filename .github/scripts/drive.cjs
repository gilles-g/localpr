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
  await page.click('#tab-conversation');
  expect('the conversation lists it, ending its snippet on the commented line',
    (await page.locator('#tracker-list .conv-card').count()) === 1 &&
    (await page.locator('#tracker-list .conv-snippet tr:last-child .conv-num').innerText()) === '3');
  await page.click('#tab-files');
  expect('leaving the conversation drops its copies of the threads',
    (await page.locator('#tracker-list .conv-card').count()) === 0);

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
  await page.click('#tab-conversation');
  expect('a locally kept comment outlives a page served with an older stamp',
    (await page.locator('#tracker-list .conv-card').count()) === 4);
  await page.click('#tab-files');

  await page.click('#global');
  await page.locator('.form-row-inline [data-ask]').check();
  await page.locator('.form-row-inline [data-skill]').fill('dev-gourou');
  await page.locator('.form-row-inline textarea').fill('is this the right place for it?');
  await page.locator('.form-row-inline [data-ok]').click();
  await page.waitForTimeout(800);
  const log = fs.readFileSync(path.join(out, 'events.log'), 'utf8');
  const asked = log.match(/^ask (\d+): 1 comment\(s\) - to handle: (.+)$/m);
  expect('a question leaves at once, on its own', !!asked && fs.existsSync(asked[2]));
  const askFile = asked ? fs.readFileSync(asked[2], 'utf8') : '';
  expect('the question names its skill', askFile.includes('through the `dev-gourou` skill'));
  const askId = (askFile.match(/\*\*(C\d+)\*\*/) || [])[1];
  expect('the thread waits for Claude', (await page.locator('#thread-' + askId + ' .response.thinking').count()) === 1);
  await page.click('#global');
  await page.locator('.form-row-inline textarea').fill('typed while Claude answers');
  fs.writeFileSync(path.join(out, 'replies', askId + '.json'),
    JSON.stringify({ comment: askId, verdict: 'answered', response: 'Yes, **keep it** there.' }));
  await page.waitForTimeout(1500);
  expect('the answer shows up without a reload',
    (await page.locator('#thread-' + askId + ' .response.answer .md').innerHTML()) === '<p>Yes, <strong>keep it</strong> there.</p>');
  expect('the form being typed survives the answer',
    (await page.locator('.form-row-inline textarea').inputValue()) === 'typed while Claude answers');
  await page.locator('.form-row-inline [data-cancel]').click();

  const fil = page.locator('#thread-' + askId);
  await fil.locator('[data-reply]').click();
  expect('a reply to a question asks Claude again, through the same skill',
    (await page.locator('.form-row-inline [data-ask]').isChecked()) &&
    (await page.locator('.form-row-inline [data-skill]').inputValue()) === 'dev-gourou');
  await page.locator('.form-row-inline textarea').fill('and in the other bounded context?');
  await page.locator('.form-row-inline [data-ok]').click();
  await page.waitForTimeout(800);
  const relance = fs.readFileSync(path.join(out, 'events.log'), 'utf8').match(/^ask (\d+): 1 comment\(s\) - to handle: (.+)$/gm);
  const relanceFile = relance && relance.length === 2 ? fs.readFileSync(relance[1].split('to handle: ')[1], 'utf8') : '';
  const relanceId = (relanceFile.match(/^- \*\*(C\d+)\*\*/m) || [])[1];
  expect('the follow-up question carries the thread so far',
    relanceFile.includes(`in reply to **${askId}**`) && relanceFile.includes('> Yes, **keep it** there.'));
  expect('the follow-up sits in the same thread', (await fil.locator('#comment-' + relanceId + ' .response.thinking').count()) === 1);
  fs.writeFileSync(path.join(out, 'replies', relanceId + '.json'),
    JSON.stringify({ comment: relanceId, verdict: 'answered', response: 'Same answer there.' }));
  await page.waitForTimeout(1500);
  expect('the follow-up answer shows up in the thread',
    (await fil.locator('#comment-' + relanceId + ' .response.answer .md').innerText()) === 'Same answer there.');

  await fil.locator('[data-reply]').click();
  await page.locator('.form-row-inline [data-ask]').uncheck();
  await page.locator('.form-row-inline label[data-sev="blocking"]').click();
  await page.locator('.form-row-inline textarea').fill('do what you suggested');
  await page.locator('.form-row-inline [data-ok]').click();
  await page.waitForTimeout(600);
  expect('a plain reply waits for the next batch', (await page.locator('#envoyer').innerText()) === 'Send 2 comment(s)' &&
    (await fil.locator('.thread-reply').count()) === 2);

  await page.click('#relire');
  await page.locator('#relire-skill').fill('code-review');
  await page.click('#relire-go');
  await page.waitForTimeout(800);
  const revue = fs.readFileSync(path.join(out, 'events.log'), 'utf8')
    .match(/^review (\d+): the whole diff, through the `code-review` skill - to handle: (.+)$/m);
  expect('a review asked of Claude is announced', !!revue && fs.existsSync(revue[2]));
  const run = revue ? revue[1] : '0';
  const runFile = path.join(out, `findings-${run}.json`);
  expect('the review request points at its findings file and its skill', !!revue &&
    fs.readFileSync(revue[2], 'utf8').includes(runFile) &&
    fs.readFileSync(revue[2], 'utf8').includes('through the `code-review` skill'));
  expect('the findings file is opened, not done', fs.existsSync(runFile) &&
    JSON.parse(fs.readFileSync(runFile, 'utf8')).done === false);
  expect('the button says Claude is reviewing', (await page.locator('#relire').innerText()) === 'Claude is reviewing…');
  await page.click('#global');
  await page.locator('.form-row-inline textarea').fill('typed while Claude reviews');
  const suggestions = [
    { file: 'src/Domain/Commission/New.php', line: 2, extrait: 'B', severity: 'major',
      finding: 'Why **uppercase**?', recommendation: 'keep it lowercase' },
    { severity: 'minor', finding: 'The rename deserves a note in the changelog.' },
  ];
  fs.writeFileSync(runFile, JSON.stringify({ source: 'Claude', skill: 'code-review', done: false, findings: suggestions }));
  await page.waitForTimeout(1500);
  const surLigne = renamed.locator(`.thread-row #thread-F${run}-1`);
  expect('a suggestion shows up under its line without a reload', (await surLigne.count()) === 1 &&
    (await surLigne.locator('.comment-body').innerHTML()) === '<p>Why <strong>uppercase</strong>? — keep it lowercase</p>');
  expect('a suggestion carries its author and severity', (await surLigne.locator('.who').innerText()) === 'Claude' &&
    (await surLigne.locator('.sev-tag').innerText()) === 'Suggestion' &&
    (await surLigne.locator('.badge-outline').allInnerTexts()).includes('major'));
  expect('a suggestion without a file is global', (await page.locator(`#globaux #thread-F${run}-2`).count()) === 1);
  expect('the form being typed survives the suggestions',
    (await page.locator('.form-row-inline textarea').inputValue()) === 'typed while Claude reviews');
  await page.locator('.form-row-inline [data-cancel]').click();
  fs.writeFileSync(runFile, JSON.stringify({ source: 'Claude', skill: 'code-review', done: true, findings: suggestions }));
  await page.waitForTimeout(1500);
  expect('the end of the review is announced',
    (await page.locator('#state-enreg').innerText()).includes('Claude reviewed: 2 suggestion(s)') &&
    (await page.locator('#relire').innerText()) === 'Review with Claude');

  const globale = page.locator(`#globaux #thread-F${run}-2`);
  const ecarts = () => JSON.parse(fs.readFileSync(path.join(out, 'comments.json'), 'utf8')).dismissed || {};
  await page.evaluate(() => {
    const cle = 'localpr:' + window.LOCALPR.repo + ':' + window.LOCALPR.base;
    const s = JSON.parse(localStorage.getItem(cle));
    s.dismissed = { 'F99-1': { reason: 'duplicate', body: 'from an earlier review' } };
    s.updated = new Date(Date.now() + 1000).toISOString();
    localStorage.setItem(cle, JSON.stringify(s));
  });
  await page.reload();
  await page.waitForTimeout(900);
  await globale.locator('[data-ecart]').click();
  expect('dismissing a suggestion asks for a reason',
    (await globale.locator('[data-ecarter]').allInnerTexts()).join(',') === 'not relevant,duplicate,outdated');
  await globale.locator('[data-raison="not-relevant"]').click();
  await page.waitForTimeout(600);
  expect('a dismissed suggestion folds, with its reason',
    (await globale.locator('details:not([open]) summary').innerText()).includes('dismissed as not relevant') &&
    !(await globale.locator('.comment-body').isVisible()));
  expect('a dismissal is saved with the body it was made on, an earlier review\'s dropped',
    Object.keys(ecarts()).join() === `F${run}-2` && ecarts()[`F${run}-2`].reason === 'not-relevant' &&
    ecarts()[`F${run}-2`].body === 'The rename deserves a note in the changelog.');
  await page.reload();
  await page.waitForTimeout(900);
  expect('a dismissal survives a reload', (await globale.locator('details:not([open])').count()) === 1);
  await globale.locator('summary').click();
  await globale.locator('[data-retablir]').click();
  await page.waitForTimeout(600);
  expect('a restored suggestion can be taken up again',
    (await globale.locator(`[data-rep="F${run}-2"]`).count()) === 1 && !ecarts()[`F${run}-2`]);
  await globale.locator('[data-ecart]').click();
  await globale.locator('[data-raison="outdated"]').click();
  await page.waitForTimeout(600);
  await page.click('#relire');
  await page.click('#relire-go');
  await page.waitForTimeout(800);
  const suivante = [...fs.readFileSync(path.join(out, 'events.log'), 'utf8')
    .matchAll(/^review (\d+): .* - to handle: (.+)$/gm)].pop();
  expect('the next review is told what was dismissed', !!suivante && suivante[1] !== run &&
    fs.readFileSync(suivante[2], 'utf8')
      .includes('- the whole review - outdated\n  > The rename deserves a note in the changelog.'));
  fs.writeFileSync(path.join(out, `findings-${suivante ? suivante[1] : 0}.json`),
    JSON.stringify({ source: 'Claude', done: true, findings: [] }));
  await page.waitForTimeout(1500);

  const arbre = page.locator(`.tree-file[data-f="${renamedModel.index}"] .tree-decide`);
  expect('the file tree flags the suggestion left to decide',
    (await arbre.isVisible()) && (await arbre.innerText()) === '1');
  await page.click('#tab-conversation');
  const aTrancher = page.locator(`#tracker-list [data-carte="F${run}-1"]`);
  expect('the conversation counts what is left to decide',
    (await page.locator('#ct-decide').innerText()) === '1 to decide' &&
    (await page.locator(`#tracker-list [data-carte="F${run}-2"]`).getAttribute('class')).includes('dismissed'));
  expect('a suggestion card shows the code it is about',
    (await aTrancher.locator('.conv-snippet tr.add .conv-code').innerText()).includes('B'));
  await page.click('#ct-decide');
  expect('to decide filters everything else out',
    (await page.locator('#tracker-list .conv-card:visible').count()) === 1 && (await aTrancher.isVisible()));
  await aTrancher.locator(`[data-rep="F${run}-1"]`).click();
  await page.waitForTimeout(600);
  expect('a suggestion taken up from the conversation becomes a comment to send',
    (await page.locator('#envoyer').innerText()) === 'Send 3 comment(s)' &&
    (await page.locator(`#thread-F${run}-1, #conv-F${run}-1`).count()) === 0 &&
    !(await page.locator('#ct-decide').isVisible()) && !(await arbre.isVisible()));
  const carteEnvoyee = page.locator('#tracker-list .conv-card', { has: page.locator('[data-reply]') }).first();
  await carteEnvoyee.locator('[data-reply]').click();
  expect('a sent thread can be replied to from the conversation',
    (await carteEnvoyee.locator('.form-row-inline textarea').count()) === 1);
  await carteEnvoyee.locator('.form-row-inline [data-cancel]').click();
  const visee = await carteEnvoyee.getAttribute('data-carte');
  await carteEnvoyee.locator('[data-goto]').click();
  await page.waitForTimeout(400);
  expect('View in code goes to the thread in the diff',
    (await page.locator('#pane-files').isVisible()) && (await page.locator(`#thread-${visee}`).isVisible()));

  await page.click('#terminer');
  await page.waitForTimeout(1500);
  expect('review sent', (await page.locator('#state-enreg').innerText()).includes('review sent'));
  expect('TODO.md written', fs.existsSync(path.join(out, 'TODO.md')));
  const todo = fs.readFileSync(path.join(out, 'TODO.md'), 'utf8');
  expect('TODO.md repeats nothing already sent', todo.startsWith('# 3 comment(s) to handle') &&
    todo.includes('Already sent and not repeated here: batch-1.md, ask-2.md, ask-3.md.'));
  expect('a plain reply reaches TODO.md with its thread',
    todo.includes('> do what you suggested') && todo.includes(`in reply to **${askId}**`) &&
    todo.includes('> Same answer there.'));
  expect('a suggestion taken up reaches TODO.md with its origin',
    todo.includes('(taken from a Claude finding, severity major)') && todo.includes('anchor: `B`'));
  expect('a sent comment stays sent after a reload', (await page.locator('.badge-outline', { hasText: /^sent$/ }).count()) === 5);
  expect('done sentinel written', fs.existsSync(path.join(out, 'done')));
  expect('server.json removed on clean shutdown', !fs.existsSync(path.join(out, 'server.json')));
  expect('no JS error', errors.length === 0);
  if (errors.length) console.log(errors);

  await browser.close();
  console.log(failures.length ? `page: ${failures.length} failure(s)` : 'page: OK');
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
