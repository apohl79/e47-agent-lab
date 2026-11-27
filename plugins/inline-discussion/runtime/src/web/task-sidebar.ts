import { TASK_STATES, type Task, type TaskSnapshot } from '../task-types.ts';
import { proposedPreparation, type PreparationScope } from '../task-approval.ts';
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
  const scrollPositions = {
    document: { left: 0, top: 0 },
    tasks: { left: 0, top: 0 },
  };
  function setMode(open: boolean): void {
    open = open && snapshot.enabled;
    const wasOpen = !host.hidden;
    const view = document.defaultView;
    if (view && wasOpen !== open) {
      scrollPositions[wasOpen ? 'tasks' : 'document'] = { left: view.scrollX, top: view.scrollY };
    }
    host.hidden = !open;
    document.body.classList.toggle('task-mode', open);
    toggle.textContent = open ? 'Document' : 'Tasks';
    toggle.setAttribute('aria-expanded', String(open));
    if (view && wasOpen !== open) {
      view.scrollTo({ ...scrollPositions[open ? 'tasks' : 'document'], behavior: 'instant' });
    }
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
  const categoryFilter = document.createElement('select');
  categoryFilter.setAttribute('aria-label', 'Filter tasks by category');
  const filters = document.createElement('div');
  filters.className = 'task-filters';
  filters.append(filter, categoryFilter);
  const list = document.createElement('div');
  list.className = 'task-workspace';
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  host.append(heading, filters, error, list);
  let snapshot: TaskSnapshot = { enabled: false, revision: 0, tasks: [] };
  let expanded = '';
  let busy = false;
  const notes = new Map<string, string>();
  const actionFeedback = new Map<string, { message: string; failed: boolean }>();
  const quickActions = new Map<string, { action: string; revision: number }>();
  const expandedActions = new Map<string, { action: string; revision: number }>();
  const rows = new Map<string, { key: string; elements: HTMLTableRowElement[] }>();
  let deferred = false;
  let pointerTarget: Node | null = null;
  const tableWrap = document.createElement('div');
  tableWrap.className = 'task-table-scroll';
  const table = document.createElement('table');
  table.className = 'task-table';
  const caption = table.createCaption();
  const head = table.createTHead().insertRow();
  for (const name of ['Task', 'State', 'Category', 'Classification', 'Confidence / readiness', 'Owner']) {
    const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = name; head.append(cell);
  }
  const body = table.createTBody();
  tableWrap.append(table);
  list.append(tableWrap);
  const flush = () => { if (deferred) render(true); };
  host.addEventListener('focusout', () => setTimeout(flush, 0));
  host.addEventListener('pointerdown', (event) => { pointerTarget = event.target as Node; }, true);
  for (const name of ['pointerup', 'pointercancel']) {
    document.addEventListener(name, () => {
      pointerTarget = null;
      setTimeout(flush, 0);
    }, true);
  }
  document.addEventListener('selectionchange', flush);
  type ClassificationDraft = { revision: number; route: Task['route']; classificationConfidence: number; readinessConfidence: number; reason: string };
  const drafts = new Map<string, ClassificationDraft>();
  const quickClassifications = new Set<string>();
  const routes: Array<[Task['route'], string]> = [
    ['agent', 'Fully agent-executable'], ['agent_preparable', 'Agent-preparable'],
    ['human_delegable', 'Human-delegable'], ['you', 'Personal'], ['verify_first', 'Verify first'],
  ];
  const canClassify = (task: Task) => !['in_progress', 'ready_for_review', 'completed', 'cancelled'].includes(task.state);
  function revisionFor(task: Task, renderedRevision: number): number {
    return snapshot.tasks.find((current) => current.id === task.id)?.revision === task.revision
      ? snapshot.revision : renderedRevision;
  }
  function classificationDraft(task: Task, renderedRevision: number): ClassificationDraft {
    return drafts.get(task.id) ?? {
      revision: revisionFor(task, renderedRevision), route: task.route,
      classificationConfidence: task.classificationConfidence, readinessConfidence: task.readinessConfidence, reason: '',
    };
  }
  async function saveClassification(task: Task, draft: ClassificationDraft): Promise<void> {
    busy = true;
    render();
    try {
      const { revision, reason: note, ...patch } = draft;
      snapshot = await command('classify', task.id, revision, note, patch);
      drafts.delete(task.id);
      quickClassifications.delete(task.id);
      actionFeedback.delete(task.id);
      error.textContent = '';
    } catch (failure) {
      error.textContent = `${failure instanceof Error ? failure.message : String(failure)}. Discard classification edits to use current values.`;
    } finally { busy = false; render(); }
  }
  function renderQuickClassification(task: Task): HTMLElement {
    const renderedRevision = snapshot.revision;
    const cell = document.createElement('div');
    cell.className = 'task-quick-state';
    const select = document.createElement('select');
    select.id = `task-quick-classification-${task.id}`;
    select.setAttribute('aria-label', `Quick classification for ${task.id}`);
    for (const [value, text] of routes) {
      const option = document.createElement('option');
      option.value = value; option.textContent = text; select.append(option);
    }
    const pending = quickClassifications.has(task.id);
    select.value = pending ? drafts.get(task.id)!.route : task.route;
    select.disabled = busy || !canClassify(task);
    select.addEventListener('change', () => {
      const draft = classificationDraft(task, renderedRevision);
      draft.route = select.value as Task['route'];
      drafts.set(task.id, draft);
      quickClassifications.add(task.id);
      quickActions.delete(task.id);
      if (expanded === task.id) expanded = '';
      render();
      document.getElementById(`task-quick-classification-reason-${task.id}`)?.focus();
    });
    cell.append(select);
    if (pending) {
      const draft = drafts.get(task.id)!;
      const reason = document.createElement('textarea');
      reason.id = `task-quick-classification-reason-${task.id}`;
      reason.setAttribute('aria-label', `Classification reason for ${task.id}`);
      reason.placeholder = 'Classification reason (optional)';
      reason.value = draft.reason;
      reason.disabled = busy;
      reason.addEventListener('input', () => { draft.reason = reason.value; });
      const notice = document.createElement('p');
      notice.textContent = 'Unsaved · saving clears prior approval.';
      const save = document.createElement('button');
      save.className = 'btn'; save.textContent = 'Save classification'; save.disabled = busy || !canClassify(task);
      save.addEventListener('click', () => void saveClassification(task, draft));
      const discard = document.createElement('button');
      discard.className = 'btn'; discard.textContent = 'Discard classification edits'; discard.disabled = busy;
      discard.addEventListener('click', () => {
        drafts.delete(task.id); quickClassifications.delete(task.id); error.textContent = ''; render();
      });
      cell.append(reason, notice, save, discard);
    }
    return cell;
  }
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
    quickClassifications.delete(id);
    expanded = id;
    filter.value = 'all';
    categoryFilter.value = '';
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
    if (task.state === 'awaiting_approval') actions.push(['approve', 'Approve preparation'], ['approve_with_note', 'Ready with note']);
    if (['needs_input', 'blocked'].includes(task.state)) actions.push(['resume', 'Answer / resume']);
    if (task.state === 'ready_for_review') actions.push(['accept', 'Accept outcome'], ['request_changes', 'Request changes']);
    else if (!['completed', 'cancelled'].includes(task.state)) actions.push(['accept', 'Mark outcome complete']);
    if (!['completed', 'cancelled'].includes(task.state)) actions.push(['cancel', 'Cancel task']);
    return actions;
  }
  function appendActionFeedback(parent: HTMLElement, task: Task): void {
    const feedback = actionFeedback.get(task.id);
    if (!feedback) return;
    const message = document.createElement('p');
    message.className = feedback.failed ? 'task-action-error' : 'task-action-notification';
    message.textContent = feedback.message;
    parent.append(message);
  }
  async function runAction(task: Task, action: string, expectedRevision: number): Promise<void> {
    const text = notes.get(task.id) ?? '';
    let preparation: PreparationScope | undefined;
    if (action === 'approve' || action === 'approve_with_note') {
      preparation = proposedPreparation(task);
      const approved = preparation ?? task;
      const accepted = await modalConfirm({
        title: 'Approve preparation',
        body: `${preparation ? 'This task has no agent permissions. Approve the following preparation-only scope instead of executing the underlying obligation.\n\n' : ''}${approved.scope}\n\nPermissions: ${approved.permissions.join(', ')}\n\nAcceptance: ${approved.acceptance}\n\nStop: ${approved.stopCondition}\n\nReadiness estimate: ${task.readinessConfidence}% (advisory)\n\n${text.trim() ? `Your note to the worker: ${text.trim()}\n\n` : ''}Approval notifies the owning main session and authorizes its configured workflow to start scoped preparation. External actions and final outcome acceptance remain unauthorized.`,
        primaryLabel: 'Approve preparation',
      });
      if (!accepted) return;
    }
    busy = true;
    render();
    try {
      snapshot = await command(action === 'approve_with_note' ? 'approve' : action, task.id, expectedRevision, text, preparation);
      notes.delete(task.id);
      quickActions.delete(task.id);
      expandedActions.delete(task.id);
      actionFeedback.delete(task.id);
      error.textContent = snapshot.notification ?? '';
      if (snapshot.notification) actionFeedback.set(task.id, { message: snapshot.notification, failed: false });
    } catch (failure) {
      error.textContent = failure instanceof Error ? failure.message : String(failure);
      actionFeedback.set(task.id, { message: error.textContent, failed: true });
    } finally {
      busy = false;
      render();
    }
  }
  function renderQuickState(task: Task): HTMLElement {
    const renderedRevision = snapshot.revision;
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
    const destinations: Record<string, string> = { approve: 'ready', approve_with_note: 'ready', resume: 'ready', request_changes: 'awaiting approval', accept: 'completed', cancel: 'cancelled' };
    for (const [action, text] of actions) {
      const option = document.createElement('option');
      option.value = action; option.textContent = `${destinations[action]} · ${text}`;
      select.append(option);
    }
    select.disabled = busy || !actions.length;
    const pending = quickActions.get(task.id);
    const selected = actions.find(([action]) => action === pending?.action);
    select.value = selected?.[0] ?? '';
    select.addEventListener('change', () => {
      if (select.value) quickActions.set(task.id, { action: select.value, revision: revisionFor(task, renderedRevision) });
      else quickActions.delete(task.id);
      quickClassifications.delete(task.id);
      if (expanded === task.id) expanded = '';
      render();
      (document.getElementById(`task-quick-note-${task.id}`) ?? document.getElementById(`task-quick-apply-${task.id}`) ?? document.getElementById(select.id))?.focus();
    });
    cell.append(select);
    if (selected && pending) {
      const note = document.createElement('textarea');
      note.id = `task-quick-note-${task.id}`;
      note.setAttribute('aria-label', `Quick action evidence for ${task.id}`);
      note.placeholder = 'Note to the agent, answer, or feedback (optional)';
      note.value = notes.get(task.id) ?? '';
      note.disabled = busy;
      note.addEventListener('input', () => notes.set(task.id, note.value));
      cell.append(note);
      const apply = document.createElement('button');
      apply.id = `task-quick-apply-${task.id}`;
      apply.className = 'btn'; apply.textContent = selected[1];
      apply.disabled = busy;
      apply.addEventListener('click', () => void runAction(task, selected[0], pending.revision));
      const cancel = document.createElement('button');
      cancel.className = 'btn'; cancel.textContent = 'Cancel quick action'; cancel.disabled = busy;
      cancel.addEventListener('click', () => { quickActions.delete(task.id); render(); document.getElementById(select.id)?.focus(); });
      const notice = document.createElement('p');
      notice.textContent = `Current: ${label(task.state)}. ${selected[0].startsWith('approve') ? 'Review scope in the confirmation. Approval may start scoped preparation through the owning main session.' : 'Note optional.'}`;
      cell.append(notice, apply, cancel);
    }
    appendActionFeedback(cell, task);
    return cell;
  }
  function renderTask(task: Task): HTMLElement {
    const renderedRevision = snapshot.revision;
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
    if (task.route === 'human_delegable') detail(details, 'Human delegation', `${task.candidateDelegate || 'Unassigned'} · ${task.handoff}`);
    detail(details, 'Confidence', `classification ${task.classificationConfidence}% · readiness ${task.readinessConfidence}%`);
    if (canClassify(task)) {
      const editor = document.createElement('details');
      const editorTitle = document.createElement('summary');
      editorTitle.textContent = 'Edit classification';
      editor.open = drafts.has(task.id);
      editor.append(editorTitle);
      const draft = classificationDraft(task, renderedRevision);
      const remember = () => {
        if (!drafts.has(task.id)) draft.revision = revisionFor(task, renderedRevision);
        drafts.set(task.id, draft);
      };
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
      for (const [key, name] of [['classificationConfidence', 'Classification confidence (%)'], ['readinessConfidence', 'Readiness confidence (%)']] as const) {
        const input = document.createElement('input');
        input.type = 'number'; input.min = '0'; input.max = '100'; input.step = '1';
        input.value = String(draft[key]); input.required = true; input.disabled = busy;
        input.addEventListener('input', () => { draft[key] = input.valueAsNumber; remember(); });
        field(name, input);
      }
      const reason = document.createElement('textarea');
      reason.value = draft.reason;
      reason.placeholder = 'Classification reason (optional)';
      reason.disabled = busy;
      reason.addEventListener('input', () => { draft.reason = reason.value; remember(); });
      field('Classification reason', reason);
      const notice = document.createElement('p');
      notice.textContent = 'Saving clears prior approval. Active work and outcomes under review must be stopped or returned for changes before reclassification.';
      editor.append(notice);
      const save = document.createElement('button');
      save.className = 'btn'; save.textContent = 'Save classification'; save.disabled = busy;
      save.addEventListener('click', async () => {
        if (!Array.from(editor.querySelectorAll('input')).every((input) => input.reportValidity())) {
          error.textContent = 'Confidence values from 0 to 100 are required.';
          return;
        }
        await saveClassification(task, draft);
      });
      const discard = document.createElement('button');
      discard.className = 'btn'; discard.textContent = 'Discard classification edits'; discard.disabled = busy;
      discard.addEventListener('click', () => { drafts.delete(task.id); quickClassifications.delete(task.id); error.textContent = ''; render(); });
      editor.append(save, discard);
      details.append(editor);
    }
    detail(details, 'Scope', task.scope);
    detail(details, 'Permissions', task.permissions.join(', '));
    detail(details, 'Acceptance', task.acceptance);
    detail(details, 'Stop', task.stopCondition);
    detail(details, 'Dependencies', task.dependencies.join(', '));
    detail(details, 'Progress', task.progress);
    detail(details, 'User note to worker', task.approvalNote ?? '');
    detail(details, 'Dispatch claim', task.dispatch?.id ?? '');
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
    note.placeholder = 'Note to the agent, answer, or feedback (optional)';
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
      option.disabled = busy;
      transition.append(option);
      const button = document.createElement('button');
      button.className = 'btn';
      button.textContent = text;
      button.hidden = expandedActions.get(task.id)?.action !== action;
      button.disabled = busy;
      button.addEventListener('click', () => void runAction(task, action, expandedActions.get(task.id)?.revision ?? revisionFor(task, renderedRevision)));
      details.append(button);
      actionButtons.push(button);
    }
    if (actions.length) {
      transition.value = expandedActions.get(task.id)?.action ?? '';
      transition.disabled = busy;
      transition.addEventListener('change', () => {
        if (transition.value) expandedActions.set(task.id, { action: transition.value, revision: revisionFor(task, renderedRevision) });
        else expandedActions.delete(task.id);
        actionButtons.forEach((button, index) => { button.hidden = actions[index]![0] !== transition.value; });
      });
      details.insertBefore(transition, note);
    }
    appendActionFeedback(details, task);
    row.append(jump, status, details);
    return row;
  }
  function render(background = false): void {
    const view = document.defaultView;
    const scroll = { left: view?.scrollX ?? 0, top: view?.scrollY ?? 0 };
    const horizontal = tableWrap.scrollLeft;
    const anchor = background ? Array.from(body.rows).find((row) => {
      const rect = row.getBoundingClientRect();
      return rect.bottom > 0 && rect.top < (view?.innerHeight ?? 0);
    }) : undefined;
    const anchorTop = anchor?.getBoundingClientRect().top ?? 0;
    const anchorId = anchor?.id || anchor?.querySelector('.task-title-button')?.id;
    const selection = document.getSelection();
    const active = document.activeElement;
    const editing = active?.matches('input, textarea, select, [contenteditable="true"]');
    const interacting = (element: HTMLElement) => (editing && element.contains(active)) ||
      (pointerTarget !== null && element.contains(pointerTarget)) ||
      (!!selection && !selection.isCollapsed &&
        (element.contains(selection.anchorNode) || element.contains(selection.focusNode)));
    deferred = false;
    if (!snapshot.enabled) setMode(false);
    toggle.hidden = !snapshot.enabled;
    if (snapshot.error) error.textContent = snapshot.error;
    const categories = new Map<string, string>();
    const sections: Array<{ level: number; title: string }> = [];
    for (const element of doc?.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6, [data-task-id]') ?? []) {
      if (element.dataset.taskId) {
        categories.set(element.dataset.taskId, sections.map((section) => section.title).join(' / ') || 'Uncategorized');
      } else if (element.parentElement === doc) {
        const level = Number(element.tagName.slice(1));
        while (sections.length && sections.at(-1)!.level >= level) sections.pop();
        if (level > 1) sections.push({ level, title: element.textContent?.trim() ?? '' });
      }
    }
    const categoryFor = (task: Task) => categories.get(task.id) ?? 'Uncategorized';
    const selectedCategory = categoryFilter.value;
    const allCategories = [...new Set(snapshot.tasks.map(categoryFor))];
    if (JSON.stringify(Array.from(categoryFilter.options, (option) => option.value)) !== JSON.stringify(['', ...allCategories])) {
      if (background && interacting(categoryFilter)) deferred = true;
      else {
        categoryFilter.replaceChildren();
        for (const value of ['', ...allCategories]) {
          const option = document.createElement('option');
          option.value = value;
          option.textContent = value || 'All categories';
          categoryFilter.append(option);
        }
        categoryFilter.value = allCategories.includes(selectedCategory) ? selectedCategory : '';
      }
    }
    const visible = snapshot.tasks.filter((task) =>
      ((!categoryFilter.value || categoryFor(task) === categoryFilter.value) &&
        (filter.value === 'all' || (filter.value === 'open' ? !['completed', 'cancelled'].includes(task.state) : task.state === filter.value))) ||
      (background && rows.get(task.id)?.elements.some(interacting)));
    const captionText = `${visible.length} tasks · select a task to review or edit`;
    if (caption.textContent !== captionText) caption.textContent = captionText;
    const next: HTMLTableRowElement[] = [];
    for (const task of visible) {
      const key = JSON.stringify([task, categoryFor(task), expanded === task.id, busy]);
      const previous = rows.get(task.id);
      if (background && previous && (previous.key === key || previous.elements.some(interacting))) {
        if (previous.key !== key) deferred = true;
        next.push(...previous.elements);
        continue;
      }
      const openDetails = new Map(previous?.elements.flatMap((row) =>
        Array.from(row.querySelectorAll('details'), (details) => [details.querySelector('summary')?.textContent, details.open] as const)));
      const tr = document.createElement('tr');
      const elements = [tr];
      tr.dataset.selected = String(expanded === task.id);
      const choose = document.createElement('button');
      choose.className = 'task-title-button'; choose.textContent = task.title;
      choose.setAttribute('aria-label', task.title);
      choose.setAttribute('aria-expanded', String(expanded === task.id));
      choose.setAttribute('aria-controls', `task-details-${task.id}`);
      choose.addEventListener('click', () => {
        quickActions.delete(task.id);
        quickClassifications.delete(task.id);
        expanded = expanded === task.id ? '' : task.id;
        render();
        document.getElementById(`task-toggle-${task.id}`)?.focus();
      });
      choose.id = `task-toggle-${task.id}`;
      tr.insertCell().append(choose);
      tr.insertCell().append(renderQuickState(task));
      tr.insertCell().textContent = categoryFor(task);
      tr.insertCell().append(renderQuickClassification(task));
      for (const value of [`${task.classificationConfidence}% / ${task.readinessConfidence}%`, task.owner || 'Unassigned']) {
        tr.insertCell().textContent = value;
      }
      if (expanded === task.id) {
        const detailsRow = document.createElement('tr');
        detailsRow.id = `task-details-${task.id}`;
        detailsRow.className = 'task-detail-row';
        const cell = detailsRow.insertCell();
        cell.colSpan = head.cells.length;
        cell.append(renderTask(task));
        if (background) {
          for (const details of detailsRow.querySelectorAll('details')) {
            const open = openDetails.get(details.querySelector('summary')?.textContent);
            if (open !== undefined) details.open = open;
          }
        }
        elements.push(detailsRow);
      }
      rows.set(task.id, { key, elements });
      next.push(...elements);
    }
    const retained = new Set(next);
    for (const row of Array.from(body.rows)) {
      if (!retained.has(row)) row.remove();
    }
    let cursor = body.firstChild;
    for (const row of next) {
      if (row === cursor) cursor = cursor.nextSibling;
      else body.insertBefore(row, cursor);
    }
    while (cursor) {
      const removed = cursor;
      cursor = cursor.nextSibling;
      removed.remove();
    }
    for (const id of rows.keys()) {
      if (!visible.some((task) => task.id === id)) rows.delete(id);
    }
    if (background) {
      if (tableWrap.scrollLeft !== horizontal) tableWrap.scrollLeft = horizontal;
      const currentAnchor = anchorId ? document.getElementById(anchorId)?.closest('tr') : anchor;
      const delta = currentAnchor?.isConnected ? currentAnchor.getBoundingClientRect().top - anchorTop : 0;
      if (view && (view.scrollX !== scroll.left || view.scrollY !== scroll.top + delta)) {
        view.scrollTo({ left: scroll.left, top: scroll.top + delta, behavior: 'instant' });
      }
    }
    for (const task of snapshot.tasks) {
      const target = document.getElementById(`task-${task.id}`);
      if (!target) continue;
      let badge = target.querySelector<HTMLElement>(':scope > .task-live-status');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'task-live-status';
        target.append(badge);
      }
      const text = ` · Current: ${label(task.state)}`;
      if (badge.textContent !== text) badge.textContent = text;
      const checkbox = target.querySelector<HTMLInputElement>('input[type="checkbox"]');
      if (checkbox) { checkbox.checked = task.state === 'completed'; checkbox.disabled = false; checkbox.title = 'Open task controls to review or change this task'; }
    }
  }
  filter.addEventListener('change', () => render());
  categoryFilter.addEventListener('change', () => render());
  return { update(value) { snapshot = value; error.textContent = value.error ?? ''; render(true); }, refresh: () => render(true) };
}
