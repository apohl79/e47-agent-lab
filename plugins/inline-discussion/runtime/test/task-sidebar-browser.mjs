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
writeFileSync(doc, '# Today\n\n<!-- task-register: tasks.json -->\n\n- [ ] Capacity review <!-- task-id: TASK-042 -->\n\nDiscuss this paragraph.\n');
const server = await createServer({
  docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'),
  staticDir: join(runtime, 'dist'), shutdownOnFinish: false,
  agentFactory: () => { throw new Error('Acceptance must not start agents'); },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`http://127.0.0.1:${server.port}/`);
  await page.locator('#task-sidebar').waitFor({ state: 'visible' });
  assert.match(await page.locator('#task-sidebar').innerText(), /Capacity review/);
  await page.locator('.task-sidebar-item > details > summary').click();
  await page.getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.locator('#modal-root').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'ready');
  assert.equal(read().tasks[0].state, 'ready');
  update({ action: 'start', id: 'TASK-042', task: { session: 'acceptance-fixture-only' } });
  update({ action: 'needs_input', id: 'TASK-042', note: 'Which region?' });
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'needs input');
  await page.getByLabel('Answer or review evidence for TASK-042').fill('EU');
  await page.getByRole('button', { name: 'Answer / resume' }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'ready');
  update({ action: 'start', id: 'TASK-042', task: { session: 'acceptance-fixture-only' } });
  update({ action: 'submit', id: 'TASK-042', note: 'Report ready', task: { deliverable: 'report.md', evidence: 'Checked demand measurements' } });
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'ready for review');
  await page.getByLabel('Answer or review evidence for TASK-042').fill('Reviewed report; scoped recommendation is complete');
  await page.getByRole('button', { name: 'Accept outcome' }).click();
  await page.getByLabel('Filter tasks by status').selectOption('completed');
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'completed');
  assert.equal(read().tasks[0].state, 'completed');
  assert.equal(await page.locator('#task-TASK-042 input').isChecked(), true);
  await page.screenshot({ path: join(root, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator('#task-sidebar').isVisible(), false);
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  assert.equal(await page.locator('#task-sidebar').isVisible(), true);
  await page.screenshot({ path: join(root, 'mobile.png'), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
    JSON.stringify(await page.evaluate(() => Array.from(document.querySelectorAll('body *')).filter((element) => element.getBoundingClientRect().right > innerWidth).map((element) => [element.tagName, element.id, element.className]).slice(0, 12))));
  console.log(`PASS built browser approval → waiting → resume → review → completed; desktop/mobile: ${root}`);
} finally {
  await browser.close();
  await server.close();
}
