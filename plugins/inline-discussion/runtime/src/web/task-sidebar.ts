import { TASK_STATES, type Task, type TaskSnapshot } from '../task-types.ts';
import { modalConfirm } from './modal.ts';

const label = (value: string) => value.replaceAll('_', ' ');
export function createTaskSidebar(
  document: Document,
  command: (action: string, id: string, revision: number, note: string, task?: Partial<Task>) => Promise<TaskSnapshot>,
): { update(snapshot: TaskSnapshot): void; refresh(): void } {
  const host = document.createElement('aside');
  host.id = 'task-sidebar';
  host.setAttribute('aria-label', 'Tasks');
  host.hidden = true;
  const doc = document.getElementById('doc');
  if (doc) doc.before(host); else document.body.append(host);
  const toggle = document.createElement('button');
  toggle.id = 'task-sidebar-toggle';
  toggle.className = 'btn';
  toggle.textContent = 'Tasks';
  toggle.hidden = true;
  toggle.setAttribute('aria-controls', host.id);
  toggle.setAttribute('aria-expanded', 'false');
  function setMode(open: boolean): void {
    host.hidden = !open || !snapshot.enabled;
    document.body.classList.toggle('task-mode', open && snapshot.enabled);
    toggle.textContent = open ? 'Document' : 'Tasks';
    toggle.setAttribute('aria-expanded', String(open));
  }
  toggle.addEventListener('click', () => setMode(host.hidden));
  const controls = document.getElementById('controls');
  if (controls) controls.prepend(toggle); else host.before(toggle);
  const heading = document.createElement('h2');
  heading.textContent = 'Tasks';
  const filter = document.createElement('select');
  filter.setAttribute('aria-label', 'Filter tasks by status');
  for (const value of ['open', 'all', ...TASK_STATES]) {
    const option = document.createElement('option');
    option.value = value;
      option.textContent = value === 'open' ? 'All open tasks' : value === 'all' ? 'All tasks' : label(value);
    filter.append(option);
  }
  const list = document.createElement('div');
  list.className = 'task-workspace';
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  host.append(heading, filter, error, list);
  let snapshot: TaskSnapshot = { enabled: false, revision: 0, tasks: [] };
  let expanded = '';
  let busy = false;
  const notes = new Map<string, string>();
  const quickActions = new Map<string, { action: string; revision: number }>();
  type ClassificationDraft = { revision: number; route: Task['route']; humanDelegable: boolean; classificationConfidence: number; readinessConfidence: number; reason: string };
  const drafts = new Map<string, ClassificationDraft>();
  const routes: Array<[Task['route'], string]> = [
    ['agent', 'Fully agent-executable'], ['agent_preparable', 'Agent-preparable'],
    ['human_delegable', 'Human-delegable'], ['you', 'Personal'], ['verify_first', 'Verify first'],
  ];
  doc?.addEventListener('click', (event) => {
    const input = (event.target as Element).closest<HTMLInputElement>('input[type="checkbox"]');
    const id = input?.closest<HTMLElement>('[data-task-id]')?.dataset.taskId;
    if (!id) return;
    event.preventDefault();
    if (!snapshot.tasks.some((task) => task.id === id)) {
      setMode(true);
      error.textContent = snapshot.error ?? `Task ${id} is unavailable. Reload the document to refresh its register.`;
      return;
    }
    quickActions.delete(id);
    expanded = id;
    filter.value = 'all';
    setMode(true);
    render();
    list.querySelector<HTMLElement>('.task-sidebar-item textarea, .task-sidebar-item a')?.focus();
  });
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
  function actionsFor(task: Task): Array<[string, string]> {
    const actions: Array<[string, string]> = [];
    if (task.state === 'awaiting_approval') actions.push(['approve', 'Approve preparation']);
    if (['needs_input', 'blocked'].includes(task.state)) actions.push(['resume', 'Answer / resume']);
    if (task.state === 'ready_for_review') actions.push(['accept', 'Accept outcome'], ['request_changes', 'Request changes']);
    else if (!['completed', 'cancelled'].includes(task.state)) actions.push(['accept', 'Mark outcome complete']);
    if (!['completed', 'cancelled'].includes(task.state)) actions.push(['cancel', 'Cancel task']);
    return actions;
  }
  async function runAction(task: Task, action: string, expectedRevision: number, note: HTMLTextAreaElement): Promise<void> {
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
      quickActions.delete(task.id);
      error.textContent = '';
    } catch (failure) {
      error.textContent = failure instanceof Error ? failure.message : String(failure);
    } finally {
      busy = false;
      render();
    }
  }
  function renderQuickState(task: Task): HTMLElement {
    const cell = document.createElement('div');
    cell.className = 'task-quick-state';
    const select = document.createElement('select');
    select.id = `task-quick-select-${task.id}`;
    select.setAttribute('aria-label', `Quick state for ${task.id}`);
    const actions = actionsFor(task);
    const current = document.createElement('option');
    current.value = '';
    current.textContent = label(task.state);
    select.append(current);
    const destinations: Record<string, string> = { approve: 'ready', resume: 'ready', request_changes: 'awaiting approval', accept: 'completed', cancel: 'cancelled' };
    for (const [action, text] of actions) {
      const option = document.createElement('option');
      option.value = action; option.textContent = `${destinations[action]} · ${text}`;
      option.disabled = action === 'approve' && task.readinessConfidence <= 90;
      select.append(option);
    }
    select.disabled = busy || !actions.length;
    const pending = quickActions.get(task.id);
    const selected = actions.find(([action]) => action === pending?.action);
    select.value = selected?.[0] ?? '';
    select.addEventListener('change', () => {
      if (select.value) quickActions.set(task.id, { action: select.value, revision: snapshot.revision });
      else quickActions.delete(task.id);
      if (expanded === task.id) expanded = '';
      render();
      (document.getElementById(`task-quick-note-${task.id}`) ?? document.getElementById(`task-quick-apply-${task.id}`) ?? document.getElementById(select.id))?.focus();
    });
    cell.append(select);
    if (selected && pending) {
      const note = document.createElement('textarea');
      note.id = `task-quick-note-${task.id}`;
      note.setAttribute('aria-label', `Quick action evidence for ${task.id}`);
      note.placeholder = 'Answer, feedback, or outcome evidence';
      note.value = notes.get(task.id) ?? '';
      note.disabled = busy;
      note.addEventListener('input', () => notes.set(task.id, note.value));
      if (selected[0] !== 'approve') cell.append(note);
      const apply = document.createElement('button');
      apply.id = `task-quick-apply-${task.id}`;
      apply.className = 'btn'; apply.textContent = selected[1];
      apply.disabled = busy || (selected[0] === 'approve' && task.readinessConfidence <= 90);
      apply.addEventListener('click', () => void runAction(task, selected[0], pending.revision, note));
      const cancel = document.createElement('button');
      cancel.className = 'btn'; cancel.textContent = 'Cancel quick action'; cancel.disabled = busy;
      cancel.addEventListener('click', () => { quickActions.delete(task.id); render(); document.getElementById(select.id)?.focus(); });
      const notice = document.createElement('p');
      notice.textContent = `Current: ${label(task.state)}. ${selected[0] === 'approve' ? 'Review scope in the confirmation. No session will start.' : 'Evidence or a reason is required before applying.'}`;
      cell.append(notice, apply, cancel);
    }
    return cell;
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
      setMode(false);
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    const status = document.createElement('span');
    status.className = 'task-status';
    status.dataset.state = task.state;
    status.textContent = label(task.state);
    const details = document.createElement('details');
    details.open = true;
    const summary = document.createElement('summary');
    summary.textContent = `${task.id} · ${task.owner || 'Unassigned'}`;
    details.append(summary);
    detail(details, 'Route', label(task.route));
    detail(details, 'Human delegation', task.humanDelegable ? `${task.candidateDelegate || 'Unassigned'} · ${task.handoff}` : 'Not proposed');
    detail(details, 'Confidence', `classification ${task.classificationConfidence}% · readiness ${task.readinessConfidence}%`);
    if (!['in_progress', 'ready_for_review', 'completed', 'cancelled'].includes(task.state)) {
      const editor = document.createElement('details');
      const editorTitle = document.createElement('summary');
      editorTitle.textContent = 'Edit classification';
      editor.open = drafts.has(task.id);
      editor.append(editorTitle);
      const draft = drafts.get(task.id) ?? {
        revision: snapshot.revision, route: task.route, humanDelegable: task.humanDelegable,
        classificationConfidence: task.classificationConfidence, readinessConfidence: task.readinessConfidence, reason: '',
      };
      const remember = () => drafts.set(task.id, draft);
      const field = (name: string, input: HTMLElement) => {
        input.setAttribute('aria-label', name);
        const wrapper = document.createElement('label');
        wrapper.className = 'task-field';
        wrapper.append(document.createTextNode(name), input);
        editor.append(wrapper);
      };
      const route = document.createElement('select');
      for (const [value, text] of routes) {
        const option = document.createElement('option'); option.value = value; option.textContent = text; route.append(option);
      }
      route.value = draft.route;
      route.disabled = busy;
      route.addEventListener('change', () => { draft.route = route.value as Task['route']; remember(); });
      field('Classification', route);
      const delegable = document.createElement('input');
      delegable.type = 'checkbox';
      delegable.checked = draft.humanDelegable;
      delegable.disabled = busy;
      delegable.addEventListener('change', () => { draft.humanDelegable = delegable.checked; remember(); });
      field('Human-delegable independently of classification', delegable);
      for (const [key, name] of [['classificationConfidence', 'Classification confidence (%)'], ['readinessConfidence', 'Readiness confidence (%)']] as const) {
        const input = document.createElement('input');
        input.type = 'number'; input.min = '0'; input.max = '100'; input.step = '1';
        input.value = String(draft[key]); input.required = true; input.disabled = busy;
        input.addEventListener('input', () => { draft[key] = input.valueAsNumber; remember(); });
        field(name, input);
      }
      const reason = document.createElement('textarea');
      reason.value = draft.reason;
      reason.required = true; reason.disabled = busy;
      reason.addEventListener('input', () => { draft.reason = reason.value; remember(); });
      field('Classification reason', reason);
      const notice = document.createElement('p');
      notice.textContent = 'Saving clears prior approval. Active work and outcomes under review must be stopped or returned for changes before reclassification.';
      editor.append(notice);
      const save = document.createElement('button');
      save.className = 'btn'; save.textContent = 'Save classification'; save.disabled = busy;
      save.addEventListener('click', async () => {
        if (!draft.reason.trim() || !Array.from(editor.querySelectorAll('input')).every((input) => input.reportValidity())) {
          error.textContent = 'A reason and confidence values from 0 to 100 are required.';
          return;
        }
        busy = true; render();
        try {
          const { revision, reason: note, ...patch } = draft;
          snapshot = await command('classify', task.id, revision, note, patch);
          drafts.delete(task.id);
          error.textContent = '';
        } catch (failure) {
          error.textContent = `${failure instanceof Error ? failure.message : String(failure)}. Discard classification edits to use current values.`;
        } finally { busy = false; render(); }
      });
      const discard = document.createElement('button');
      discard.className = 'btn'; discard.textContent = 'Discard classification edits'; discard.disabled = busy;
      discard.addEventListener('click', () => { drafts.delete(task.id); error.textContent = ''; render(); });
      editor.append(save, discard);
      details.append(editor);
    }
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
    const actions = actionsFor(task);
    const note = document.createElement('textarea');
    note.setAttribute('aria-label', `Answer or review evidence for ${task.id}`);
    note.placeholder = 'Answer, feedback, or outcome evidence';
    note.value = notes.get(task.id) ?? '';
    note.addEventListener('input', () => notes.set(task.id, note.value));
    if (actions.some(([action]) => action !== 'approve')) details.append(note);
    const transition = document.createElement('select');
    transition.setAttribute('aria-label', `Change state for ${task.id}`);
    const current = document.createElement('option');
    current.value = ''; current.textContent = `Current: ${label(task.state)}`;
    transition.append(current);
    const actionButtons: HTMLButtonElement[] = [];
    for (const [action, text] of actions) {
      const option = document.createElement('option');
      option.value = action; option.textContent = text;
      option.disabled = busy || (action === 'approve' && task.readinessConfidence <= 90);
      transition.append(option);
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = text;
      button.hidden = true;
      button.disabled = busy || (action === 'approve' && task.readinessConfidence <= 90);
      button.addEventListener('click', () => void runAction(task, action, snapshot.revision, note));
      details.append(button);
      actionButtons.push(button);
    }
    if (actions.length) {
      transition.disabled = busy;
      transition.addEventListener('change', () => {
        actionButtons.forEach((button, index) => { button.hidden = actions[index]![0] !== transition.value; });
      });
      details.insertBefore(transition, note);
    }
    row.append(jump, status, details);
    return row;
  }
  function render(): void {
    if (!snapshot.enabled) setMode(false);
    toggle.hidden = !snapshot.enabled;
    if (snapshot.error) error.textContent = snapshot.error;
    const visible = snapshot.tasks.filter((task) => filter.value === 'all' || (filter.value === 'open' ? !['completed', 'cancelled'].includes(task.state) : task.state === filter.value));
    const tableWrap = document.createElement('div');
    tableWrap.className = 'task-table-scroll';
    const table = document.createElement('table');
    table.className = 'task-table';
    const caption = document.createElement('caption');
    caption.textContent = `${visible.length} tasks · select a task to review or edit`;
    const head = table.createTHead().insertRow();
    for (const name of ['Task', 'State', 'Classification', 'Human-delegable', 'Confidence / readiness', 'Owner']) {
      const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = name; head.append(cell);
    }
    table.prepend(caption);
    const body = table.createTBody();
    for (const task of visible) {
      const tr = body.insertRow();
      tr.dataset.selected = String(expanded === task.id);
      const choose = document.createElement('button');
      choose.className = 'task-title-button'; choose.textContent = task.title;
      choose.setAttribute('aria-label', task.title);
      choose.setAttribute('aria-expanded', String(expanded === task.id));
      choose.setAttribute('aria-controls', `task-details-${task.id}`);
      choose.addEventListener('click', () => {
        quickActions.delete(task.id);
        expanded = expanded === task.id ? '' : task.id;
        render();
        document.getElementById(`task-toggle-${task.id}`)?.focus();
      });
      choose.id = `task-toggle-${task.id}`;
      tr.insertCell().append(choose);
      tr.insertCell().append(renderQuickState(task));
      for (const value of [routes.find(([route]) => route === task.route)![1], task.humanDelegable ? 'Yes' : 'No', `${task.classificationConfidence}% / ${task.readinessConfidence}%`, task.owner || 'Unassigned']) {
        tr.insertCell().textContent = value;
      }
      if (expanded === task.id) {
        const detailsRow = body.insertRow();
        detailsRow.id = `task-details-${task.id}`;
        detailsRow.className = 'task-detail-row';
        const cell = detailsRow.insertCell();
        cell.colSpan = head.cells.length;
        cell.append(renderTask(task));
      }
    }
    tableWrap.append(table);
    list.replaceChildren(tableWrap);
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
      if (checkbox) { checkbox.checked = task.state === 'completed'; checkbox.disabled = false; checkbox.title = 'Open task controls to review or change this task'; }
    }
  }
  filter.addEventListener('change', render);
  return { update(value) { snapshot = value; error.textContent = value.error ?? ''; render(); }, refresh: render };
}
