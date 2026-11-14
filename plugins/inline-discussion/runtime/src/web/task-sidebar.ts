import { TASK_STATES, type Task, type TaskSnapshot } from '../task-types.ts';
import { modalConfirm } from './modal.ts';

const label = (value: string) => value.replaceAll('_', ' ');
export function createTaskSidebar(
  document: Document,
  command: (action: string, id: string, revision: number, note: string) => Promise<TaskSnapshot>,
): { update(snapshot: TaskSnapshot): void; refresh(): void } {
  const host = document.createElement('aside');
  host.id = 'task-sidebar';
  host.setAttribute('aria-label', 'Tasks');
  host.hidden = true;
  host.dataset.collapsed = 'true';
  const doc = document.getElementById('doc');
  if (doc) doc.before(host); else document.body.append(host);
  const toggle = document.createElement('button');
  toggle.id = 'task-sidebar-toggle';
  toggle.className = 'btn';
  toggle.textContent = 'Tasks';
  toggle.hidden = true;
  toggle.setAttribute('aria-controls', host.id);
  toggle.setAttribute('aria-expanded', 'false');
  toggle.addEventListener('click', () => {
    const open = host.dataset.collapsed === 'true';
    host.dataset.collapsed = String(!open);
    toggle.setAttribute('aria-expanded', String(open));
  });
  const controls = document.getElementById('controls');
  if (controls) controls.prepend(toggle); else host.before(toggle);
  const heading = document.createElement('h2');
  heading.textContent = 'Tasks · current status';
  const filter = document.createElement('select');
  filter.setAttribute('aria-label', 'Filter tasks by status');
  for (const value of ['open', ...TASK_STATES]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value === 'open' ? 'All open tasks' : label(value);
    filter.append(option);
  }
  const list = document.createElement('div');
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  host.append(heading, filter, error, list);
  let snapshot: TaskSnapshot = { enabled: false, revision: 0, tasks: [] };
  let expanded = '';
  let busy = false;
  const notes = new Map<string, string>();
  function detail(parent: HTMLElement, name: string, value: string): void {
    if (!value) return;
    const line = document.createElement('p');
    const title = document.createElement('strong');
    title.textContent = `${name}: `;
    line.append(title, document.createTextNode(value));
    parent.append(line);
  }
  function link(parent: HTMLElement, name: string, value: string): void {
    if (!value) return;
    const target = value.match(/^\[[^\]]+\]\(([^)]+)\)$/)?.[1] ?? value;
    const a = document.createElement('a');
    if (/^https?:\/\//i.test(target)) {
      a.href = target;
      a.textContent = name;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      parent.append(a);
    } else if (/^\/(?!\/)[^\\\r\n]+$/.test(target) || /^[\w.-]+(?:\/[\w .-]+)+$/.test(target)) {
      a.setAttribute('href', target.startsWith('/') ? target : `/${target}`);
      a.textContent = name;
      parent.append(a);
    } else detail(parent, name, value);
  }
  function renderTask(task: Task): HTMLElement {
    const row = document.createElement('section');
    row.className = 'task-sidebar-item';
    const jump = document.createElement('a');
    jump.textContent = task.title;
    jump.href = `#task-${task.id}`;
    jump.addEventListener('click', (event) => {
      const target = document.getElementById(`task-${task.id}`);
      if (!target) return;
      event.preventDefault();
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    const status = document.createElement('span');
    status.className = 'task-status';
    status.dataset.state = task.state;
    status.textContent = label(task.state);
    const details = document.createElement('details');
    details.open = expanded === task.id;
    const summary = document.createElement('summary');
    summary.textContent = `${task.id} · ${task.owner || 'Unassigned'}`;
    details.append(summary);
    details.addEventListener('toggle', () => { if (details.open) expanded = task.id; else if (expanded === task.id) expanded = ''; });
    detail(details, 'Route', label(task.route));
    detail(details, 'Human delegation', task.humanDelegable ? `${task.candidateDelegate || 'Unassigned'} · ${task.handoff}` : 'Not proposed');
    detail(details, 'Confidence', `classification ${task.classificationConfidence}% · readiness ${task.readinessConfidence}%`);
    detail(details, 'Scope', task.scope);
    detail(details, 'Permissions', task.permissions.join(', '));
    detail(details, 'Acceptance', task.acceptance);
    detail(details, 'Stop', task.stopCondition);
    detail(details, 'Dependencies', task.dependencies.join(', '));
    detail(details, 'Progress', task.progress);
    detail(details, 'Question', task.question);
    detail(details, 'First seen', task.firstSeen);
    link(details, 'Source', task.source);
    link(details, 'Session', task.session);
    link(details, 'Deliverable', task.deliverable);
    detail(details, 'Verification', task.evidence);
    const history = document.createElement('details');
    const historyTitle = document.createElement('summary');
    historyTitle.textContent = 'History';
    history.append(historyTitle);
    for (const entry of task.history) detail(history, `${entry.at} · ${entry.actor} · ${label(entry.state)}`, entry.note);
    details.append(history);
    const actions: Array<[string, string]> = [];
    if (task.state === 'awaiting_approval') actions.push(['approve', 'Approve preparation']);
    if (['needs_input', 'blocked'].includes(task.state)) actions.push(['resume', 'Answer / resume']);
    if (task.state === 'ready_for_review') actions.push(['accept', 'Accept outcome'], ['request_changes', 'Request changes']);
    else if (!['completed', 'cancelled'].includes(task.state)) actions.push(['accept', 'Mark outcome complete']);
    if (!['completed', 'cancelled'].includes(task.state)) actions.push(['cancel', 'Cancel task']);
    const note = document.createElement('textarea');
    note.setAttribute('aria-label', `Answer or review evidence for ${task.id}`);
    note.placeholder = 'Answer, feedback, or outcome evidence';
    note.value = notes.get(task.id) ?? '';
    note.addEventListener('input', () => notes.set(task.id, note.value));
    if (actions.some(([action]) => action !== 'approve')) details.append(note);
    for (const [action, text] of actions) {
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = text;
      button.disabled = busy || (action === 'approve' && task.readinessConfidence <= 90);
      button.addEventListener('click', async () => {
        const expectedRevision = snapshot.revision;
        let text = notes.get(task.id) ?? '';
        if (action === 'approve') {
          const accepted = await modalConfirm({
            title: 'Approve preparation',
            body: `${task.scope}\n\nPermissions: ${task.permissions.join(', ')}\n\nAcceptance: ${task.acceptance}\n\nStop: ${task.stopCondition}\n\nThis records approval only. It does not launch a session or authorize external actions.`,
            primaryLabel: 'Approve preparation',
          });
          if (!accepted) return;
          text = 'Approved displayed preparation scope';
        } else if (!text.trim()) {
          error.textContent = action === 'accept' ? 'Outcome-level acceptance evidence is required.' : 'An answer, feedback, or reason is required.';
          note.focus();
          return;
        }
        busy = true;
        render();
        try {
          snapshot = await command(action, task.id, expectedRevision, text);
          notes.delete(task.id);
          error.textContent = '';
        } catch (failure) {
          error.textContent = failure instanceof Error ? failure.message : String(failure);
        } finally {
          busy = false;
          render();
        }
      });
      details.append(button);
    }
    row.append(jump, status, details);
    return row;
  }
  function render(): void {
    host.hidden = !snapshot.enabled;
    toggle.hidden = !snapshot.enabled;
    document.body.classList.toggle('has-task-sidebar', snapshot.enabled);
    if (snapshot.error) error.textContent = snapshot.error;
    list.replaceChildren(...snapshot.tasks.filter((task) => filter.value === 'open' ? !['completed', 'cancelled'].includes(task.state) : task.state === filter.value).map(renderTask));
    for (const task of snapshot.tasks) {
      const target = document.getElementById(`task-${task.id}`);
      if (!target) continue;
      let badge = target.querySelector<HTMLElement>(':scope > .task-live-status');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'task-live-status';
        target.append(badge);
      }
      badge.textContent = ` · Current: ${label(task.state)}`;
      const checkbox = target.querySelector<HTMLInputElement>('input[type="checkbox"]');
      if (checkbox) { checkbox.checked = task.state === 'completed'; checkbox.disabled = true; }
    }
  }
  filter.addEventListener('change', render);
  return { update(value) { snapshot = value; error.textContent = value.error ?? ''; render(); }, refresh: render };
}
