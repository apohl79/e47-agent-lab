import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

import { TASK_STATES, type Task, type TaskRegister, type TaskCommand } from './task-types.ts';
export type { Task, TaskRegister, TaskCommand } from './task-types.ts';
const ROUTES = ['agent', 'agent_preparable', 'human_delegable', 'you', 'verify_first'];
const PERMISSIONS = ['read_sources', 'write_artifacts', 'write_isolated_code', 'run_local_checks', 'update_task_status'];
const USER_ACTIONS = ['approve', 'resume', 'request_changes', 'accept', 'cancel'];
const TEXT_FIELDS = ['title', 'source', 'owner', 'candidateDelegate', 'scope', 'acceptance', 'stopCondition', 'progress', 'question', 'session', 'deliverable', 'evidence'] as const;
const SCOPE_FIELDS = ['title', 'source', 'owner', 'scope', 'permissions', 'acceptance', 'stopCondition', 'dependencies', 'route', 'readinessConfidence'] as const;

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function validateTask(task: Task): void {
  check(typeof task.id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(task.id), 'invalid task ID');
  for (const key of TEXT_FIELDS) check(typeof task[key] === 'string', `invalid ${key}`);
  check(task.title.trim() && task.source.trim(), 'title and source required');
  check(['none', 'proposed', 'accepted'].includes(task.handoff), 'invalid delegation');
  check(TASK_STATES.includes(task.state) && ROUTES.includes(task.route), 'invalid task state or route');
  for (const key of ['classificationConfidence', 'readinessConfidence'] as const) {
    check(Number.isFinite(task[key]) && task[key] >= 0 && task[key] <= 100, `invalid ${key}`);
  }
  for (const key of ['permissions', 'dependencies'] as const) {
    check(Array.isArray(task[key]) && task[key].every((value) => typeof value === 'string'), `invalid ${key}`);
  }
  check(task.permissions.every((value) => PERMISSIONS.includes(value)), 'only preparation permissions are supported');
  check(Number.isSafeInteger(task.revision) && task.revision >= 1 && Array.isArray(task.history), 'invalid task revision/history');
  check(task.approvalNote === undefined || typeof task.approvalNote === 'string', 'invalid approval note');
  if (task.dispatch !== undefined) {
    check(typeof task.dispatch?.id === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(task.dispatch.id)
      && typeof task.dispatch.approval === 'string' && task.dispatch.approval.length === 64, 'invalid dispatch claim');
  }
}
export function readTaskRegister(path: string): TaskRegister {
  const value = JSON.parse(readFileSync(path, 'utf8')) as TaskRegister;
  check(value.version === 1 && Number.isSafeInteger(value.revision) && value.revision >= 0 && Array.isArray(value.tasks), 'invalid task register');
  value.tasks.forEach(validateTask);
  check(new Set(value.tasks.map((task) => task.id)).size === value.tasks.length, 'duplicate task ID');
  return value;
}
function scopeFingerprint(task: Task): string {
  const scope: unknown[] = SCOPE_FIELDS.map((key) => task[key]);
  if (task.approvalNote) scope.push(task.approvalNote);
  return createHash('sha256').update(JSON.stringify(scope)).digest('hex');
}
function ready(task: Task, register: TaskRegister): void {
  check(['agent', 'agent_preparable', 'verify_first'].includes(task.route), 'task has no agent work route');
  check(task.scope.trim() && task.acceptance.trim() && task.stopCondition.trim() && task.permissions.length, 'scope, acceptance, permissions and stop condition required');
  check(task.dependencies.every((id) => register.tasks.some((entry) => entry.id === id && entry.state === 'completed')), 'dependencies are not completed');
}
export function applyTaskCommand(register: TaskRegister, command: TaskCommand, actor: 'agent' | 'user'): TaskRegister {
  check(command.expectedRevision === register.revision, 'stale task register revision; reload before retrying');
  check(command.note === undefined || typeof command.note === 'string', 'invalid note');
  check(!USER_ACTIONS.includes(command.action) || actor === 'user', 'explicit user action required');
  const next = structuredClone(register);
  const now = new Date().toISOString();
  let task = next.tasks.find((entry) => entry.id === command.id);
  if (command.action === 'create') {
    check(!task, 'task already exists');
    const input = command.task ?? {};
    task = {
      id: command.id ?? `TASK-${randomUUID().slice(0, 8)}`,
      title: input.title ?? '', source: input.source ?? '', owner: input.owner ?? '',
      state: 'identified', route: 'verify_first', classificationConfidence: 0, readinessConfidence: 0,
      candidateDelegate: '', handoff: 'none',
      scope: '', permissions: [], acceptance: '', stopCondition: '', dependencies: [],
      firstSeen: now.slice(0, 10), updatedAt: now, revision: 1, progress: '', question: '',
      session: '', deliverable: '', evidence: '', history: [],
    };
    check(!next.tasks.some((entry) => entry.source === task!.source && entry.title === task!.title && !['completed', 'cancelled'].includes(entry.state)), 'open task already exists; reuse its ID');
    next.tasks.push(task);
  } else {
    check(task, 'task not found');
    check(!['completed', 'cancelled'].includes(task.state), 'task is terminal; create an explicit follow-up');
    task.revision += 1;
    if (command.action === 'classify') {
      check(!['in_progress', 'ready_for_review'].includes(task.state), 'stop active/review work before changing scope');
      const patch = command.task ?? {};
      for (const key of [...TEXT_FIELDS, 'route', 'classificationConfidence', 'readinessConfidence', 'permissions', 'dependencies', 'handoff'] as const) {
        if (patch[key] !== undefined) Object.assign(task, { [key]: patch[key] });
      }
      task.approval = undefined;
      task.approvalNote = undefined;
      task.dispatch = undefined;
      task.state = ['agent', 'agent_preparable', 'verify_first'].includes(task.route) ? 'awaiting_approval' : 'identified';
    } else if (command.action === 'approve') {
      check(task.state === 'awaiting_approval', 'task is not awaiting approval');
      if (command.task !== undefined) {
        const fields = ['scope', 'permissions', 'acceptance', 'stopCondition'];
        check(Object.keys(command.task).every((key) => fields.includes(key)), 'unsupported approval scope field');
        Object.assign(task, command.task);
        validateTask(task);
      }
      ready(task, next);
      task.approvalNote = command.note?.trim() || undefined;
      task.approval = scopeFingerprint(task);
      task.dispatch = undefined;
      task.state = 'ready';
    } else if (command.action === 'claim' || command.action === 'bind') {
      check(task.state === 'ready', 'task is not ready');
      ready(task, next);
      check(task.approval === scopeFingerprint(task), 'scope approval missing or invalidated');
      check(typeof command.claimId === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(command.claimId), 'dispatch claim ID required');
      if (command.action === 'claim') {
        check(command.expectedTaskRevision === task.revision - 1, 'stale task approval event; reload before dispatch');
        check(!task.dispatch, 'task dispatch is already claimed; do not launch another session');
        task.dispatch = { id: command.claimId, approval: task.approval };
      } else {
        check(task.dispatch?.id === command.claimId && task.dispatch.approval === task.approval, 'dispatch claim missing or invalidated');
        const session = command.task?.session;
        check(typeof session === 'string' && session.trim(), 'session reference required');
        check(!task.session || task.session === session, 'task already belongs to another session');
        task.session = session;
      }
    } else if (command.action === 'start') {
      check(task.state === 'ready', 'task is not ready');
      ready(task, next);
      check(task.approval === scopeFingerprint(task), 'scope approval missing or invalidated');
      const session = command.task?.session;
      check(typeof session === 'string' && session.trim(), 'session reference required');
      if (task.dispatch || command.claimId !== undefined) {
        check(task.dispatch?.id === command.claimId && task.dispatch?.approval === task.approval, 'dispatch claim missing or invalidated');
        check(task.session === session, 'worker does not match bound dispatch session');
      }
      task.session = session;
      task.state = 'in_progress';
    } else if (['progress', 'needs_input', 'block', 'submit'].includes(command.action)) {
      check(task.state === 'in_progress', 'task is not in progress');
      check(typeof command.note === 'string' && command.note.trim(), 'progress or stopping reason required');
      task.progress = command.note;
      if (command.action === 'needs_input') {
        task.question = command.note;
        task.state = 'needs_input';
      }
      if (command.action === 'block') task.state = 'blocked';
      if (command.action === 'submit') {
        const deliverable = command.task?.deliverable;
        const evidence = command.task?.evidence;
        check(typeof deliverable === 'string' && deliverable.trim() && typeof evidence === 'string' && evidence.trim(), 'deliverable and verification evidence required');
        task.deliverable = deliverable;
        task.evidence = evidence;
        task.state = 'ready_for_review';
      }
    } else if (command.action === 'resume') {
      check(['needs_input', 'blocked'].includes(task.state), 'task is not waiting');
      ready(task, next);
      check(task.approval === scopeFingerprint(task), 'scope approval missing or invalidated');
      task.approvalNote = command.note?.trim() || undefined;
      task.approval = scopeFingerprint(task);
      task.progress = command.note ?? '';
      task.question = '';
      task.dispatch = undefined;
      task.state = 'ready';
    } else if (command.action === 'request_changes') {
      check(task.state === 'ready_for_review', 'task is not ready for review');
      task.progress = command.note ?? '';
      task.approval = undefined;
      task.approvalNote = undefined;
      task.state = 'awaiting_approval';
    } else if (command.action === 'accept') {
      task.state = 'completed';
    } else if (command.action === 'cancel') {
      task.state = 'cancelled';
      task.approval = undefined;
      task.approvalNote = undefined;
    } else {
      throw new Error('unknown task action');
    }
  }
  validateTask(task);
  task.updatedAt = now;
  task.history.push({ at: now, actor, state: task.state, note: command.note ?? command.action });
  next.revision += 1;
  return next;
}
export function writeTaskCommand(path: string, command: TaskCommand, actor: 'agent' | 'user'): TaskRegister {
  const lock = `${path}.lock`;
  mkdirSync(lock);
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    const next = applyTaskCommand(readTaskRegister(path), command, actor);
    writeFileSync(temporary, JSON.stringify(next, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
    renameSync(temporary, path);
    return next;
  } finally {
    try { unlinkSync(temporary); } catch { /* no temporary file after rename */ }
    rmdirSync(lock);
  }
}
export function initializeTaskRegister(path: string): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify({ version: 1, revision: 0, tasks: [] }, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
}
