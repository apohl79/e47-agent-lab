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
  route: 'agent_preparable', classificationConfidence: 99, readinessConfidence: 30,
  scope: 'Prepare a local review', permissions: ['read_sources', 'write_artifacts'],
  acceptance: 'Local review artifact exists', stopCondition: 'Stop for final user review',
} });
writeFileSync(doc, '# Acceptance\n\n<!-- task-register: tasks.json -->\n\n- [ ] Prepare local review <!-- task-id: TASK-1 -->\n');
let notifications = 0;
const approvalNote = 'I started a DevEx conversation. Prepare a follow-up for review.';
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
    assert.match(prompt, /approvalNote.*verbatim/);
    assert.equal(read().tasks[0].approvalNote, approvalNote);
    command({ action: 'claim', id: event.taskId, expectedTaskRevision: event.taskRevision, claimId: 'browser-claim' });
    assert.throws(() => command({ action: 'claim', id: event.taskId, expectedTaskRevision: read().tasks[0].revision, claimId: 'duplicate-claim' }));
    command({ action: 'bind', id: event.taskId, claimId: 'browser-claim', task: { session: 'isolated-worker-fixture' } });
    command({ action: 'start', id: event.taskId, claimId: 'browser-claim', task: { session: 'isolated-worker-fixture' } });
    writeFileSync(join(root, 'review.md'), `Review fixture: final user acceptance required.\nUser note: ${read().tasks[0].approvalNote}\n`);
    command({ action: 'submit', id: event.taskId, note: 'Prepared isolated review', task: { deliverable: join(root, 'review.md'), evidence: 'Read the local fixture' } });
  } },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`http://127.0.0.1:${server.port}/`);
  await page.locator('#task-sidebar-toggle').click();
  const quick = page.getByLabel('Quick state for TASK-1', { exact: true });
  assert.equal(await quick.locator('option[value="approve"]').isDisabled(), false);
  await quick.selectOption('approve_with_note');
  await page.getByLabel('Quick action evidence for TASK-1', { exact: true }).fill(approvalNote);
  await page.locator('#task-quick-apply-TASK-1').click();
  await page.locator('#modal-root').getByText(/Approval notifies the owning main session/).waitFor();
  await page.locator('#modal-root').getByText(/I started a DevEx conversation/).waitFor();
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
  assert.ok(readFileSync(join(root, 'review.md'), 'utf8').includes(approvalNote));
  await page.getByRole('button', { name: 'Prepare local review', exact: true }).click();
  await page.getByText('User note to worker:', { exact: true }).waitFor();
  await page.getByLabel('Change state for TASK-1', { exact: true }).selectOption('request_changes');
  await page.getByRole('button', { name: 'Request changes', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'awaiting approval');
  assert.equal(read().tasks[0].approvalNote, undefined);
  await page.getByLabel('Change state for TASK-1', { exact: true }).selectOption('approve_with_note');
  assert.equal(await page.getByRole('button', { name: 'Ready with note', exact: true }).isEnabled(), true);
  await page.getByRole('button', { name: 'Ready with note', exact: true }).click();
  await page.locator('#modal-root').getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.equal(notifications, 1, 'cancelled expanded approval cannot dispatch');
  await page.getByLabel('Filter tasks by status').selectOption('all');
  await page.getByLabel('Change state for TASK-1', { exact: true }).selectOption('accept');
  await page.getByRole('button', { name: 'Mark outcome complete', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.task-status')?.textContent === 'completed');
  assert.equal(read().tasks[0].state, 'completed', 'explicit human completion needs no reason');
  assert.equal(await page.locator('#task-quick-select-TASK-1 option[value="approve"]').count(), 0);
  console.log('PASS: low-confidence ready with note → main notification → CLI claim/bind/start → note in artifact; expanded cancellation; blank review/completion reasons; no live agent.');
} finally {
  await browser.close();
  await server.close();
}
