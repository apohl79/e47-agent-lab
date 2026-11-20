import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from '../dist/server.js';

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const runtime = dirname(dirname(fileURLToPath(import.meta.url)));
const root = mkdtempSync(join(tmpdir(), 'task-dispatch-browser-'));
const register = join(root, 'tasks.json');
const doc = join(root, 'today.md');
const cli = join(runtime, 'dist/task-control.js');
const read = () => JSON.parse(execFileSync(process.execPath, [cli, 'read', register], { encoding: 'utf8' }));
const command = (value) => JSON.parse(execFileSync(process.execPath, [cli, 'update', register, '-'], {
  input: JSON.stringify({ ...value, expectedRevision: read().revision }), encoding: 'utf8',
}));
execFileSync(process.execPath, [cli, 'init', register]);
command({ action: 'create', id: 'TASK-1', task: { title: 'Prepare local review', source: 'Isolated acceptance fixture' } });
command({ action: 'classify', id: 'TASK-1', task: {
  route: 'agent_preparable', classificationConfidence: 99, readinessConfidence: 95,
  scope: 'Prepare a local review', permissions: ['read_sources', 'write_artifacts'],
  acceptance: 'Local review artifact exists', stopCondition: 'Stop for final user review',
} });
writeFileSync(doc, '# Acceptance\n\n<!-- task-register: tasks.json -->\n\n- [ ] Prepare local review <!-- task-id: TASK-1 -->\n');
let notifications = 0;
// Exercise the real UI/API/CLI boundary without authorizing a live model worker.
// The host fixture records one isolated worker's lifecycle using the built CLI.
const server = await createServer({
  docPath: doc, projectRoot: root, sessionDir: join(root, 'session'),
  prefsPath: join(root, 'prefs'), staticDir: join(runtime, 'dist'),
  agentFactory: () => { throw new Error('No live agents in acceptance'); },
  mainSession: { async send(prompt) {
    notifications++;
    const event = JSON.parse(prompt.split('\n').at(-1));
    assert.equal(event.type, 'inline-discussion.task-ready');
    command({ action: 'claim', id: event.taskId, expectedTaskRevision: event.taskRevision, claimId: 'browser-claim' });
    assert.throws(() => command({ action: 'claim', id: event.taskId, expectedTaskRevision: read().tasks[0].revision, claimId: 'duplicate-claim' }));
    command({ action: 'bind', id: event.taskId, claimId: 'browser-claim', task: { session: 'isolated-worker-fixture' } });
    command({ action: 'start', id: event.taskId, claimId: 'browser-claim', task: { session: 'isolated-worker-fixture' } });
    writeFileSync(join(root, 'review.md'), 'Review fixture: final user acceptance required.\n');
    command({ action: 'submit', id: event.taskId, note: 'Prepared isolated review', task: { deliverable: join(root, 'review.md'), evidence: 'Read the local fixture' } });
  } },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`http://127.0.0.1:${server.port}/`);
  await page.locator('#task-sidebar-toggle').click();
  await page.getByLabel('Quick state for TASK-1', { exact: true }).selectOption('approve');
  await page.locator('#task-quick-apply-TASK-1').click();
  await page.locator('#modal-root').getByText(/Approval notifies the owning main session/).waitFor();
  assert.equal(notifications, 0, 'Opening confirmation cannot dispatch');
  await page.locator('#modal-root').getByRole('button', { name: 'Approve preparation', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-quick-select-TASK-1 option')?.textContent === 'ready for review');
  const task = read().tasks[0];
  assert.equal(notifications, 1);
  assert.equal(task.session, 'isolated-worker-fixture');
  assert.equal(task.dispatch.id, 'browser-claim');
  assert.equal(task.state, 'ready_for_review');
  assert.equal(task.history.filter((entry) => entry.note === 'claim').length, 1);
  assert.match(readFileSync(join(root, 'review.md'), 'utf8'), /user acceptance required/);
  assert.equal(await page.locator('#task-quick-select-TASK-1 option[value="approve"]').count(), 0);
  console.log('PASS: built browser approval → main-session notification → CLI claim/bind/start → review-ready artifact; duplicate claim rejected; no final acceptance or live agent.');
} finally {
  await browser.close();
  await server.close();
}
