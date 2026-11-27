import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from '../dist/server.js';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const runtime = dirname(dirname(fileURLToPath(import.meta.url)));
const root = mkdtempSync(join(tmpdir(), 'task-live-updates-'));
const register = join(root, 'tasks.json');
const doc = join(root, 'today.md');
const cli = join(runtime, 'dist/task-control.js');
const read = () => JSON.parse(readFileSync(register, 'utf8'));
function update(command) {
  execFileSync(process.execPath, [cli, 'update', register, '-'], {
    input: JSON.stringify({ ...command, expectedRevision: read().revision }),
  });
}
execFileSync(process.execPath, [cli, 'init', register]);
for (let i = 0; i < 18; i++) {
  update({ action: 'create', id: `TASK-${i}`, task: { title: `Review ${i}`, source: 'Local acceptance fixture' } });
  update({ action: 'classify', id: `TASK-${i}`, task: {
    route: 'agent_preparable', scope: 'Prepare local review', permissions: ['read_sources', 'write_artifacts', 'update_task_status'],
    acceptance: 'Source-linked report', stopCondition: 'Stop for user review',
  } });
}
const markdown = '# Live tasks\n\n<!-- task-register: tasks.json -->\n\n## Reviews\n\n' +
  Array.from({ length: 18 }, (_, i) => `- [ ] Review ${i} <!-- task-id: TASK-${i} -->\n\nParagraph ${i}.\n`).join('\n');
writeFileSync(doc, markdown);
const server = await createServer({
  docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs'),
  staticDir: join(runtime, 'dist'), shutdownOnFinish: false,
  agentFactory: () => { throw new Error('No live agents in acceptance'); },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 650 } });
  await page.goto(`http://127.0.0.1:${server.port}/#task-TASK-0`);
  await page.locator('#task-sidebar-toggle').click();
  await page.getByLabel('Quick state for TASK-12', { exact: true }).selectOption('approve_with_note');
  const note = page.getByLabel('Quick action evidence for TASK-12', { exact: true });
  await note.fill('Keep my typing and selection');
  await note.evaluate((el) => { el.setSelectionRange(5, 14); window.savedNote = el; });
  const before = await page.evaluate(() => ({ y: scrollY, x: document.querySelector('.task-table-scroll').scrollLeft }));
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 21 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 21%'));
  assert.equal(await note.evaluate((el) => el === window.savedNote && el === document.activeElement), true, 'unrelated updates retain the focused DOM node');
  assert.deepEqual(await note.evaluate((el) => [el.selectionStart, el.selectionEnd]), [5, 14]);
  assert.ok(Math.abs((await page.evaluate(() => scrollY)) - before.y) < 2, 'task update retains viewport');
  await page.keyboard.type('replacement');
  assert.equal(await note.inputValue(), 'Keep replacement and selection');

  // Even the edited task can change without replacing an active control.
  update({ action: 'classify', id: 'TASK-12', task: { readinessConfidence: 22 } });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 23 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 23%'));
  assert.equal(await note.evaluate((el) => el === window.savedNote && el === document.activeElement), true);
  await page.locator('#task-sidebar h2').click();
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-12').closest('tr').textContent.includes('/ 22%'));
  assert.equal(await note.inputValue(), 'Keep replacement and selection');

  // Expanded state actions and open details survive updates too.
  await page.locator('#task-toggle-TASK-12').click();
  const transition = page.getByLabel('Change state for TASK-12', { exact: true });
  await transition.selectOption('approve');
  await transition.evaluate((el) => { window.savedSelect = el; });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 24 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 24%'));
  assert.equal(await transition.evaluate((el) => el === window.savedSelect), true);
  assert.equal(await transition.inputValue(), 'approve');

  // Document rewrites must neither rebuild the table nor revisit its URL hash.
  await checkDocumentUpdates(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.task-table-scroll').evaluate((el) => { el.scrollLeft = 260; });
  const horizontal = await page.locator('.task-table-scroll').evaluate((el) => el.scrollLeft);
  assert.ok(horizontal > 0, 'mobile fixture has horizontal table overflow');
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 25 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 25%'));
  assert.equal(await page.locator('.task-table-scroll').evaluate((el) => el.scrollLeft), horizontal);
  await page.setViewportSize({ width: 1100, height: 650 });

  // A retained control must not approve a changed scope using a newer revision.
  await page.locator('#task-toggle-TASK-11').click();
  const staleState = page.getByLabel('Change state for TASK-11', { exact: true });
  await staleState.focus();
  update({ action: 'classify', id: 'TASK-11', task: { scope: 'New scope that was not displayed' } });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 26 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 26%'));
  await staleState.selectOption('approve');
  await page.locator('#task-details-TASK-11').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.locator('#modal-root').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-details-TASK-11 .task-action-error')?.textContent.includes('revision'));
  assert.equal(read().tasks.find((task) => task.id === 'TASK-11').approval, undefined);

  const title = page.locator('#task-toggle-TASK-9');
  await title.scrollIntoViewIfNeeded();
  await title.evaluate((el) => {
    window.selectedTitle = el;
    const range = document.createRange();
    range.selectNodeContents(el);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  });
  update({ action: 'classify', id: 'TASK-9', task: { readinessConfidence: 27 } });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 27 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 27%'));
  assert.equal(await title.evaluate((el) => el === window.selectedTitle && getSelection().toString() === 'Review 9'), true);
  await page.evaluate(() => getSelection().removeAllRanges());
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-9').closest('tr').textContent.includes('/ 27%'));

  const categoryCell = title.locator('xpath=ancestor::tr').locator('td').nth(2);
  const bounds = await categoryCell.boundingBox();
  await page.mouse.move(bounds.x + 10, bounds.y + 10);
  await page.mouse.down();
  await categoryCell.evaluate((el) => { window.pressedCell = el; });
  update({ action: 'classify', id: 'TASK-9', task: { readinessConfidence: 28 } });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 28 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 28%'));
  assert.equal(await categoryCell.evaluate((el) => el === window.pressedCell), true, 'pointer-down target remains connected');
  await page.mouse.up();
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-9').closest('tr').textContent.includes('/ 28%'));

  // Even a row leaving the selected filter waits until editing ends.
  await page.getByLabel('Filter tasks by status').selectOption('awaiting_approval');
  await page.getByLabel('Quick state for TASK-10', { exact: true }).selectOption('approve_with_note');
  const removedNote = page.getByLabel('Quick action evidence for TASK-10', { exact: true });
  await removedNote.fill('Keep until blur');
  await removedNote.evaluate((el) => { window.removedNote = el; });
  update({ action: 'classify', id: 'TASK-10', task: { route: 'you' } });
  update({ action: 'classify', id: 'TASK-0', task: { readinessConfidence: 29 } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-0').closest('tr').textContent.includes('/ 29%'));
  assert.equal(await removedNote.evaluate((el) => el === window.removedNote && el === document.activeElement), true);
  await page.locator('#task-sidebar h2').click();
  await page.locator('#task-toggle-TASK-10').waitFor({ state: 'detached' });
  await page.screenshot({ path: join(root, 'tasks-stable.png') });
  console.log(`PASS: live updates preserve focused controls, caret, drafts, expanded actions and viewport; ${root}`);

  async function checkDocumentUpdates(page) {
    await transition.focus();
    const position = await page.evaluate(() => scrollY);
    writeFileSync(doc, `${markdown}\nUpdated evidence.\n`);
    await page.waitForFunction(() => document.querySelector('#doc').textContent.includes('Updated evidence.'));
    assert.equal(await transition.evaluate((el) => el === window.savedSelect && el === document.activeElement), true);
    assert.ok(Math.abs((await page.evaluate(() => scrollY)) - position) < 2, 'document rewrite does not jump to URL anchor in Tasks mode');
    await page.locator('#task-sidebar-toggle').click();
    await page.locator('#task-TASK-14').scrollIntoViewIfNeeded();
    const documentPosition = await page.evaluate(() => scrollY);
    writeFileSync(doc, `${markdown}\nUpdated evidence again.\n`);
    await page.waitForFunction(() => document.querySelector('#doc').textContent.includes('Updated evidence again.'));
    assert.ok(Math.abs((await page.evaluate(() => scrollY)) - documentPosition) < 2, 'document rewrite does not revisit old URL anchor');
    await page.locator('#task-sidebar-toggle').click();
  }
} finally {
  await browser.close();
  await server.close();
}
