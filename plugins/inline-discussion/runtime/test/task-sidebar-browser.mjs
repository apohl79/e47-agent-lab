import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from '../dist/server.js';
import { execFileSync } from 'node:child_process';

// Run against built assets. Point PLAYWRIGHT_MODULE at an existing Playwright
// installation; this acceptance check does not install browsers or packages.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const runtime = dirname(dirname(fileURLToPath(import.meta.url)));
const root = mkdtempSync(join(tmpdir(), 'task-browser-'));
const register = join(root, 'tasks.json');
const doc = join(root, 'today.md');
const cli = join(runtime, 'dist/task-control.js');
function read() { return JSON.parse(readFileSync(register, 'utf8')); }
function update(command) {
  execFileSync(process.execPath, [cli, 'update', register, '-'], {
    input: JSON.stringify({ ...command, expectedRevision: read().revision }),
  });
}
execFileSync(process.execPath, [cli, 'init', register]);
update({ action: 'create', id: 'TASK-042', task: { title: 'Capacity review', source: 'https://example.test/42', owner: 'Owner' } });
update({ action: 'classify', id: 'TASK-042', task: {
  route: 'agent_preparable', classificationConfidence: 96, readinessConfidence: 95,
  scope: 'Prepare a local capacity report', permissions: ['read_sources', 'write_artifacts', 'update_task_status'],
  acceptance: 'Measured demand and headroom', stopCondition: 'Final user review',
} });
update({ action: 'create', id: 'TASK-043', task: { title: 'Second task', source: 'https://example.test/43' } });
writeFileSync(doc, '# Today\n\n<!-- task-register: tasks.json -->\n\n- [ ] Capacity review <!-- task-id: TASK-042 -->\n- [ ] Second task <!-- task-id: TASK-043 -->\n\nDiscuss this paragraph.\n');
const server = await createServer({
  docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'),
  staticDir: join(runtime, 'dist'), shutdownOnFinish: false,
  agentFactory: () => { throw new Error('Acceptance must not start agents'); },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`http://127.0.0.1:${server.port}/`);
  assert.equal(await page.locator('#task-sidebar').isVisible(), false);
  const initialRevision = read().revision;
  await page.locator('#task-TASK-042 input').click();
  await page.locator('#task-sidebar').waitFor({ state: 'visible' });
  assert.equal(read().revision, initialRevision, 'checkbox navigation does not mutate or complete');
  assert.equal(await page.locator('#task-TASK-042 input').isChecked(), false);
  assert.match(await page.locator('#task-sidebar').innerText(), /Capacity review/);
  const taskToggle = page.getByRole('button', { name: 'Capacity review', exact: true });
  assert.equal(await taskToggle.getAttribute('aria-expanded'), 'true');
  assert.equal(await page.locator('#task-details-TASK-042 > td').getAttribute('colspan'), '6');
  assert.equal(await page.locator('#task-details-TASK-042').evaluate((row) => row.previousElementSibling.querySelector('button').textContent), 'Capacity review');
  const evidence = page.getByLabel('Answer or review evidence for TASK-042');
  await evidence.fill('Keep this draft while collapsed');
  await taskToggle.click();
  assert.equal(await page.locator('.task-detail-row').count(), 0);
  assert.equal(await taskToggle.getAttribute('aria-expanded'), 'false');
  await page.keyboard.press('Space');
  assert.equal(await evidence.inputValue(), 'Keep this draft while collapsed');
  await page.getByRole('button', { name: 'Second task', exact: true }).click();
  assert.equal(await page.locator('.task-detail-row').count(), 1);
  assert.equal(await page.locator('#task-details-TASK-043').isVisible(), true);
  await taskToggle.click();
  assert.equal(await evidence.inputValue(), 'Keep this draft while collapsed');
  await evidence.fill('');
  await taskToggle.click();
  // Collapsed rows expose guarded status changes without expanding details.
  const secondQuick = page.getByLabel('Quick state for TASK-043', { exact: true });
  await secondQuick.selectOption('accept');
  assert.equal(await page.locator('#task-details-TASK-043').count(), 0);
  assert.equal(await page.locator('.task-detail-row').count(), 0);
  await page.screenshot({ path: join(root, 'quick-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: join(root, 'quick-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const secondRow = page.locator('tr').filter({ has: page.locator('#task-toggle-TASK-043') });
  await secondRow.getByRole('button', { name: 'Mark outcome complete', exact: true }).click();
  assert.match(await page.getByRole('alert').innerText(), /evidence is required/);
  assert.equal(read().tasks[1].state, 'identified');
  await page.getByLabel('Quick action evidence for TASK-043', { exact: true }).fill('Outcome verified in local acceptance fixture');
  await page.getByRole('button', { name: 'Second task', exact: true }).click();
  assert.equal(await page.getByLabel('Quick action evidence for TASK-043', { exact: true }).count(), 0);
  assert.equal(await page.getByLabel('Answer or review evidence for TASK-043').inputValue(), 'Outcome verified in local acceptance fixture');
  await secondQuick.selectOption('accept');
  assert.equal(await page.locator('.task-detail-row').count(), 0);
  update({ action: 'classify', id: 'TASK-043', task: { route: 'you' } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-043')?.closest('tr')?.textContent.includes('Personal'));
  await secondRow.getByRole('button', { name: 'Mark outcome complete', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role="alert"]')?.textContent.includes('revision'));
  assert.equal(read().tasks[1].state, 'identified', 'quick actions must reject stale revisions');
  await secondRow.getByRole('button', { name: 'Cancel quick action', exact: true }).click();
  await secondQuick.selectOption('accept');
  assert.equal(await page.getByLabel('Quick action evidence for TASK-043', { exact: true }).inputValue(), 'Outcome verified in local acceptance fixture');
  await secondRow.getByRole('button', { name: 'Mark outcome complete', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-quick-select-TASK-043')?.disabled);
  assert.equal(read().tasks[1].state, 'completed');
  assert.equal(await secondQuick.isDisabled(), true);
  await taskToggle.click();
  const transition = page.getByLabel('Change state for TASK-042');
  const applyState = async (action, name) => {
    await transition.selectOption(action);
    await page.getByRole('button', { name, exact: true }).click();
  };
  await applyState('accept', 'Mark outcome complete');
  assert.match(await page.getByRole('alert').innerText(), /evidence is required/);
  assert.equal(read().tasks[0].state, 'awaiting_approval');
  const quick = page.getByLabel('Quick state for TASK-042', { exact: true });
  await quick.selectOption('approve');
  assert.equal(await page.locator('.task-detail-row').count(), 0);
  await page.getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.locator('#modal-root').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(read().tasks[0].state, 'awaiting_approval');
  await page.getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.locator('#modal-root').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-quick-select-TASK-042 option')?.textContent === 'ready');
  assert.equal(read().tasks[0].state, 'ready');
  await taskToggle.click();
  await page.getByText('Edit classification', { exact: true }).click();
  await page.getByLabel('Classification', { exact: true }).selectOption('you');
  await page.getByLabel('Human-delegable independently of classification').check();
  await page.getByLabel('Classification confidence (%)', { exact: true }).fill('98');
  await page.getByLabel('Readiness confidence (%)', { exact: true }).fill('91');
  await page.getByLabel('Classification reason', { exact: true }).fill('Personal decision; a colleague can prepare context');
  await page.getByRole('button', { name: 'Save classification', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'identified');
  assert.equal(read().tasks[0].approval, undefined, 'reclassification clears prior approval');
  assert.equal(read().tasks[0].humanDelegable, true);
  assert.equal(read().tasks[0].classificationConfidence, 98);
  await page.reload();
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.getByRole('button', { name: 'Capacity review', exact: true }).click();
  assert.match(await page.locator('.task-table tbody').innerText(), /Personal/);
  await page.getByText('Edit classification', { exact: true }).click();
  assert.equal(await page.getByLabel('Classification', { exact: true }).inputValue(), 'you');
  // A live register update must not silently rebase an edited classification.
  await page.getByLabel('Classification reason', { exact: true }).fill('Stale draft');
  update({ action: 'classify', id: 'TASK-042', task: { route: 'verify_first' } });
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-042')?.closest('tr')?.textContent.includes('Verify first'));
  await page.getByRole('button', { name: 'Save classification', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role="alert"]')?.textContent.includes('revision'));
  assert.equal(read().tasks[0].route, 'verify_first');
  assert.equal(await page.getByLabel('Classification reason', { exact: true }).inputValue(), 'Stale draft');
  await page.getByRole('button', { name: 'Discard classification edits', exact: true }).click();
  await page.getByText('Edit classification', { exact: true }).click();
  await page.getByLabel('Classification', { exact: true }).selectOption('agent_preparable');
  await page.getByLabel('Human-delegable independently of classification').uncheck();
  await page.getByLabel('Classification reason', { exact: true }).fill('Local preparation is safe and ready');
  await page.getByRole('button', { name: 'Save classification', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-toggle-TASK-042')?.closest('tr')?.textContent.includes('Agent-preparable'));
  assert.equal(read().tasks[0].humanDelegable, false);
  await applyState('approve', 'Approve preparation');
  await page.locator('#modal-root').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'ready');
  update({ action: 'start', id: 'TASK-042', task: { session: 'acceptance-fixture-only' } });
  update({ action: 'needs_input', id: 'TASK-042', note: 'Which region?' });
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'needs input');
  await quick.selectOption('resume');
  await page.getByLabel('Quick action evidence for TASK-042', { exact: true }).fill('EU');
  await page.getByRole('button', { name: 'Answer / resume', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-quick-select-TASK-042 option')?.textContent === 'ready');
  await taskToggle.click();
  update({ action: 'start', id: 'TASK-042', task: { session: 'acceptance-fixture-only' } });
  update({ action: 'submit', id: 'TASK-042', note: 'Report ready', task: { deliverable: 'report.md', evidence: 'Checked demand measurements' } });
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'ready for review');
  await page.getByLabel('Answer or review evidence for TASK-042').fill('Reviewed report; scoped recommendation is complete');
  await applyState('accept', 'Accept outcome');
  await page.getByLabel('Filter tasks by status').selectOption('completed');
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'completed');
  assert.equal(read().tasks[0].state, 'completed');
  assert.equal(await page.locator('#task-TASK-042 input').isChecked(), true);
  await page.screenshot({ path: join(root, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator('#task-sidebar').isVisible(), true);
  await page.screenshot({ path: join(root, 'mobile.png'), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
    JSON.stringify(await page.evaluate(() => Array.from(document.querySelectorAll('body *')).filter((element) => element.getBoundingClientRect().right > innerWidth).map((element) => [element.tagName, element.id, element.className]).slice(0, 12))));
  await page.getByRole('button', { name: 'Document', exact: true }).click();
  assert.equal(await page.locator('#doc').isVisible(), true);
  await page.locator('#task-TASK-042 input').focus();
  await page.keyboard.press('Space');
  assert.equal(await page.locator('#task-sidebar').isVisible(), true);
  assert.equal(read().tasks[0].state, 'completed', 'keyboard navigation does not reopen completed tasks');
  // Failed register hydration must not fall back to a misleading local toggle.
  writeFileSync(register, '{broken register');
  await page.reload();
  await page.locator('#task-TASK-042 input').click();
  assert.equal(await page.locator('#task-TASK-042 input').isChecked(), false, 'unchecked Markdown snapshot must not toggle on a register error');
  assert.equal(await page.locator('#task-sidebar').isVisible(), true);
  assert.notEqual(await page.getByRole('alert').innerText(), '');
  console.log(`PASS built browser collapsed quick approval/resume/completion, evidence and stale-write guards, expanded workflow and classification; desktop/mobile: ${root}`);
} finally {
  await browser.close();
  await server.close();
}
