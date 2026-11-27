import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyTaskCommand } from '../src/task-register.ts';
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
