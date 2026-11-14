import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, readFileSync, writeFileSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { JSDOM } from 'jsdom';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';
import { renderDoc, trackedTaskCheckbox } from '../src/markdown.ts';
import { initializeTaskRegister, readTaskRegister, writeTaskCommand } from '../src/task-register.ts';
import { documentTaskSnapshot } from '../src/task-document.ts';
import { createTaskSidebar } from '../src/web/task-sidebar.ts';
import type { TaskCommand, TaskSnapshot } from '../src/task-types.ts';

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'task-workflow-'));
  const path = join(root, 'tasks.json');
  const doc = join(root, 'today.md');
  const markdown = '# Tasks\n\n<!-- task-register: tasks.json -->\n\n- [ ] Capacity review <!-- task-id: TASK-042 -->\n';
  writeFileSync(doc, markdown);
  initializeTaskRegister(path);
  const update = (command: Omit<TaskCommand, 'expectedRevision'>, actor: 'agent' | 'user' = 'agent') =>
    writeTaskCommand(path, { ...command, expectedRevision: readTaskRegister(path).revision }, actor);
  update({ action: 'create', id: 'TASK-042', task: { title: 'Capacity review', source: 'https://example.test/42', owner: 'Owner' } });
  update({ action: 'classify', id: 'TASK-042', task: {
    route: 'agent_preparable', classificationConfidence: 99, readinessConfidence: 94,
    scope: 'Prepare a local report', permissions: ['read_sources', 'write_artifacts', 'update_task_status'],
    acceptance: 'Evidence-backed report', stopCondition: 'User review', dependencies: [],
    humanDelegable: true, candidateDelegate: 'Service owner', handoff: 'proposed',
  } });
  return { root, path, doc, markdown, update };
}

test('register gates approval, readiness, revision, scope, waiting and final acceptance', () => {
  const { path, update } = fixture();
  assert.throws(() => update({ action: 'approve', id: 'TASK-042' }), /user action/);
  update({ action: 'classify', id: 'TASK-042', task: { readinessConfidence: 90 } });
  assert.throws(() => update({ action: 'approve', id: 'TASK-042' }, 'user'), /strictly greater/);
  update({ action: 'classify', id: 'TASK-042', task: { readinessConfidence: 94 } });
  update({ action: 'approve', id: 'TASK-042' }, 'user');
  assert.throws(() => writeTaskCommand(path, { action: 'start', id: 'TASK-042', expectedRevision: 0 }, 'agent'), /stale/);
  update({ action: 'classify', id: 'TASK-042', task: { scope: 'Changed scope' } });
  assert.throws(() => update({ action: 'start', id: 'TASK-042', task: { session: 'session-1' } }), /not ready/);
  update({ action: 'approve', id: 'TASK-042' }, 'user');
  update({ action: 'start', id: 'TASK-042', task: { session: 'session-1' } });
  update({ action: 'needs_input', id: 'TASK-042', note: 'Which region?' });
  assert.throws(() => update({ action: 'resume', id: 'TASK-042', note: 'EU' }), /user action/);
  update({ action: 'resume', id: 'TASK-042', note: 'EU' }, 'user');
  update({ action: 'start', id: 'TASK-042', task: { session: 'session-1' } });
  assert.throws(() => update({ action: 'submit', id: 'TASK-042', note: 'Done' }), /verification evidence/);
  update({ action: 'submit', id: 'TASK-042', note: 'Prepared', task: { deliverable: 'report.md', evidence: 'Reviewed source' } });
  assert.throws(() => update({ action: 'accept', id: 'TASK-042', note: 'Reviewed report' }), /user action/);
  update({ action: 'accept', id: 'TASK-042', note: 'Report satisfies the scoped outcome' }, 'user');
  assert.equal(readTaskRegister(path).tasks[0]!.state, 'completed');
  assert.throws(() => update({ action: 'classify', id: 'TASK-042' }), /terminal/);
});

test('document binding survives carryover, ignores code, and rejects missing/duplicate/outside registers', () => {
  const { root, doc, markdown, path } = fixture();
  assert.match(renderDoc(markdown).html, /data-task-id="TASK-042"/);
  const tomorrow = join(root, 'tomorrow.md');
  writeFileSync(tomorrow, markdown.replace('# Tasks', '# Tomorrow').replace('Capacity review', 'Reworded capacity review'));
  assert.deepEqual(documentTaskSnapshot(doc, root), documentTaskSnapshot(tomorrow, root));
  writeFileSync(doc, markdown + '\n```markdown\n- [ ] Fake <!-- task-id: FAKE -->\n```\n');
  assert.equal(documentTaskSnapshot(doc, root).tasks.length, 1);
  writeFileSync(doc, markdown + '\n- [ ] Duplicate <!-- task-id: TASK-042 -->\n');
  assert.match(documentTaskSnapshot(doc, root).error!, /duplicate/);
  writeFileSync(doc, markdown.replace('TASK-042', 'MISSING'));
  assert.match(documentTaskSnapshot(doc, root).error!, /missing/);
  const elsewhere = mkdtempSync(join(tmpdir(), 'outside-tasks-'));
  symlinkSync(path, join(elsewhere, 'alias.json'));
  writeFileSync(join(elsewhere, 'outside.md'), '<!-- task-register: alias.json -->\n');
  assert.match(documentTaskSnapshot(join(elsewhere, 'outside.md'), elsewhere).error!, /outside project/);
});

test('task ID comments do not activate tracking without document opt-in', () => {
  const markdown = '# Ordinary checklist\n\n- [ ] Editable <!-- task-id: TASK-042 -->\n';
  const rendered = renderDoc(markdown);
  assert.doesNotMatch(rendered.html, /data-task-id/);
  assert.equal(trackedTaskCheckbox(markdown, rendered.blockIds.at(-1)!, 0), false);
});

test('actual HTTP API approves scoped work, rejects stale commands and checkbox bypass, and streams CLI progress', async () => {
  const { root, path, doc, update } = fixture();
  const server = await createServer({
    docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'),
    shutdownOnFinish: false, agentFactory: mockAgentFactory({ reply: 'unused', conclusion: 'unused' }),
  });
  const base = `http://127.0.0.1:${server.port}`;
  const abort = new AbortController();
  try {
    const boot = await (await fetch(`${base}/api/bootstrap`)).json() as { tasks: TaskSnapshot; blockIds: string[] };
    assert.equal(boot.tasks.tasks[0]!.state, 'awaiting_approval');
    const post = (body: object) => fetch(`${base}/api/tasks/command`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal((await post({ action: 'approve', id: 'TASK-042', expectedRevision: 0 })).status, 409);
    const approved = await post({ action: 'approve', id: 'TASK-042', expectedRevision: boot.tasks.revision });
    assert.equal(approved.status, 200);
    assert.equal(readTaskRegister(path).tasks[0]!.state, 'ready');
    const blockedCheckbox = await fetch(`${base}/api/task-checkboxes`, {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ blockId: boot.blockIds.at(-1), checkboxIndex: 0, checked: true }),
    });
    assert.equal(blockedCheckbox.status, 409);
    assert.match(readFileSync(doc, 'utf8'), /- \[ \]/);
    const stream = await fetch(`${base}/events?path=${encodeURIComponent(doc)}`, { signal: abort.signal });
    const reader = stream.body!.getReader();
    const initial = new TextDecoder().decode((await reader.read()).value);
    assert.match(initial, /tasks.updated/);
    update({ action: 'start', id: 'TASK-042', task: { session: 'session-1' } });
    update({ action: 'progress', id: 'TASK-042', note: 'Measured demand' });
    const next = new TextDecoder().decode((await reader.read()).value);
    assert.match(next, /Measured demand/);
    assert.match(next, /in_progress/);
  } finally {
    abort.abort();
    await server.close();
  }
});

test('sidebar filters, navigates and retains input across live updates without replacing the document', async () => {
  const { root, doc, markdown } = fixture();
  const dom = new JSDOM(`<main id="doc">${renderDoc(markdown).html}</main>`);
  const target = dom.window.document.getElementById('task-TASK-042')!;
  let jumped = false;
  target.scrollIntoView = () => { jumped = true; };
  const sidebar = createTaskSidebar(dom.window.document, async () => { throw new Error('not invoked'); });
  const snapshot = documentTaskSnapshot(doc, root);
  sidebar.update(snapshot);
  const host = dom.window.document.getElementById('task-sidebar')!;
  assert.match(host.textContent!, /Capacity review/);
  const note = host.querySelector('textarea')!;
  note.value = 'Keep my draft';
  note.dispatchEvent(new dom.window.Event('input'));
  sidebar.update({ ...snapshot, revision: snapshot.revision + 1 });
  assert.equal(host.querySelector('textarea')!.value, 'Keep my draft');
  assert.equal(dom.window.document.getElementById('task-TASK-042'), target);
  host.querySelector('a')!.click();
  assert.equal(jumped, true);
  const filter = host.querySelector('select')!;
  filter.value = 'completed';
  filter.dispatchEvent(new dom.window.Event('change'));
  assert.equal(host.querySelectorAll('.task-sidebar-item').length, 0);
  dom.window.close();
});
