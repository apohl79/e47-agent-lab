import assert from 'node:assert/strict';
import { mkdtempSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';
import { initializeTaskRegister, readTaskRegister, writeTaskCommand } from '../src/task-register.ts';
import type { TaskCommand, TaskSnapshot } from '../src/task-types.ts';

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'task-dispatch-'));
  const path = join(root, 'tasks.json');
  const doc = join(root, 'today.md');
  writeFileSync(doc, '# Tasks\n\n<!-- task-register: tasks.json -->\n\n- [ ] Review <!-- task-id: TASK-1 -->\n');
  initializeTaskRegister(path);
  const update = (command: Omit<TaskCommand, 'expectedRevision'>, actor: 'user' | 'agent' = 'agent') =>
    writeTaskCommand(path, { ...command, expectedRevision: readTaskRegister(path).revision }, actor);
  update({ action: 'create', id: 'TASK-1', task: { title: 'Review', source: 'local test' } });
  update({ action: 'classify', id: 'TASK-1', task: {
    route: 'agent_preparable', readinessConfidence: 95, classificationConfidence: 99,
    scope: 'Prepare a local review', permissions: ['read_sources', 'write_artifacts'],
    acceptance: 'Review is source-linked', stopCondition: 'Stop for final user review',
  } });
  return { root, path, doc, update };
}

test('approval HTTP handoff, atomic claim, binding and worker lifecycle preserve one owner', async () => {
  const { root, path, doc, update } = fixture();
  update({ action: 'classify', id: 'TASK-1', task: { readinessConfidence: 0 } });
  const note = 'I started the DevEx conversation; prepare a follow-up for review.';
  const prompts: string[] = [];
  const server = await createServer({
    docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs'),
    agentFactory: mockAgentFactory({ reply: '', conclusion: '' }),
    mainSession: { async send(prompt) { prompts.push(prompt); } },
  });
  try {
    const approve = () => fetch(`http://127.0.0.1:${server.port}/api/tasks/command`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'approve', id: 'TASK-1', expectedRevision: readTaskRegister(path).revision, note }),
    });
    const response = await approve();
    assert.equal(response.status, 200);
    assert.match((await response.json() as TaskSnapshot).notification!, /notified/);
    assert.equal(prompts.length, 1);
    assert.equal(readTaskRegister(path).tasks[0]!.approvalNote, note);
    assert.match(prompts[0]!, /approvalNote.*verbatim/);
    const event = JSON.parse(prompts[0]!.split('\n').at(-1)!);
    assert.equal(event.type, 'inline-discussion.task-ready');
    assert.equal(event.projectRoot, root);
    assert.equal(event.registerPath, realpathSync(path));
    assert.equal(event.documentPath, doc);
    assert.equal(event.taskId, 'TASK-1');
    assert.equal(event.approval, readTaskRegister(path).tasks[0]!.approval);
    assert.equal((await approve()).status, 409);
    assert.equal(prompts.length, 1);
    assert.throws(() => update({ action: 'claim', id: 'TASK-1', claimId: 'claim-a', expectedTaskRevision: event.taskRevision - 1 }), /stale task approval/);
    update({ action: 'claim', id: 'TASK-1', claimId: 'claim-a', expectedTaskRevision: event.taskRevision });
    const claimedRevision = readTaskRegister(path).tasks[0]!.revision;
    assert.throws(() => update({ action: 'claim', id: 'TASK-1', claimId: 'claim-b', expectedTaskRevision: claimedRevision }), /already claimed/);
    assert.throws(() => update({ action: 'start', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } }), /bound dispatch/);
    assert.throws(() => update({ action: 'bind', id: 'TASK-1', claimId: 'claim-b', task: { session: 'worker-1' } }), /claim missing/);
    update({ action: 'bind', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } });
    assert.throws(() => update({ action: 'bind', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-2' } }), /another session/);
    assert.throws(() => update({ action: 'start', id: 'TASK-1', task: { session: 'worker-1' } }), /claim missing/);
    update({ action: 'start', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } });
    assert.equal(readTaskRegister(path).tasks[0]!.state, 'in_progress');
    update({ action: 'needs_input', id: 'TASK-1', note: 'Which report?' });
    const resumed = await fetch(`http://127.0.0.1:${server.port}/api/tasks/command`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ action: 'resume', id: 'TASK-1', expectedRevision: readTaskRegister(path).revision, note: 'The local report' }),
    });
    assert.equal(resumed.status, 200);
    assert.equal(prompts.length, 2);
    const task = readTaskRegister(path).tasks[0]!;
    assert.equal(task.dispatch, undefined);
    assert.equal(task.session, 'worker-1');
    assert.equal(task.approvalNote, 'The local report');
    assert.throws(() => update({ action: 'start', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } }), /claim missing/);
    update({ action: 'claim', id: 'TASK-1', claimId: 'claim-resume', expectedTaskRevision: task.revision });
    update({ action: 'bind', id: 'TASK-1', claimId: 'claim-resume', task: { session: 'worker-1' } });
    update({ action: 'start', id: 'TASK-1', claimId: 'claim-resume', task: { session: 'worker-1' } });
    update({ action: 'submit', id: 'TASK-1', note: 'Prepared', task: { deliverable: 'review.md', evidence: 'Checked local sources' } });
    assert.equal(readTaskRegister(path).tasks[0]!.state, 'ready_for_review');
    assert.throws(() => update({ action: 'accept', id: 'TASK-1', note: 'Finished' }), /user action/);
  } finally {
    await server.close();
  }
});

test('lost notification or absent main session leaves a durable ready queue, not a failed approval', async () => {
  for (const failure of [false, true]) {
    const { root, path, doc } = fixture();
    const server = await createServer({
      docPath: doc, projectRoot: root, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs'),
      agentFactory: mockAgentFactory({ reply: '', conclusion: '' }),
      ...(failure ? { mainSession: { async send() { throw new Error('socket unavailable'); } } } : {}),
    });
    try {
      const response = await fetch(`http://127.0.0.1:${server.port}/api/tasks/command`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'approve', id: 'TASK-1', expectedRevision: readTaskRegister(path).revision }),
      });
      assert.equal(response.status, 200);
      const snapshot = await response.json() as TaskSnapshot;
      assert.match(snapshot.notification!, failure ? /notification failed.*socket unavailable/ : /No owning main session/);
      const persisted = readTaskRegister(path).tasks[0]!;
      assert.equal(persisted.state, 'ready');
      assert.equal(persisted.dispatch, undefined);
      assert.ok(persisted.approval);
    } finally {
      await server.close();
    }
  }
});

test('claim survives reload without timeout takeover and scope change invalidates it', () => {
  const { path, update } = fixture();
  update({ action: 'approve', id: 'TASK-1' }, 'user');
  const revision = readTaskRegister(path).tasks[0]!.revision;
  update({ action: 'claim', id: 'TASK-1', claimId: 'claim-a', expectedTaskRevision: revision });
  assert.equal(readTaskRegister(path).tasks[0]!.dispatch?.id, 'claim-a');
  update({ action: 'classify', id: 'TASK-1', task: { scope: 'Changed scope' } });
  assert.equal(readTaskRegister(path).tasks[0]!.dispatch, undefined);
  assert.throws(() => update({ action: 'bind', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } }), /not ready/);
  update({ action: 'approve', id: 'TASK-1' }, 'user');
  assert.throws(() => update({ action: 'bind', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } }), /claim missing/);
  assert.throws(() => update({ action: 'start', id: 'TASK-1', claimId: 'claim-a', task: { session: 'worker-1' } }), /claim missing/);
});
