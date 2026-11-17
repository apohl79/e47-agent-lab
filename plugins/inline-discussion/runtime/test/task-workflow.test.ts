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

test('route alone controls automation and old delegation flags remain readable', () => {
  const { path, update } = fixture();
  assert.equal('humanDelegable' in readTaskRegister(path).tasks[0]!, false);
  const legacy = JSON.parse(readFileSync(path, 'utf8'));
  legacy.tasks[0].humanDelegable = true;
  writeFileSync(path, JSON.stringify(legacy));
  update({ action: 'approve', id: 'TASK-042' }, 'user');
  update({ action: 'classify', id: 'TASK-042', task: { route: 'human_delegable', candidateDelegate: 'Service owner', handoff: 'proposed' } });
  assert.equal(readTaskRegister(path).tasks[0]!.state, 'identified');
  assert.throws(() => update({ action: 'approve', id: 'TASK-042' }, 'user'), /not awaiting approval/);
  assert.throws(() => update({ action: 'start', id: 'TASK-042', task: { session: 'not-started' } }), /not ready/);
  update({ action: 'classify', id: 'TASK-042', task: { route: 'agent' } });
  update({ action: 'approve', id: 'TASK-042' }, 'user');
  assert.equal(readTaskRegister(path).tasks[0]!.state, 'ready');
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

test('task categories follow document sections, ignore nested headings, and refresh without register writes', () => {
  const { root, doc, path, update } = fixture();
  update({ action: 'create', id: 'TASK-043', task: { title: 'Second task', source: 'https://example.test/43' } });
  const markdown = '# Today\n\n<!-- task-register: tasks.json -->\n\n- [ ] Capacity review <!-- task-id: TASK-042 -->\n\n## Initiatives\n\n### SAP\n\n> ## Not a category\n\n- [ ] Second task <!-- task-id: TASK-043 -->\n\n  ### Internal task heading\n';
  writeFileSync(doc, markdown);
  const dom = new JSDOM(`<body><button id="task-sidebar-toggle"></button><main id="doc">${renderDoc(markdown).html}</main></body>`);
  const document = dom.window.document;
  const sidebar = createTaskSidebar(document, async () => { throw new Error('Categories must not write'); });
  const revision = readTaskRegister(path).revision;
  sidebar.update(documentTaskSnapshot(doc, root));
  const filter = document.querySelector<HTMLSelectElement>('[aria-label="Filter tasks by category"]')!;
  assert.deepEqual(Array.from(filter.options, option => option.textContent), ['All categories', 'Uncategorized', 'Initiatives / SAP']);
  filter.value = 'Initiatives / SAP';
  filter.dispatchEvent(new dom.window.Event('change'));
  assert.equal(document.querySelector('#task-toggle-TASK-042'), null);
  assert.ok(document.querySelector('#task-toggle-TASK-043'));
  document.querySelector('#doc')!.innerHTML = renderDoc(markdown.replace('### SAP', '### CPL')).html;
  sidebar.refresh();
  assert.equal(filter.value, '');
  assert.match(document.querySelector('.task-table')!.textContent!, /Initiatives \/ CPL/);
  assert.equal(readTaskRegister(path).revision, revision);
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
    assert.equal((await post({ action: 'start', id: 'TASK-042', expectedRevision: boot.tasks.revision })).status, 409);
    const approved = await post({ action: 'approve', id: 'TASK-042', expectedRevision: boot.tasks.revision });
    assert.equal(approved.status, 200);
    assert.equal(readTaskRegister(path).tasks[0]!.state, 'ready');
    assert.equal((await post({ action: 'classify', id: 'TASK-042', expectedRevision: boot.tasks.revision + 1, task: { route: 'you' } })).status, 409);
    assert.equal((await post({ action: 'classify', id: 'TASK-042', expectedRevision: boot.tasks.revision + 1, note: 'Reject scope change', task: { scope: 'Different' } })).status, 409);
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
  (host.querySelector('.task-title-button') as HTMLButtonElement).click();
  assert.equal(host.querySelector('.task-title-button')!.getAttribute('aria-expanded'), 'true');
  assert.equal(host.querySelector('.task-sidebar-item')!.closest('tr')!.previousElementSibling!.querySelector('.task-title-button')!.textContent, 'Capacity review');
  assert.equal(host.querySelector<HTMLTableCellElement>('.task-detail-row > td')!.colSpan, 6);
  const note = host.querySelector('textarea')!;
  note.value = 'Keep my draft';
  note.dispatchEvent(new dom.window.Event('input'));
  sidebar.update({ ...snapshot, revision: snapshot.revision + 1 });
  assert.equal(host.querySelector('textarea')!.value, 'Keep my draft');
  (host.querySelector('.task-title-button') as HTMLButtonElement).click();
  assert.equal(host.querySelector('.task-sidebar-item'), null);
  assert.equal(host.querySelector('.task-title-button')!.getAttribute('aria-expanded'), 'false');
  (host.querySelector('.task-title-button') as HTMLButtonElement).click();
  assert.equal(host.querySelector('textarea')!.value, 'Keep my draft');
  assert.equal(dom.window.document.getElementById('task-TASK-042'), target);
  host.querySelector('a')!.click();
  assert.equal(jumped, true);
  const filter = host.querySelector('select')!;
  filter.value = 'completed';
  filter.dispatchEvent(new dom.window.Event('change'));
  assert.equal(host.querySelectorAll('tbody tr').length, 0);
  dom.window.close();
});

test('unavailable tracked tasks open their error without visually toggling completion', () => {
  const dom = new JSDOM('<main id="doc"><li data-task-id="TASK-MISSING"><input type="checkbox"></li></main>');
  const sidebar = createTaskSidebar(dom.window.document, async () => { throw new Error('must not persist'); });
  sidebar.update({ enabled: true, revision: 0, tasks: [], error: 'Register missing' });
  const input = dom.window.document.querySelector('input')!;
  input.click();
  assert.equal(input.checked, false);
  assert.equal(dom.window.document.getElementById('task-sidebar')!.hidden, false);
  assert.equal(dom.window.document.querySelector('[role="alert"]')!.textContent, 'Register missing');
  dom.window.close();
});

test('collapsed quick actions require evidence and retain their revision and draft after a stale write', async () => {
  const { root, doc, markdown } = fixture();
  const dom = new JSDOM(`<main id="doc">${renderDoc(markdown).html}</main>`);
  const calls: Array<{ action: string; revision: number; note: string }> = [];
  const sidebar = createTaskSidebar(dom.window.document, async (action, _id, revision, note) => {
    calls.push({ action, revision, note });
    throw new Error('stale register revision');
  });
  const snapshot = documentTaskSnapshot(doc, root);
  sidebar.update(snapshot);
  const host = dom.window.document.getElementById('task-sidebar')!;
  const select = host.querySelector<HTMLSelectElement>('[aria-label="Quick state for TASK-042"]')!;
  assert.equal(Array.from(select.options).some((option) => option.value === 'start'), false);
  select.value = 'accept';
  select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(host.querySelector('.task-detail-row'), null);
  const apply = () => Array.from(host.querySelectorAll('button')).find((button) => button.textContent === 'Mark outcome complete')!.click();
  apply();
  assert.equal(calls.length, 0);
  assert.match(host.querySelector('[role="alert"]')!.textContent!, /evidence is required/);
  const note = host.querySelector('textarea')!;
  note.value = 'Verified scoped outcome';
  note.dispatchEvent(new dom.window.Event('input'));
  sidebar.update({ ...snapshot, revision: snapshot.revision + 1 });
  apply();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [{ action: 'accept', revision: snapshot.revision, note: 'Verified scoped outcome' }]);
  assert.equal(host.querySelector('textarea')!.value, 'Verified scoped outcome');
  assert.match(host.querySelector('[role="alert"]')!.textContent!, /stale register revision/);
  dom.window.close();
});
