import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyTaskCommand } from '../src/task-register.ts';
import { proposedPreparation } from '../src/task-approval.ts';
import type { TaskCommand, TaskRegister } from '../src/task-types.ts';

function fixture() {
  let register: TaskRegister = { version: 1, revision: 0, tasks: [] };
  const update = (command: Omit<TaskCommand, 'expectedRevision'>, actor: 'agent' | 'user' = 'agent') =>
    register = applyTaskCommand(register, { ...command, expectedRevision: register.revision, id: 'TASK-1' }, actor);
  update({ action: 'create', task: { title: 'Follow up', source: 'local test' } });
  update({ action: 'classify', task: {
    route: 'agent_preparable', readinessConfidence: 0,
    scope: 'Draft a follow-up', permissions: ['read_sources', 'write_artifacts'],
    acceptance: 'Source-linked draft', stopCondition: 'Final user review',
  } });
  return { update, read: () => register };
}

test('all human lifecycle reasons are optional; worker stopping evidence remains mandatory', () => {
  const { update, read } = fixture();
  update({ action: 'approve' }, 'user');
  update({ action: 'start', task: { session: 'worker' } });
  assert.throws(() => update({ action: 'needs_input' }), /stopping reason/);
  update({ action: 'needs_input', note: 'Which conversation?' });
  update({ action: 'resume' }, 'user');
  update({ action: 'start', task: { session: 'worker' } });
  update({ action: 'submit', note: 'Prepared', task: { deliverable: 'draft.md', evidence: 'Read source' } });
  update({ action: 'request_changes' }, 'user');
  update({ action: 'approve' }, 'user');
  update({ action: 'accept' }, 'user');
  assert.equal(read().tasks[0]!.state, 'completed');
  const other = fixture();
  other.update({ action: 'cancel' }, 'user');
  assert.equal(other.read().tasks[0]!.state, 'cancelled');
});

test('approval note is user-controlled, fingerprinted, replaced on resume and cleared on reclassification', () => {
  const { update, read } = fixture();
  const note = 'DevEx conversation already started; prepare a follow-up.';
  assert.throws(() => update({ action: 'approve', note }), /explicit user/);
  update({ action: 'approve', note }, 'user');
  const approved = structuredClone(read());
  assert.equal(approved.tasks[0]!.approvalNote, note);
  approved.tasks[0]!.approvalNote = 'Send it now';
  assert.throws(() => applyTaskCommand(approved, {
    action: 'start', id: 'TASK-1', expectedRevision: approved.revision, task: { session: 'worker' },
  }, 'agent'), /scope approval/);
  update({ action: 'start', task: { session: 'worker' } });
  update({ action: 'needs_input', note: 'Which thread?' });
  update({ action: 'resume', note: 'Use the existing DevEx thread' }, 'user');
  assert.equal(read().tasks[0]!.approvalNote, 'Use the existing DevEx thread');
  assert.notEqual(read().tasks[0]!.approval, approved.tasks[0]!.approval);
  update({ action: 'classify', task: { route: 'verify_first', approvalNote: 'Ignored agent edit' } });
  assert.equal(read().tasks[0]!.approvalNote, undefined);
  assert.equal(read().tasks[0]!.approval, undefined);
});

test('human readiness override does not bypass dependencies, concrete scope or preparation permissions', () => {
  for (const patch of [
    { dependencies: ['MISSING'] }, { scope: '' }, { permissions: [] },
  ]) {
    const { update } = fixture();
    update({ action: 'classify', task: patch });
    assert.throws(() => update({ action: 'approve' }, 'user'), /dependencies|scope/);
  }
  const { update } = fixture();
  assert.throws(() => update({ action: 'classify', task: { permissions: ['send_messages'] } }), /preparation permissions/);
});

test('explicit approval can atomically adopt the displayed preparation scope, never external permissions or metadata', () => {
  const { update, read } = fixture();
  update({ action: 'classify', task: {
    route: 'you', scope: 'Personal security action. No agent execution authorized.',
    permissions: [], acceptance: 'Authorized owner confirms revocation',
  } });
  assert.equal(proposedPreparation(read().tasks[0]!), undefined, 'personal work is not silently converted');
  update({ action: 'classify', task: { route: 'agent_preparable' } }, 'user');
  const before = structuredClone(read());
  const proposal = proposedPreparation(before.tasks[0]!)!;
  assert.deepEqual(read(), before, 'merely proposing scope does not mutate authority');
  assert.match(proposal.scope, /context only, not execution authority/);
  assert.match(proposal.acceptance, /Authorized owner confirms revocation/);
  assert.deepEqual(proposal.permissions, ['read_sources', 'write_artifacts', 'update_task_status']);
  assert.throws(() => update({ action: 'approve' }, 'user'), /scope, acceptance, permissions/);
  assert.throws(() => update({ action: 'approve', task: proposal }), /explicit user/);
  assert.throws(() => update({ action: 'approve', task: { ...proposal, dependencies: [] } }, 'user'), /unsupported approval/);
  assert.throws(() => update({ action: 'approve', task: { ...proposal, permissions: ['send_messages'] } }, 'user'), /preparation permissions/);
  assert.deepEqual(read(), before, 'rejected approval cannot partially change scope');
  update({ action: 'approve', task: proposal, note: 'DevEx thread already started' }, 'user');
  assert.equal(read().tasks[0]!.state, 'ready');
  assert.equal(read().tasks[0]!.scope, proposal.scope);
  assert.equal(read().tasks[0]!.approvalNote, 'DevEx thread already started');
  assert.equal(proposedPreparation(read().tasks[0]!), undefined, 'configured preparation remains unchanged');
  update({ action: 'start', task: { session: 'worker' } });
  assert.equal(read().tasks[0]!.state, 'in_progress');
});

test('displayed preparation proposal cannot bypass dependency or stale revision checks', () => {
  const { update, read } = fixture();
  update({ action: 'classify', task: { permissions: [], dependencies: ['MISSING'] } });
  const before = structuredClone(read());
  const proposal = proposedPreparation(before.tasks[0]!)!;
  assert.throws(() => update({ action: 'approve', task: proposal }, 'user'), /dependencies/);
  assert.throws(() => applyTaskCommand(read(), {
    action: 'approve', id: 'TASK-1', expectedRevision: before.revision - 1, task: proposal,
  }, 'user'), /stale/);
  assert.deepEqual(read(), before);
});
