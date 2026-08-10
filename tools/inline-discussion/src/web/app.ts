// src/web/app.ts
import DOMPurify from 'dompurify';
import { Marked } from 'marked';
import hljs from 'highlight.js/lib/common';
import mermaid from 'mermaid';
import type { AgentActivity, ApplyProgress, ApplyTask, Highlight, Thread } from '../types.ts';
import {
  composerKeyAction,
  composerNoteModifierActive,
  detectComposerPlatform,
} from './composer-shortcuts.ts';
import { modalChoice, modalConfirm, modalStatus, type ModalStatusHandle } from './modal.ts';
import { scrollToFragment } from './navigation.ts';
import { calculateOverlayPlacement } from './overlay-position.ts';
import { quoteOccurrence } from './quote-position.ts';
import { appendQuoteToTextarea } from './quote-insertion.ts';
import { updateBlockNoteIndicator } from './block-note-indicator.ts';
import { isArchivedThreadDuplicate } from './thread-dedup.ts';
import { findThreadDetails } from './thread-details.ts';
import { installImageViewer } from './image-viewer.ts';
import { blockPlusHost } from './block-plus-host.ts';
import {
  awaitsMermaidRender,
  captureDiagramSource,
  mermaidThemeFor,
  pendingDiagrams,
  renderedDiagrams,
  restoreDiagramSource,
} from './mermaid-diagrams.ts';
import { installShiftArrowTextareaSelection } from './textarea-selection.ts';

// Dedicated marked instance for rendering thread messages. GFM on so tables +
// fenced code work. `breaks: true` so assistant single-newlines survive as
// <br> inside paragraphs (matches how assistant replies flow).
const msgMarked = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    code(code: string, infostring?: string): string {
      const lang = (infostring ?? '').trim().split(/\s+/)[0] || undefined;
      if (lang?.toLowerCase() === 'mermaid') {
        return `<pre class="mermaid">${escapeHtml(code)}</pre>\n`;
      }
      const language = lang && hljs.getLanguage(lang) ? lang : undefined;
      const highlighted = language
        ? hljs.highlight(code, { language }).value
        : hljs.highlightAuto(code).value;
      const cls = language ? `hljs language-${language}` : 'hljs';
      return `<pre><code class="${cls}">${highlighted}</code></pre>\n`;
    },
  },
});

function initializeMermaid(): void {
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: mermaidThemeFor(document.documentElement.dataset.theme),
  });
}

initializeMermaid();

let mermaidRenderQueue: Promise<void> = Promise.resolve();

function scheduleMermaidRender(): void {
  queueMicrotask(() => {
    const nodes = pendingDiagrams(document);
    if (nodes.length === 0) return;
    for (const node of nodes) captureDiagramSource(node);
    mermaidRenderQueue = mermaidRenderQueue.then(async () => {
      const pending = nodes.filter((node) => node.isConnected && !node.dataset.processed);
      if (pending.length === 0) return;
      try {
        await mermaid.run({ nodes: pending, suppressErrors: true });
      } catch (error) {
        console.warn('Failed to render Mermaid diagram', error);
      } finally {
        installBlockPluses();
      }
    });
  });
}

// Rendered SVGs bake in their palette, so a theme switch has to re-run mermaid
// against the stashed source rather than restyling the existing output.
function syncMermaidTheme(): void {
  initializeMermaid();
  const rendered = renderedDiagrams(document);
  if (rendered.length === 0) return;
  for (const node of rendered) restoreDiagramSource(node);
  scheduleMermaidRender();
}

const ACTIVITY_TEXT_LIMIT = 256;

function renderMarkdown(text: string): string {
  const raw = msgMarked.parse(text) as string;
  scheduleMermaidRender();
  return DOMPurify.sanitize(raw, {
    ADD_TAGS: ['details', 'summary'] as string[],
    ADD_ATTR: ['checked', 'disabled', 'type'] as string[],
  });
}

interface Bootstrap {
  html: string;
  blockIds: string[];
  title: string;
  threads: Thread[];
  activeThreads?: string[];
  highlights?: Highlight[];
  archivedThreads: Thread[];
  applying?: boolean;
  applyStatus?: string | null;
  applyProgress?: ApplyProgress | null;
  applyTasks?: ApplyTask[];
  applyAvailable?: boolean;
  hasMainSession?: boolean;
  targetLine?: number | null;
  readOnly?: boolean;
  sourceView?: boolean;
  documentPath?: string;
}

interface Prefs {
  theme?: 'light' | 'dark' | 'auto';
  width?: 'comfortable' | 'full';
}

const state = {
  threads: new Map<string, Thread>(),
  highlights: new Map<string, Highlight>(),
  archived: new Map<string, Thread>(),
  activeThreadId: null as string | null,
  prefs: { theme: 'auto', width: 'comfortable' } as Required<Prefs>,
  applying: false,
  applyProgress: null as ApplyProgress | null,
  applyTasks: [] as ApplyTask[],
  applyAvailable: false,
  // Defaults to true so a missing field (e.g. older server build) keeps the
  // legacy behaviour: Apply visible. The standalone CLI shortcut sets this
  // to false in /api/bootstrap so we hide Apply.
  hasMainSession: true,
  readOnly: false,
  sourceView: false,
  documentPath: '',
};

interface TurnState {
  active: boolean;
  queued: string[];
  startedAt: number;
  heartbeat: number | null;
}

const turnStates = new Map<string, TurnState>();

function getTurnState(threadId: string): TurnState {
  const existing = turnStates.get(threadId);
  if (existing) return existing;
  const created: TurnState = { active: false, queued: [], startedAt: 0, heartbeat: null };
  turnStates.set(threadId, created);
  return created;
}

const NOTE_OVERLAY_HIDE_MS = 1000;
const noteOverlayTimers = new Map<string, number>();
let overlayPositionFrame: number | null = null;

function clearNoteOverlayTimer(card: HTMLElement): void {
  const timer = noteOverlayTimers.get(card.id);
  if (timer === undefined) return;
  window.clearTimeout(timer);
  noteOverlayTimers.delete(card.id);
}

function hideNoteOverlay(card: HTMLElement): void {
  clearNoteOverlayTimer(card);
  card.classList.add('note-hidden');
  card.setAttribute('aria-hidden', 'true');
  positionNoteOverlays();
}

function scheduleNoteOverlayHide(card: HTMLElement): void {
  clearNoteOverlayTimer(card);
  const timer = window.setTimeout(() => {
    if (card.matches(':hover') || card.contains(document.activeElement)) {
      scheduleNoteOverlayHide(card);
      return;
    }
    hideNoteOverlay(card);
  }, NOTE_OVERLAY_HIDE_MS);
  noteOverlayTimers.set(card.id, timer);
}

function revealNoteOverlay(card: HTMLElement): void {
  clearNoteOverlayTimer(card);
  card.classList.remove('note-hidden');
  card.removeAttribute('aria-hidden');
  positionNoteOverlays();
  scheduleNoteOverlayHide(card);
}

function revealNoteOverlaysForBlock(blockId: string): void {
  for (const card of document.querySelectorAll<HTMLElement>(`.note-overlay[data-note-anchor-block-id="${blockId}"]`)) {
    revealNoteOverlay(card);
  }
}

function revealNoteOverlaysForMark(mark: HTMLElement): void {
  for (const threadId of (mark.dataset.threadIds ?? '').split(',').filter(Boolean)) {
    const card = document.getElementById(`thread-${threadId}`);
    if (card?.classList.contains('note-overlay')) revealNoteOverlay(card);
  }
}

function bindNoteOverlayInteractions(card: HTMLElement, anchor: HTMLElement, blockId: string): void {
  if (!card.dataset.noteOverlayBound) {
    card.dataset.noteOverlayBound = '1';
    card.addEventListener('mouseenter', () => clearNoteOverlayTimer(card));
    card.addEventListener('mouseleave', () => scheduleNoteOverlayHide(card));
    card.addEventListener('focusin', () => clearNoteOverlayTimer(card));
    card.addEventListener('focusout', (event) => {
      if (!card.contains(event.relatedTarget as Node | null)) scheduleNoteOverlayHide(card);
    });
  }
  const threadId = card.id.replace(/^thread-/, '');
  const quoteMarks = [...anchor.querySelectorAll<HTMLElement>('.quote-highlight')]
    .filter((mark) => mark.dataset.threadIds?.split(',').includes(threadId));
  const targets = quoteMarks.length > 0 || card.dataset.noteAnchorQuote ? quoteMarks : [anchor];
  for (const target of targets) {
    if (target.dataset.noteRevealBound) continue;
    target.dataset.noteRevealBound = '1';
    const reveal = (): void => {
      if (target.classList.contains('quote-highlight')) revealNoteOverlaysForMark(target);
      else revealNoteOverlaysForBlock(blockId);
    };
    target.addEventListener('mouseenter', reveal);
    target.addEventListener('focusin', reveal);
  }
}

function findQuoteRange(block: HTMLElement, quote: string, occurrence: number): Range | null {
  if (!quote) return null;
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      return (node as Text).parentElement?.closest('.block-plus')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  const segments: Array<{ node: Text; start: number }> = [];
  let flat = '';
  let node: Text | null;
  while ((node = walker.nextNode() as Text | null)) {
    segments.push({ node, start: flat.length });
    flat += node.data;
  }
  let from = 0;
  let foundAt = -1;
  for (let index = 0; index < occurrence; index += 1) {
    foundAt = flat.indexOf(quote, from);
    if (foundAt === -1) return null;
    from = foundAt + quote.length;
  }
  const endAt = foundAt + quote.length;
  const start = segments.find((segment) =>
    segment.start <= foundAt && foundAt < segment.start + segment.node.data.length,
  );
  const end = segments.find((segment) =>
    segment.start < endAt && endAt <= segment.start + segment.node.data.length,
  );
  if (!start || !end) return null;
  const range = document.createRange();
  range.setStart(start.node, foundAt - start.start);
  range.setEnd(end.node, endAt - end.start);
  return range;
}

function overlayTarget(card: HTMLElement, anchor: HTMLElement): { rect: DOMRect; underQuote: boolean } {
  const threadId = card.id.replace(/^thread-/, '');
  const mark = card.classList.contains('note-overlay')
    ? [...anchor.querySelectorAll<HTMLElement>('.quote-highlight')]
      .find((candidate) => candidate.dataset.threadIds?.split(',').includes(threadId))
    : null;
  const composerQuote = card.classList.contains('composer-overlay')
    ? card.dataset.composerAnchorQuote
    : undefined;
  const composerOccurrence = Number.parseInt(card.dataset.composerAnchorOccurrence ?? '1', 10) || 1;
  const composerRange = composerQuote ? findQuoteRange(anchor, composerQuote, composerOccurrence) : null;
  return {
    rect: mark?.getBoundingClientRect() ?? composerRange?.getBoundingClientRect() ?? anchor.getBoundingClientRect(),
    underQuote: Boolean(mark) || Boolean(composerRange) || card.classList.contains('composer-overlay'),
  };
}

function positionNoteOverlays(): void {
  const cards = [...document.querySelectorAll<HTMLElement>('.note-overlay, .composer-overlay')];
  const stackHeights = new Map<string, number>();
  for (const card of cards) {
    const blockId = card.dataset.noteAnchorBlockId;
    if (!blockId) continue;
    const anchor = document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
    if (!anchor) continue;
    const target = overlayTarget(card, anchor);
    const maxWidth = card.classList.contains('composer-overlay')
      ? Math.max(240, Math.min(640, window.innerWidth - 16))
      : Math.max(240, Math.min(560, window.innerWidth - 32));
    const stackKey = `${blockId}:${Math.round(target.rect.top)}:${Math.round(target.rect.bottom)}`;
    const offset = stackHeights.get(stackKey) ?? 0;
    const placement = calculateOverlayPlacement({
      rect: target.rect,
      underQuote: target.underQuote,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      viewportWidth: window.innerWidth,
      width: maxWidth,
      offset,
    });
    card.style.left = `${placement.left}px`;
    card.style.top = `${placement.top}px`;
    card.style.width = `${maxWidth}px`;
    card.style.zIndex = String(40 + cards.indexOf(card));
    if (!card.classList.contains('note-hidden')) stackHeights.set(stackKey, offset + card.offsetHeight + 8);
  }
}

function scheduleOverlayPosition(): void {
  if (overlayPositionFrame !== null) return;
  overlayPositionFrame = window.requestAnimationFrame(() => {
    overlayPositionFrame = null;
    positionNoteOverlays();
  });
}

function mountNoteOverlay(card: HTMLElement, anchor: HTMLElement, blockId: string, quote?: string): void {
  card.classList.add('note-card', 'note-overlay');
  card.dataset.noteAnchorBlockId = blockId;
  if (quote) card.dataset.noteAnchorQuote = quote;
  card.setAttribute('role', 'complementary');
  if (card.parentElement !== document.body) document.body.appendChild(card);
  bindNoteOverlayInteractions(card, anchor, blockId);
  hideNoteOverlay(card);
}

function mountComposerOverlay(
  card: HTMLElement,
  anchor: HTMLElement,
  blockId: string,
  quote?: string,
  occurrence = 1,
): void {
  card.classList.add('composer-overlay');
  card.dataset.noteAnchorBlockId = blockId;
  card.dataset.composerAnchorBlockId = blockId;
  if (quote) card.dataset.composerAnchorQuote = quote;
  card.dataset.composerAnchorOccurrence = String(occurrence);
  document.body.appendChild(card);
  positionNoteOverlays();
}

function appendThreadCardAfterAnchor(card: HTMLElement, anchor: HTMLElement): void {
  if (card.parentElement === anchor.parentElement) return;
  let cursor: Element = anchor;
  while (cursor.nextElementSibling?.classList.contains('thread-card')) cursor = cursor.nextElementSibling;
  cursor.after(card);
}

function unmountNoteOverlay(card: HTMLElement, anchor: HTMLElement): void {
  clearNoteOverlayTimer(card);
  card.classList.remove('note-overlay', 'note-hidden');
  card.removeAttribute('aria-hidden');
  card.removeAttribute('role');
  delete card.dataset.noteAnchorBlockId;
  delete card.dataset.noteAnchorQuote;
  delete card.dataset.noteOverlayBound;
  card.style.removeProperty('left');
  card.style.removeProperty('top');
  card.style.removeProperty('width');
  card.style.removeProperty('z-index');
  appendThreadCardAfterAnchor(card, anchor);
}

function removeNoteOverlays(): void {
  for (const card of document.querySelectorAll<HTMLElement>('.note-overlay')) {
    clearNoteOverlayTimer(card);
    card.remove();
  }
}

function removeComposerOverlays(): void {
  for (const card of document.querySelectorAll<HTMLElement>('.composer-overlay')) card.remove();
}

function listenDocumentEvent<T>(es: EventSource, event: string, handler: (payload: T) => void): void {
  es.addEventListener(event, (raw) => {
    const payload = JSON.parse((raw as MessageEvent).data) as T & { documentPath?: string };
    if (payload.documentPath && payload.documentPath !== state.documentPath) return;
    handler(payload as T);
  });
}

let applyOverlay: ModalStatusHandle | null = null;

init().catch((err) => console.error(err));

async function init(): Promise<void> {
  await loadAndApplyPrefs();
  document.addEventListener('keydown', onGlobalKeyDown);
  document.getElementById('back-to-discussion')!.addEventListener('click', backToDiscussion);
  document.getElementById('theme-toggle')!.addEventListener('click', toggleTheme);
  document.getElementById('width-toggle')!.addEventListener('click', toggleWidth);
  document.getElementById('finish-top')!.addEventListener('click', finish);
  document.getElementById('finish-bottom')!.addEventListener('click', finish);
  document.getElementById('pause-top')!.addEventListener('click', pause);
  document.getElementById('pause-bottom')!.addEventListener('click', pause);
  document.getElementById('apply-top')!.addEventListener('click', onApplyClick);
  document.getElementById('apply-bottom')!.addEventListener('click', onApplyClick);

  const sourcePath = window.location.pathname === '/' ? '' : `?${new URLSearchParams({ path: window.location.pathname }).toString()}`;
  const boot = (await (await fetch(`/api/bootstrap${sourcePath}`)).json()) as Bootstrap;
  applyTitle(boot.title);
  renderDoc(boot.html);
  installImageViewer(document.getElementById('doc')!);
  state.readOnly = boot.readOnly === true;
  state.sourceView = boot.sourceView === true;
  state.documentPath = boot.documentPath ?? (window.location.pathname === '/' ? '' : window.location.pathname);
  state.applyAvailable = boot.applyAvailable ?? boot.threads.length > 0;
  const backButton = document.getElementById('back-to-discussion') as HTMLButtonElement | null;
  if (backButton) backButton.hidden = !state.sourceView;
  if (!state.readOnly) {
    installBlockPluses();
    installThreadQuoteSelection();
  }
  scrollToLine(boot.targetLine);
  for (const t of boot.threads) state.threads.set(t.id, t);
  for (const id of boot.activeThreads ?? []) {
    const turn = getTurnState(id);
    turn.active = true;
    turn.startedAt = Date.now();
  }
  for (const h of boot.highlights ?? []) state.highlights.set(h.id, h);
  for (const t of boot.archivedThreads) state.archived.set(t.id, t);
  renderExistingThreads();
  scrollToLocationHash();
  window.addEventListener('hashchange', scrollToLocationHash);
  window.addEventListener('resize', positionNoteOverlays);
  window.addEventListener('scroll', scheduleOverlayPosition, { passive: true });

  // When the server started without a main host session (standalone CLI),
  // hide the Apply buttons entirely. Apply delegates work to the main
  // agent — without one, the controls have nothing to do.
  state.hasMainSession = boot.hasMainSession !== false;
  if (!state.hasMainSession) {
    for (const id of ['apply-top', 'apply-bottom']) {
      const btn = document.getElementById(id) as HTMLButtonElement | null;
      if (btn) btn.hidden = true;
    }
  }
  if (state.readOnly) {
    for (const id of ['apply-top', 'apply-bottom', 'pause-top', 'pause-bottom', 'finish-top', 'finish-bottom']) {
      const btn = document.getElementById(id) as HTMLButtonElement | null;
      if (btn) btn.hidden = true;
    }
  }

  // Re-open the Apply overlay if a sibling tab kicked off Apply before this
  // page loaded — the server records `applying` in /api/bootstrap so reloading
  // mid-Apply restores the blocking modal instead of dropping into a stale UI.
  if (boot.applying) {
    state.applying = true;
    state.applyProgress = boot.applyProgress ?? null;
    state.applyTasks = boot.applyTasks ?? [];
    showApplyOverlay(boot.applyProgress ?? null, boot.applyStatus ?? 'Applying changes in main session...', state.applyTasks);
    setApplyAndFinishDisabled(true);
  }
  recomputeApplyEnabled();

  // Source-code views remain read-only. Markdown subdocuments participate in
  // the same live annotation stream as the main discussion, scoped by the
  // documentPath field carried on document-specific events.
  if (state.readOnly) return;

  const es = new EventSource(`/events?path=${encodeURIComponent(state.documentPath)}`);
  listenDocumentEvent(es, 'thread.message.delta', onDelta);
  listenDocumentEvent(es, 'thread.message.status', onStatus);
  listenDocumentEvent(es, 'thread.message.activity', onActivity);
  listenDocumentEvent(es, 'thread.message.done', onDone);
  listenDocumentEvent(es, 'thread.message.interrupted', onInterrupted);
  listenDocumentEvent(es, 'thread.message.error', onMessageError);
  listenDocumentEvent(es, 'thread.conclusion.proposed', onConclusion);
  listenDocumentEvent(es, 'thread.closed', onClosed);
  listenDocumentEvent(es, 'thread.deleted', onDeleted);
  listenDocumentEvent(es, 'thread.updated', onUpdated);
  listenDocumentEvent(es, 'thread.created', onThreadCreated);
  listenDocumentEvent(es, 'highlight.created', onHighlightCreated);
  listenDocumentEvent(es, 'highlight.deleted', onHighlightDeleted);
  listenDocumentEvent(es, 'doc.updated', onDocUpdated);
  listenDocumentEvent(es, 'server.finished', onFinished);
  listenDocumentEvent(es, 'server.paused', onPaused);
  es.addEventListener('server.applying', (e) => {
    const payload = JSON.parse((e as MessageEvent).data) as { progress?: ApplyProgress | null; tasks?: ApplyTask[] };
    state.applying = true;
    state.applyProgress = payload.progress ?? null;
    state.applyTasks = payload.tasks ?? state.applyTasks;
    showApplyOverlay(state.applyProgress, state.applyProgress?.status ?? 'Applying changes in main session...', state.applyTasks);
    setApplyAndFinishDisabled(true);
    recomputeApplyEnabled();
  });
  es.addEventListener('server.apply-availability', (e) => {
    const payload = JSON.parse((e as MessageEvent).data) as { applyAvailable?: boolean };
    if (typeof payload.applyAvailable === 'boolean') state.applyAvailable = payload.applyAvailable;
    recomputeApplyEnabled();
  });
  es.addEventListener('server.apply-progress', (e) => {
    const payload = JSON.parse((e as MessageEvent).data) as { progress: ApplyProgress; tasks?: ApplyTask[] };
    state.applying = true;
    state.applyProgress = payload.progress;
    state.applyTasks = payload.tasks ?? state.applyTasks;
    showApplyOverlay(payload.progress, payload.progress.status, state.applyTasks);
    setApplyAndFinishDisabled(true);
    recomputeApplyEnabled();
  });
  es.addEventListener('server.apply-state', (e) => {
    const payload = JSON.parse((e as MessageEvent).data) as {
      applying?: boolean;
      applyStatus?: string | null;
      applyProgress?: ApplyProgress | null;
      applyTasks?: ApplyTask[];
      applyAvailable?: boolean;
    };
    if (typeof payload.applyAvailable === 'boolean') state.applyAvailable = payload.applyAvailable;
    if (payload.applying) {
      state.applying = true;
      state.applyProgress = payload.applyProgress ?? null;
      state.applyTasks = payload.applyTasks ?? [];
      showApplyOverlay(state.applyProgress, payload.applyStatus ?? 'Applying changes in main session...', state.applyTasks);
      setApplyAndFinishDisabled(true);
    } else {
      state.applying = false;
      state.applyProgress = null;
      state.applyTasks = [];
      if (applyOverlay) {
        applyOverlay.dismiss();
        applyOverlay = null;
      }
      setApplyAndFinishDisabled(false);
    }
    recomputeApplyEnabled();
  });
  listenDocumentEvent(es, 'doc.reloaded', (payload: {
      html: string;
      blockIds: string[];
      title?: string;
      archivedThreads?: Thread[];
      applying?: boolean;
      applyProgress?: ApplyProgress | null;
      applyTasks?: ApplyTask[];
  }) => {
    state.threads.clear();
    state.activeThreadId = null;
    state.archived.clear();
    onDocUpdated(payload);
    if (payload.applying === false) {
      state.applying = false;
      state.applyProgress = null;
      state.applyTasks = [];
      if (applyOverlay) {
        applyOverlay.dismiss();
        applyOverlay = null;
      }
      setApplyAndFinishDisabled(false);
      recomputeApplyEnabled();
      return;
    }
    state.applying = payload.applying ?? true;
    state.applyProgress = payload.applyProgress ?? state.applyProgress;
    state.applyTasks = payload.applyTasks ?? state.applyTasks;
    showApplyOverlay(state.applyProgress, state.applyProgress?.status ?? 'Waiting for main session monitoring...', state.applyTasks);
    setApplyAndFinishDisabled(true);
    recomputeApplyEnabled();
  });
  es.addEventListener('server.apply-complete', (e) => {
    const payload = JSON.parse((e as MessageEvent).data) as { tasks?: ApplyTask[]; applyAvailable?: boolean };
    if (payload.tasks && applyOverlay) applyOverlay.setTasks(payload.tasks);
    if (typeof payload.applyAvailable === 'boolean') state.applyAvailable = payload.applyAvailable;
    state.applying = false;
    state.applyProgress = null;
    state.applyTasks = [];
    if (applyOverlay) {
      applyOverlay.dismiss();
      applyOverlay = null;
    }
    setApplyAndFinishDisabled(false);
    recomputeApplyEnabled();
  });
  es.addEventListener('server.apply-failed', (e) => {
    state.applying = false;
    state.applyProgress = null;
    state.applyTasks = [];
    const payload = JSON.parse((e as MessageEvent).data) as { message?: string; error?: string; applyAvailable?: boolean };
    if (typeof payload.applyAvailable === 'boolean') state.applyAvailable = payload.applyAvailable;
    const msg = payload.message ?? payload.error ?? 'Apply failed';
    if (applyOverlay) {
      applyOverlay.setError(msg);
      applyOverlay = null;
    }
    endApplyFlowError();
  });
}

function backToDiscussion(): void {
  window.location.assign('/');
}

function showApplyOverlay(progress: ApplyProgress | null, fallbackStatus: string, tasks: ApplyTask[] = []): void {
  const status = progress?.status ?? fallbackStatus;
  if (!applyOverlay) {
    applyOverlay = modalStatus({
      title: 'Applying',
      initialStatus: status,
      initialProgress: progress,
      initialTasks: tasks,
    });
    return;
  }
  if (tasks.length > 0) applyOverlay.setTasks(tasks);
  else if (progress) applyOverlay.setProgress(progress);
  else applyOverlay.setStatus(status);
}

async function onApplyClick(): Promise<void> {
  // Ask up front whether to wipe the discussion from the doc once changes are
  // applied. Three outcomes: cancel (abort), keep (legacy behaviour), remove
  // (server strips every note/thread in /api/apply/done). Escape/backdrop maps
  // to 'cancel' so a dismissal never silently applies.
  const choice = await modalChoice({
    title: 'Apply changes',
    body:
      'Closed threads and notes will be applied to your session.\n\n' +
      'Do you also want to remove all notes and threads from the document afterwards?',
    options: [
      { label: 'Cancel', value: 'cancel' },
      { label: 'Apply & keep', value: 'keep' },
      { label: 'Apply & remove all', value: 'remove', variant: 'danger' },
    ],
    cancelValue: 'cancel',
  });
  if (choice === null || choice === 'cancel') return;
  const removeThreads = choice === 'remove';

  setApplyAndFinishDisabled(true);
  applyOverlay = modalStatus({
    title: 'Applying',
    initialStatus: 'Closing threads and notes...',
  });
  try {
    const r = await fetch('/api/apply', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ removeThreads }),
    });
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))) as { error?: string; message?: string };
      if (applyOverlay) applyOverlay.setError(err.message ?? err.error ?? `HTTP ${r.status}`);
      applyOverlay = null;
      endApplyFlowError();
      return;
    }
    // From here on, server.applying / doc.reloaded / server.apply-failed
    // SSE frames drive the overlay state to completion.
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (applyOverlay) applyOverlay.setError(msg);
    applyOverlay = null;
    endApplyFlowError();
  }
}

function setApplyAndFinishDisabled(disabled: boolean): void {
  for (const id of ['apply-top', 'apply-bottom', 'pause-top', 'pause-bottom', 'finish-top', 'finish-bottom']) {
    const btn = document.getElementById(id) as HTMLButtonElement | null;
    if (btn) btn.disabled = disabled;
  }
}

// Re-enable Finish unconditionally, then derive Apply state from live-thread
// presence via recomputeApplyEnabled(). Used in the three Apply error paths
// (HTTP non-2xx, fetch reject, server.apply-failed SSE) so an empty session
// can't keep Apply clickable for spam-clicks against /api/apply.
function endApplyFlowError(): void {
  for (const id of ['pause-top', 'pause-bottom', 'finish-top', 'finish-bottom']) {
    const btn = document.getElementById(id) as HTMLButtonElement | null;
    if (btn) btn.disabled = false;
  }
  recomputeApplyEnabled();
}

function setApplyEnabled(enabled: boolean): void {
  for (const id of ['apply-top', 'apply-bottom']) {
    const btn = document.getElementById(id) as HTMLButtonElement | null;
    if (!btn) continue;
    btn.disabled = !enabled;
    btn.title = enabled ? '' : 'Nothing to apply yet.';
  }
}

function recomputeApplyEnabled(): void {
  // Standalone CLI mode: no main host agent to delegate Apply to. Hide and
  // skip the rest. (Buttons were already hidden in init(); this is the
  // safety net for any later state change.)
  if (!state.hasMainSession) {
    setApplyEnabled(false);
    return;
  }
  if (state.applying) {
    setApplyEnabled(false);
    return;
  }
  // Mirror the server's /api/apply gate (liveThreads.size === 0): closed
  // threads remain in liveThreads until apply/finish runs, so they are valid
  // signal payloads. Disabling Apply once everything is closed traps users.
  const hasLive = state.applyAvailable || state.threads.size > 0;
  const hasProposed = document.querySelector('.conclusion-edit') != null;
  setApplyEnabled(hasLive || hasProposed);
}

async function loadAndApplyPrefs(): Promise<void> {
  try {
    const r = await fetch('/api/prefs');
    if (r.ok) {
      const p = (await r.json()) as Prefs;
      if (p.theme) state.prefs.theme = p.theme;
      if (p.width) state.prefs.width = p.width;
    }
  } catch {
    // server-side prefs unavailable — keep defaults
  }
  applyTheme();
  applyWidth();
}

function applyTheme(): void {
  const t = state.prefs.theme;
  document.documentElement.dataset.theme = t === 'auto'
    ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : t;
  const btn = document.getElementById('theme-toggle')!;
  btn.textContent = document.documentElement.dataset.theme === 'dark' ? '☀' : '☾';
  btn.setAttribute('aria-label', `Theme: ${t}`);
  syncMermaidTheme();
}

function applyWidth(): void {
  const w = state.prefs.width;
  document.documentElement.dataset.width = w;
  const btn = document.getElementById('width-toggle')!;
  btn.innerHTML = w === 'full'
    ? '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2 5h16M2 10h16M2 15h16" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg><span>Full</span>'
    : '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5h10M5 10h10M5 15h10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg><span>Comfortable</span>';
  positionNoteOverlays();
}

async function savePrefs(patch: Prefs): Promise<void> {
  Object.assign(state.prefs, patch);
  try {
    await fetch('/api/prefs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    });
  } catch (err) {
    console.warn('Failed to persist prefs', err);
  }
}

function toggleTheme(): void {
  const cur = document.documentElement.dataset.theme;
  const next: 'light' | 'dark' = cur === 'dark' ? 'light' : 'dark';
  state.prefs.theme = next;
  applyTheme();
  void savePrefs({ theme: next });
}

function toggleWidth(): void {
  const next: 'comfortable' | 'full' = state.prefs.width === 'comfortable' ? 'full' : 'comfortable';
  state.prefs.width = next;
  applyWidth();
  void savePrefs({ width: next });
}

function renderDoc(html: string): void {
  const clean = DOMPurify.sanitize(html, {
    ADD_TAGS: ['details', 'summary', 'del', 's', 'strike'] as string[],
    ADD_ATTR: ['data-block-id', 'data-thread-id', 'checked', 'disabled', 'type'] as string[],
  });
  document.getElementById('doc')!.innerHTML = clean;
  scheduleMermaidRender();
}

function scrollToLine(line: number | null | undefined): void {
  if (!line) return;
  const target = document.querySelector<HTMLElement>(`[data-block-id="line-${line}"]`);
  if (!target) return;
  target.classList.add('source-line-target');
  target.scrollIntoView({ block: 'center' });
}

function scrollToLocationHash(): void {
  scrollToFragment(window.location.hash, document);
}

// Wrap the anchor quote for every thread/note (and standalone highlight) in
// a <mark> inside the doc so the user can see which substring each
// thread/highlight originated from. Safe to call repeatedly — existing
// highlights are stripped first, so this function is idempotent across doc
// re-renders and thread/highlight state changes.
function applyQuoteHighlights(): void {
  // Unwrap previous highlights and re-merge the text nodes they split.
  const prev = document.querySelectorAll<HTMLElement>('.quote-highlight');
  const touchedParents = new Set<Node>();
  for (const mark of prev) {
    const parent = mark.parentNode;
    if (!parent) continue;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    touchedParents.add(parent);
  }
  for (const p of touchedParents) (p as Element).normalize?.();

  const all = [...state.threads.values(), ...state.archived.values()];
  const blockNoteCounts = new Map<string, number>();
  for (const t of all) {
    if (
      t.kind === 'note' &&
      t.status === 'open' &&
      !t.anchor.quote &&
      (t.documentPath ?? state.documentPath) === state.documentPath
    ) {
      blockNoteCounts.set(t.anchor.blockId, (blockNoteCounts.get(t.anchor.blockId) ?? 0) + 1);
    }
    if (!t.anchor.quote) continue;
    const block = document.querySelector<HTMLElement>(
      `[data-block-id="${t.anchor.blockId}"]`,
    );
    if (!block) continue;
    highlightNthOccurrence(block, t.anchor.quote, t.anchor.occurrence ?? 1, { kind: t.kind, threadId: t.id });
  }
  for (const h of state.highlights.values()) {
    if (!h.anchor.quote) continue;
    const block = document.querySelector<HTMLElement>(
      `[data-block-id="${h.anchor.blockId}"]`,
    );
    if (!block) continue;
    highlightNthOccurrence(block, h.anchor.quote, h.anchor.occurrence ?? 1, { kind: 'highlight', highlightId: h.id });
  }
  for (const block of document.querySelectorAll<HTMLElement>('[data-block-id]')) {
    const blockId = block.dataset.blockId;
    if (!blockId || blockNoteCounts.has(blockId)) continue;
    updateBlockNoteIndicator(block, 0, () => undefined);
  }
  for (const [blockId, count] of blockNoteCounts) {
    const block = document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
    if (block) updateBlockNoteIndicator(block, count, () => revealNoteOverlaysForBlock(blockId));
  }
  for (const card of document.querySelectorAll<HTMLElement>('.note-overlay')) {
    const blockId = card.dataset.noteAnchorBlockId;
    const anchor = blockId ? document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`) : null;
    if (blockId && anchor) bindNoteOverlayInteractions(card, anchor, blockId);
  }
  positionNoteOverlays();
}

// Walk text nodes inside `block` until we land inside the Nth occurrence of
// `quote` and wrap that range in a <mark>. Only quotes that fall inside a
// single text node are wrapped — cross-element quotes (e.g. selection that
// crosses a `<code>` span) are skipped silently because `surroundContents`
// rejects them; the thread card still renders its “quote” label, the doc
// just won't carry an inline highlight for it.
//
// Threads that share the same blockId + quote piggyback onto the mark created
// by the first thread — nesting <mark>s would either fail surroundContents or
// produce visually stacked highlights, and attaching multiple ids lets the
// click handler scroll to any of the owning threads.
interface MarkOwner {
  kind: 'thread' | 'note' | 'highlight';
  threadId?: string;
  highlightId?: string;
}

function highlightNthOccurrence(
  block: HTMLElement,
  quote: string,
  occurrence: number,
  owner: MarkOwner,
): void {
  // Build a flat sequence of text nodes inside the block along with the
  // running character offset. We need this because a selection can cross
  // inline element boundaries (e.g. `foo <code>bar</code> baz`), and
  // surroundContents rejects ranges that span multiple elements. We locate
  // the Nth occurrence in the concatenated text, then wrap each touched
  // text node's contribution in its own <mark>, sharing the same owner.
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      // Skip text inside the block-plus button SVG (none today, but defensive).
      // We DO walk into existing .quote-highlight marks so threads sharing a
      // quote with an already-wrapped thread can still find their target.
      const parent = (node as Text).parentElement;
      if (parent?.closest('.block-plus')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const segments: Array<{ node: Text; start: number }> = [];
  let flat = '';
  let node: Text | null;
  while ((node = walker.nextNode() as Text | null)) {
    segments.push({ node, start: flat.length });
    flat += node.data;
  }
  if (segments.length === 0) return;

  // Find the Nth occurrence in the flat text.
  let from = 0;
  let foundAt = -1;
  for (let i = 0; i < occurrence; i += 1) {
    foundAt = flat.indexOf(quote, from);
    if (foundAt === -1) return;
    from = foundAt + quote.length;
  }
  if (foundAt === -1) return;
  const endAt = foundAt + quote.length;

  // If the start of the match already sits inside an existing quote-highlight,
  // piggyback onto it instead of nesting a new mark. Matches the previous
  // behaviour for threads that share an anchor.
  for (const seg of segments) {
    if (seg.start <= foundAt && foundAt < seg.start + seg.node.data.length) {
      const existing = seg.node.parentElement?.closest<HTMLElement>('.quote-highlight');
      if (existing) {
        attachOwnerToQuoteMark(existing, owner);
        return;
      }
      break;
    }
  }

  const title =
    owner.kind === 'note' ? 'Note anchor' :
    owner.kind === 'highlight' ? 'Highlight (hover to convert)' :
    'Thread anchor';
  const className = `quote-highlight quote-highlight-${owner.kind}`;

  // Wrap each contributing text node's portion in its own <mark>. We iterate
  // over a snapshot of `segments` because surroundContents mutates the DOM
  // (it replaces the text node with a <mark> wrapping a clone).
  for (const seg of [...segments]) {
    const nodeStart = seg.start;
    const nodeEnd = nodeStart + seg.node.data.length;
    const overlapStart = Math.max(foundAt, nodeStart);
    const overlapEnd = Math.min(endAt, nodeEnd);
    if (overlapEnd <= overlapStart) continue;
    const localStart = overlapStart - nodeStart;
    const localEnd = overlapEnd - nodeStart;
    // Skip fragments that contribute only whitespace. Markdown-rendered HTML
    // contains whitespace-only text nodes between block-level elements
    // (newlines from formatting) — wrapping them in a <mark> renders as a
    // visible empty highlighted line below the actual text.
    const fragment = seg.node.data.slice(localStart, localEnd);
    if (!fragment.trim()) continue;
    const range = document.createRange();
    range.setStart(seg.node, localStart);
    range.setEnd(seg.node, localEnd);
    const mark = document.createElement('mark');
    mark.className = className;
    mark.title = title;
    try {
      range.surroundContents(mark);
    } catch {
      // Some inline parent (e.g. <a>) makes the range non-surroundable —
      // skip just this fragment, leave the rest highlighted.
      continue;
    }
    attachOwnerToQuoteMark(mark, owner);
  }
}

// Record `owner` on `mark` and install a click handler once.
//   - Thread/note marks: clicking scrolls to the first owning card in the DOM.
//   - Highlight marks: clicking opens the convert popup (to thread / to note
//     / remove). Hovering also shows the same popup so the user does not have
//     to click to discover the actions.
function attachOwnerToQuoteMark(mark: HTMLElement, owner: MarkOwner): void {
  if (owner.kind === 'highlight' && owner.highlightId) {
    mark.dataset.highlightId = owner.highlightId;
    if (!mark.dataset.hoverBound) {
      mark.dataset.hoverBound = '1';
      mark.addEventListener('mouseenter', () => {
        const id = mark.dataset.highlightId;
        if (id) showHighlightActions(mark, id);
      });
    }
    if (!mark.dataset.highlightClickBound) {
      mark.dataset.highlightClickBound = '1';
      mark.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = mark.dataset.highlightId;
        if (id) showHighlightActions(mark, id);
      });
    }
    return;
  }

  if (!owner.threadId) return;
  const current = (mark.dataset.threadIds ?? '').split(',').filter(Boolean);
  if (!current.includes(owner.threadId)) current.push(owner.threadId);
  mark.dataset.threadIds = current.join(',');
  // Keep data-thread-id in sync with the first owner so the CSS selector
  // `#doc [data-thread-id]` and any external readers still match.
  if (!mark.dataset.threadId) mark.dataset.threadId = owner.threadId;
  if (mark.dataset.threadClickBound) return;
  mark.dataset.threadClickBound = '1';
  mark.addEventListener('click', (e) => {
    e.stopPropagation();
    const ids = (mark.dataset.threadIds ?? '').split(',').filter(Boolean);
    for (const id of ids) {
      const card = document.getElementById(`thread-${id}`);
      if (!card) {
        const thread = state.threads.get(id) ?? state.archived.get(id);
        if (!thread) continue;
        const detail = findThreadDetails(
          document.querySelectorAll<HTMLDetailsElement>('#doc details'),
          thread,
        );
        if (!detail) continue;
        detail.open = true;
        detail.scrollIntoView({ behavior: 'smooth', block: 'center' });
        detail.classList.add('thread-flash');
        setTimeout(() => detail.classList.remove('thread-flash'), 900);
        return;
      }
      if (card.classList.contains('note-overlay')) revealNoteOverlay(card);
      const noteAnchor = card.classList.contains('note-overlay')
        ? document.querySelector<HTMLElement>(`[data-block-id="${card.dataset.noteAnchorBlockId}"]`)
        : null;
      if (noteAnchor) noteAnchor.scrollIntoView({ behavior: 'smooth', block: 'center' });
      else card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('thread-flash');
      setTimeout(() => card.classList.remove('thread-flash'), 900);
      return;
    }
  });
}

function installBlockPluses(): void {
  for (const el of document.querySelectorAll<HTMLElement>('[data-block-id]')) {
    if (awaitsMermaidRender(el)) continue;
    const host = blockPlusHost(el);
    if (host.querySelector('.block-plus')) continue;
    const btn = document.createElement('button');
    btn.className = 'block-plus';
    btn.setAttribute('aria-label', 'Start thread on this block');
    btn.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openComposer(el.dataset.blockId!, undefined);
    });
    host.appendChild(btn);
  }
  installRangeSelection();
}

let rangeSelectionInstalled = false;
function installRangeSelection(): void {
  if (rangeSelectionInstalled) return;
  rangeSelectionInstalled = true;
  document.getElementById('doc')!.addEventListener('mouseup', () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    const segments = computeSelectionSegments(range);
    if (segments.length === 0) return;
    showFloatingSelectionActions(range, segments);
  });
}

// Slice the selection range into per-block (blockId, quote) segments. A
// selection that stays inside one block returns a single entry; a multi-
// paragraph selection returns one entry per block it touches so the highlight
// can span them. Whitespace-only segments are dropped — those happen at the
// edges of a multi-block selection when the range starts/ends on a block
// boundary.
interface SelectionSegment { blockId: string; quote: string; occurrence: number }

function blockText(block: HTMLElement): string {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      return (node as Text).parentElement?.closest('.block-plus')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  let text = '';
  let node: Text | null;
  while ((node = walker.nextNode() as Text | null)) text += node.data;
  return text;
}

function rangeStartOffset(block: HTMLElement, range: Range): number {
  const prefix = document.createRange();
  prefix.selectNodeContents(block);
  prefix.setEnd(range.startContainer, range.startOffset);
  return prefix.toString().length;
}

function computeSelectionSegments(range: Range): SelectionSegment[] {
  const docEl = document.getElementById('doc');
  if (!docEl) return [];
  const blocks = Array.from(docEl.querySelectorAll<HTMLElement>('[data-block-id]'));
  const segments: SelectionSegment[] = [];
  for (const block of blocks) {
    if (!range.intersectsNode(block)) continue;
    const blockRange = document.createRange();
    blockRange.selectNodeContents(block);
    const clipped = range.cloneRange();
    if (clipped.compareBoundaryPoints(Range.START_TO_START, blockRange) < 0) {
      clipped.setStart(blockRange.startContainer, blockRange.startOffset);
    }
    if (clipped.compareBoundaryPoints(Range.END_TO_END, blockRange) > 0) {
      clipped.setEnd(blockRange.endContainer, blockRange.endOffset);
    }
    const rawQuote = clipped.toString();
    if (!rawQuote.trim()) continue;
    // Trim leading/trailing whitespace so the persisted anchor matches only
    // the meaningful selected text. A multi-block selection often picks up a
    // trailing newline from the block's last text node; persisting that
    // newline causes the highlighter to wrap a whitespace-only fragment in
    // a <mark>, which renders as a visible empty yellow line.
    const quote = rawQuote.replace(/^\s+|\s+$/g, '');
    if (!quote) continue;
    const occurrence = quoteOccurrence(blockText(block), quote, rangeStartOffset(block, clipped));
    segments.push({ blockId: block.dataset.blockId!, quote, occurrence });
  }
  return segments;
}

let floating: HTMLElement | null = null;

// Show the selection actions popup with two buttons: Comment (opens the
// composer for a note/thread on the first selected block) and Highlight
// (creates a session-only highlight per touched block, convertible later).
function showFloatingSelectionActions(range: Range, segments: SelectionSegment[]): void {
  floating?.remove();
  const rect = range.getBoundingClientRect();
  const bar = document.createElement('div');
  bar.className = 'floating-comment floating-selection-actions';
  bar.style.top = `${rect.top - 44}px`;
  bar.style.left = `${rect.left}px`;
  const primary = segments[0]!;

  const comment = document.createElement('button');
  comment.type = 'button';
  comment.className = 'floating-selection-comment';
  comment.innerHTML =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-3 3v-3H6a2 2 0 0 1-2-2V6z" fill="currentColor"/></svg>' +
    '<span>Comment</span>';
  comment.addEventListener('click', () => {
    // Threads/notes anchor to a single block. For a multi-block selection
    // the composer uses the first segment; the user can edit the seed text.
    openComposer(primary.blockId, primary.quote, primary.occurrence);
    dismissFloating();
  });

  const highlight = document.createElement('button');
  highlight.type = 'button';
  highlight.className = 'floating-selection-highlight';
  highlight.innerHTML =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 14l2-2 6-6 4 4-6 6-2 2H4v-4z" fill="currentColor"/></svg>' +
    '<span>Highlight</span>';
  highlight.addEventListener('click', () => {
    void createHighlights(segments);
    dismissFloating();
  });

  bar.appendChild(comment);
  bar.appendChild(highlight);
  document.body.appendChild(bar);
  floating = bar;
  setTimeout(() => { dismissFloating(); }, 5000);
}

function dismissFloating(): void {
  floating?.remove();
  floating = null;
}

async function createHighlight(blockId: string, quote: string, occurrence: number): Promise<void> {
  const r = await fetch('/api/highlights', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ documentPath: state.documentPath, anchor: { blockId, quote, occurrence } }),
  });
  if (!r.ok) {
    console.error('highlight failed', r.status, await r.text());
    return;
  }
  // SSE highlight.created arrives shortly after and adds the entry to
  // state.highlights + re-applies marks. We don't optimistically render so the
  // server timestamp/colorIndex stay authoritative.
}

// Multi-block highlight: create one server-side highlight per segment. They
// are independent (each gets its own id and SSE event) so removing one
// segment in the popup leaves the rest in place.
async function createHighlights(segments: SelectionSegment[]): Promise<void> {
  await Promise.all(segments.map((s) => createHighlight(s.blockId, s.quote, s.occurrence)));
}

function onHighlightCreated(evt: { highlightId: string; highlight: Highlight }): void {
  state.highlights.set(evt.highlightId, evt.highlight);
  applyQuoteHighlights();
}

function onHighlightDeleted(evt: { highlightId: string }): void {
  state.highlights.delete(evt.highlightId);
  dismissHighlightActions();
  applyQuoteHighlights();
}

function onThreadCreated(evt: { threadId: string; thread: Thread }): void {
  state.threads.set(evt.threadId, evt.thread);
  if (evt.thread.kind === 'thread') {
    const turn = getTurnState(evt.threadId);
    turn.active = true;
    turn.startedAt = Date.now();
  }
  renderThread(evt.thread);
  applyQuoteHighlights();
  recomputeApplyEnabled();
}

let highlightActions: HTMLElement | null = null;
let highlightActionsTimer: number | null = null;
function showHighlightActions(mark: HTMLElement, highlightId: string): void {
  dismissHighlightActions();
  const rect = mark.getBoundingClientRect();
  const bar = document.createElement('div');
  bar.className = 'floating-comment floating-selection-actions floating-highlight-actions';
  bar.style.top = `${rect.top - 44 + window.scrollY}px`;
  bar.style.left = `${rect.left + window.scrollX}px`;
  bar.style.position = 'absolute';
  bar.dataset.highlightId = highlightId;

  const toThread = document.createElement('button');
  toThread.type = 'button';
  toThread.className = 'floating-selection-comment';
  toThread.innerHTML =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-3 3v-3H6a2 2 0 0 1-2-2V6z" fill="currentColor"/></svg>' +
    '<span>To thread</span>';
  toThread.addEventListener('click', () => {
    void promoteHighlight(highlightId, 'thread');
  });

  const toNote = document.createElement('button');
  toNote.type = 'button';
  toNote.className = 'floating-selection-comment';
  toNote.innerHTML =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 4h7l3 3v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>' +
    '<span>To note</span>';
  toNote.addEventListener('click', () => {
    void promoteHighlight(highlightId, 'note');
  });

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'floating-selection-comment';
  remove.innerHTML =
    '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>' +
    '<span>Remove</span>';
  remove.addEventListener('click', () => {
    void removeHighlight(highlightId);
  });

  bar.appendChild(toThread);
  bar.appendChild(toNote);
  bar.appendChild(remove);
  bar.addEventListener('mouseenter', () => {
    if (highlightActionsTimer !== null) {
      window.clearTimeout(highlightActionsTimer);
      highlightActionsTimer = null;
    }
  });
  bar.addEventListener('mouseleave', scheduleHighlightActionsDismiss);
  mark.addEventListener('mouseleave', scheduleHighlightActionsDismiss, { once: true });
  document.body.appendChild(bar);
  highlightActions = bar;
}

function scheduleHighlightActionsDismiss(): void {
  if (highlightActionsTimer !== null) window.clearTimeout(highlightActionsTimer);
  highlightActionsTimer = window.setTimeout(() => {
    dismissHighlightActions();
  }, 250);
}

function dismissHighlightActions(): void {
  if (highlightActionsTimer !== null) {
    window.clearTimeout(highlightActionsTimer);
    highlightActionsTimer = null;
  }
  highlightActions?.remove();
  highlightActions = null;
}

async function promoteHighlight(highlightId: string, to: 'thread' | 'note'): Promise<void> {
  const highlight = state.highlights.get(highlightId);
  if (!highlight) return;
  // For both directions we need text to seed the first message. We use the
  // anchor quote so the conversation/note starts with the exact selected
  // text. The user can edit afterwards.
  const message = highlight.anchor.quote ?? '';
  if (to === 'thread' && !message.trim()) {
    showCenterToast('Cannot promote an empty highlight to a thread.');
    return;
  }
  dismissHighlightActions();
  try {
    const r = await fetch(`/api/highlights/${highlightId}/convert`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ to, message }),
    });
    if (!r.ok) throw new Error(`server responded ${r.status}`);
  } catch (err) {
    console.error('promote highlight failed', err);
    showCenterToast(`Promote failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function removeHighlight(highlightId: string): Promise<void> {
  dismissHighlightActions();
  try {
    const r = await fetch(`/api/highlights/${highlightId}`, { method: 'DELETE' });
    if (!r.ok) throw new Error(`server responded ${r.status}`);
  } catch (err) {
    console.error('remove highlight failed', err);
    showCenterToast(`Remove failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

let centerToast: HTMLElement | null = null;
function showCenterToast(text: string): void {
  centerToast?.remove();
  const t = document.createElement('div');
  t.className = 'center-toast';
  t.textContent = text;
  document.body.appendChild(t);
  centerToast = t;
  setTimeout(() => {
    if (centerToast === t) {
      t.remove();
      centerToast = null;
    }
  }, 2500);
}

// When a user highlights text inside an assistant message of an open thread,
// offer a one-click "Quote" button that drops the selection (each line prefixed
// with "| ") into that thread's reply textarea.
let threadQuoteSelectionInstalled = false;
function installThreadQuoteSelection(): void {
  if (threadQuoteSelectionInstalled) return;
  threadQuoteSelectionInstalled = true;
  document.addEventListener('mouseup', (e) => {
    // Clicking the floating Quote button also fires a mouseup that bubbles to
    // document. Without this guard we'd tear the button down (via the floating
    // singleton reset in showFloatingQuote) before its own click handler could
    // run — so the button appeared clickable but never inserted the quote.
    const target = e.target as Element | null;
    if (target?.closest('.floating-comment')) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    const start = range.startContainer.nodeType === 1
      ? (range.startContainer as Element)
      : range.startContainer.parentElement;
    const msg = start?.closest('.msg.assistant') as HTMLElement | null;
    if (!msg) return;
    const card = msg.closest('.thread-card') as HTMLElement | null;
    if (!card || card.classList.contains('resolved')) return;
    const text = sel.toString();
    if (!text.trim()) return;
    showFloatingQuote(range, card, text);
  });
}

function showFloatingQuote(range: Range, card: HTMLElement, text: string): void {
  floating?.remove();
  const rect = range.getBoundingClientRect();
  const btn = document.createElement('button');
  btn.className = 'floating-comment floating-quote';
  btn.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5h4v4H7c0 2 1 3 2 3v2c-3 0-4-2-4-5V5zm7 0h4v4h-2c0 2 1 3 2 3v2c-3 0-4-2-4-5V5z" fill="currentColor"/></svg><span>Quote</span>';
  btn.style.top = `${rect.top - 44}px`;
  btn.style.left = `${rect.left}px`;
  btn.addEventListener('click', () => {
    insertQuoteIntoReply(card, text);
    floating?.remove();
    floating = null;
    window.getSelection()?.removeAllRanges();
  });
  document.body.appendChild(btn);
  floating = btn;
  setTimeout(() => { floating?.remove(); floating = null; }, 5000);
}

function insertQuoteIntoReply(card: HTMLElement, text: string): void {
  const ta = card.querySelector<HTMLTextAreaElement>('.reply');
  if (!ta) return;
  appendQuoteToTextarea(ta, text);
  autogrowTextarea(ta);
}

function openComposer(blockId: string, quote: string | undefined, occurrence = 1): void {
  const anchor = document.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
  if (!anchor) return;
  const existing = document.querySelector<HTMLElement>(`.composer-overlay[data-composer-anchor-block-id="${blockId}"]`);
  if (existing) return;
  const platform = detectComposerPlatform(navigator.platform);
  const noteShortcut = platform === 'macos' ? '⌘+Enter' : 'Ctrl+Enter';
  const box = document.createElement('div');
  box.className = 'composer thread-card';
  box.innerHTML = `
    ${quote ? `<blockquote class="quote">${escapeHtml(quote)}</blockquote>` : ''}
    <textarea rows="3" placeholder="Message assistant…  Enter to send, ${noteShortcut} to add note, Shift+Enter for newline."></textarea>
    <div class="composer-actions">
      <button class="btn btn-primary send">Send</button>
      <button class="btn note">Add note</button>
      <button class="btn cancel">Cancel</button>
    </div>`;
  mountComposerOverlay(box, anchor, blockId, quote, occurrence);
  const ta = box.querySelector('textarea')!;
  installShiftArrowTextareaSelection(ta);
  // Defer focus to the next frame. Focusing synchronously inside the `+`-button
  // click handler sometimes left the textarea without a visible caret because
  // the click cycle re-evaluated focus after our call returned.
  requestAnimationFrame(() => {
    positionNoteOverlays();
    ta.focus({ preventScroll: true });
    ta.scrollIntoView({ block: 'center', behavior: 'smooth' });
    scheduleOverlayPosition();
  });
  const sendBtn = box.querySelector('.send') as HTMLButtonElement;
  const noteBtn = box.querySelector('.note') as HTMLButtonElement;
  const cancelBtn = box.querySelector('.cancel') as HTMLButtonElement;
  // While the platform's note modifier is held (Cmd on macOS, Ctrl elsewhere),
  // Enter adds a note instead of sending. Highlight the "Add note" button so
  // the alternate action is visible before the key is released. Ctrl+Enter is
  // intentionally left untouched on macOS.
  const setNoteArmed = (armed: boolean): void => {
    const noteArmed = armed && !noteBtn.disabled;
    noteBtn.classList.toggle('armed', noteArmed);
    sendBtn.classList.toggle('unarmed', noteArmed);
  };
  ta.addEventListener('keydown', (e) => {
    const action = composerKeyAction(e, platform);
    setNoteArmed(composerNoteModifierActive(e, platform));
    // Enter submits; Shift+Enter inserts a newline. The platform note shortcut
    // adds a note.
    if (action !== 'none') {
      e.preventDefault();
      (action === 'note' ? noteBtn : sendBtn).click();
    }
  });
  ta.addEventListener('keyup', (e) => setNoteArmed(composerNoteModifierActive(e, platform)));
  ta.addEventListener('blur', () => setNoteArmed(false));
  const submit = async (kind: 'thread' | 'note'): Promise<void> => {
    const message = ta.value.trim(); if (!message) return;
    sendBtn.disabled = true;
    noteBtn.disabled = true;
    cancelBtn.disabled = true;
    ta.disabled = true;
    const prevError = box.querySelector('.composer-error');
    if (prevError) prevError.remove();
    try {
      await createThread(blockId, quote, message, kind, occurrence);
      box.remove();
      positionNoteOverlays();
    } catch (err) {
      const errDiv = document.createElement('div');
      errDiv.className = 'composer-error';
      errDiv.textContent = `⚠ Failed to send: ${err instanceof Error ? err.message : String(err)}`;
      box.appendChild(errDiv);
      positionNoteOverlays();
      sendBtn.disabled = false;
      noteBtn.disabled = false;
      cancelBtn.disabled = false;
      ta.disabled = false;
      ta.focus();
    }
  };
  sendBtn.addEventListener('click', () => void submit('thread'));
  noteBtn.addEventListener('click', () => void submit('note'));
  cancelBtn.addEventListener('click', () => {
    box.remove();
    positionNoteOverlays();
  });
}

async function createThread(
  blockId: string,
  quote: string | undefined,
  message: string,
  kind: 'thread' | 'note',
  occurrence = 1,
): Promise<void> {
  const r = await fetch('/api/threads', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ documentPath: state.documentPath, anchor: { blockId, quote, occurrence }, message, kind }),
  });
  if (!r.ok) throw new Error(`server responded ${r.status}`);
  const { threadId } = (await r.json()) as { threadId: string };
  const thread: Thread = {
    id: threadId, kind, status: 'open',
    documentPath: state.documentPath,
    anchor: { blockId, quote, occurrence },
    messages: [{ role: 'user', text: message, ts: new Date().toISOString() }],
    createdAt: new Date().toISOString(),
  };
  state.threads.set(threadId, thread);
  state.activeThreadId = threadId;
  const turn = getTurnState(threadId);
  turn.active = kind === 'thread';
  turn.startedAt = Date.now();
  renderThread(thread);
  if (kind === 'thread') {
    const card = document.getElementById(`thread-${threadId}`);
    if (card) showStreamingPlaceholder(card, threadId);
  }
  applyQuoteHighlights();
  recomputeApplyEnabled();
}

// Marks the trailing `.streaming` div as a visible assistant bubble so the
// blinking-cursor caret renders even before any delta has arrived.
function showStreamingPlaceholder(card: HTMLElement, threadId?: string): void {
  const streamEl = card.querySelector<HTMLElement>('.streaming');
  if (!streamEl) return;
  if (threadId) startTurnHeartbeat(card, threadId);
  streamEl.classList.add('msg', 'assistant');
  renderStreamingMessage(streamEl);
}

function startTurnHeartbeat(card: HTMLElement, threadId: string): void {
  const turn = getTurnState(threadId);
  turn.active = true;
  const interrupt = card.querySelector<HTMLButtonElement>('.interrupt-btn');
  if (interrupt) interrupt.hidden = false;
  if (!turn.startedAt) turn.startedAt = Date.now();
  if (turn.heartbeat !== null) window.clearInterval(turn.heartbeat);
  const update = (): void => {
    if (!turn.active) return;
    const stream = card.querySelector<HTMLElement>('.streaming');
    if (!stream) return;
    const elapsed = Math.floor((Date.now() - turn.startedAt) / 1000);
    stream.dataset.heartbeatStatus = `Working (${elapsed}s). Press Interrupt or Esc.`;
    if (!stream.dataset.agentStatus) renderStreamingMessage(stream);
  };
  update();
  turn.heartbeat = window.setInterval(update, 5_000);
}

function stopTurnHeartbeat(threadId: string, stream?: HTMLElement): void {
  const turn = getTurnState(threadId);
  turn.active = false;
  if (turn.heartbeat !== null) window.clearInterval(turn.heartbeat);
  turn.heartbeat = null;
  const card = document.getElementById(`thread-${threadId}`);
  const interrupt = card?.querySelector<HTMLButtonElement>('.interrupt-btn');
  if (interrupt) interrupt.hidden = true;
  if (stream) delete stream.dataset.heartbeatStatus;
}

function renderQueuedQueries(card: HTMLElement, threadId: string): void {
  const area = card.querySelector<HTMLElement>('.queued-messages');
  if (!area) return;
  const queued = getTurnState(threadId).queued;
  area.replaceChildren();
  if (queued.length === 0) return;
  const label = document.createElement('div');
  label.className = 'queued-label';
  label.textContent = `Queued ${queued.length} ${queued.length === 1 ? 'query' : 'queries'} · press ↑ to edit newest`;
  area.appendChild(label);
  for (const text of queued) {
    const item = document.createElement('div');
    item.className = 'queued-query';
    item.textContent = text;
    area.appendChild(item);
  }
}

function queueReply(card: HTMLElement, threadId: string, text: string): void {
  getTurnState(threadId).queued.push(text);
  renderQueuedQueries(card, threadId);
}

async function submitReply(card: HTMLElement, threadId: string, text: string): Promise<void> {
  const turn = getTurnState(threadId);
  if (turn.active) {
    queueReply(card, threadId, text);
    appendMsg(card, 'user', text);
    const current = state.threads.get(threadId);
    if (current) current.messages.push({ role: 'user', text, ts: new Date().toISOString() });
    try {
      const r = await fetch(`/api/threads/${threadId}/messages`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
      const index = turn.queued.indexOf(text);
      if (index >= 0) turn.queued.splice(index, 1);
      renderQueuedQueries(card, threadId);
    } catch (err) {
      const stream = card.querySelector<HTMLElement>('.streaming');
      if (stream) {
        stream.dataset.agentStatus = `Steering failed: ${err instanceof Error ? err.message : String(err)}`;
        renderStreamingMessage(stream);
      }
    }
    return;
  }
  turn.active = true;
  turn.startedAt = Date.now();
  appendMsg(card, 'user', text);
  const current = state.threads.get(threadId);
  if (current) current.messages.push({ role: 'user', text, ts: new Date().toISOString() });
  showStreamingPlaceholder(card, threadId);
  try {
    const r = await fetch(`/api/threads/${threadId}/messages`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    if (!r.ok) throw new Error(`server responded ${r.status}`);
  } catch (err) {
    onMessageError({ threadId, error: err instanceof Error ? err.message : String(err) });
  }
}

function drainQueued(threadId: string, _card: HTMLElement): void {
  // Queued entries are submitted to the active provider immediately. They
  // remain visible only until the steering request is acknowledged, so a
  // completed turn must never replay them as a second ordinary turn.
  if (getTurnState(threadId).queued.length === 0) return;
}

async function interruptThread(threadId: string, card: HTMLElement): Promise<void> {
  const turn = getTurnState(threadId);
  if (!turn.active || card.dataset.interrupting === '1') return;
  card.dataset.interrupting = '1';
  onStatus({ threadId, status: 'Interrupting…' });
  try {
    const r = await fetch(`/api/threads/${threadId}/interrupt`, { method: 'POST' });
    if (!r.ok) throw new Error(`server responded ${r.status}`);
  } catch (err) {
    delete card.dataset.interrupting;
    onMessageError({ threadId, error: err instanceof Error ? err.message : String(err) });
  }
}

function activeInterruptTarget(): { threadId: string; card: HTMLElement } | null {
  const preferred = state.activeThreadId;
  if (preferred && getTurnState(preferred).active) {
    const card = document.getElementById(`thread-${preferred}`);
    if (card) return { threadId: preferred, card };
  }
  for (const [threadId, turn] of turnStates) {
    if (!turn.active) continue;
    const card = document.getElementById(`thread-${threadId}`);
    if (card) return { threadId, card };
  }
  return null;
}

function onGlobalKeyDown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || event.defaultPrevented) return;
  if (document.querySelector('.modal-backdrop, .image-viewer-backdrop')) return;
  const target = activeInterruptTarget();
  if (!target) return;
  event.preventDefault();
  void interruptThread(target.threadId, target.card);
}

// Sizes a textarea to its content up to the CSS max-height. Called on `input`
// and after clearing so the reply field grows with Shift+Enter and shrinks
// back to one line after a message is sent.
function autogrowTextarea(ta: HTMLTextAreaElement): void {
  ta.style.height = 'auto';
  ta.style.height = `${ta.scrollHeight}px`;
}

function renderThread(thread: Thread): void {
  if (thread.status === 'closed' || thread.status === 'archived') {
    removeThreadCard(thread.id);
    return;
  }

  const anchor = document.querySelector<HTMLElement>(`[data-block-id="${thread.anchor.blockId}"]`);
  if (!anchor) return;

  let card = document.getElementById(`thread-${thread.id}`) as HTMLElement | null;
  if (!card) {
    card = document.createElement('div');
    card.id = `thread-${thread.id}`;
    card.className = 'thread-card';
  }
  if (thread.kind === 'note' && thread.status === 'open') {
    card.classList.remove('resolved');
    renderNoteCard(card, thread);
    mountNoteOverlay(card, anchor, thread.anchor.blockId, thread.anchor.quote);
    return;
  }
  if (card.classList.contains('note-overlay')) {
    unmountNoteOverlay(card, anchor);
  } else {
    appendThreadCardAfterAnchor(card, anchor);
  }
  card.classList.remove('resolved');
  card.innerHTML = `
    <div class="thread-header">
      <div class="thread-label"><span class="thread-icon">💬</span> <strong>Thread</strong> <span class="anchor-quote">${thread.anchor.quote ? `“${escapeHtml(thread.anchor.quote)}”` : 'entire block'}</span></div>
      <div class="thread-actions">
        <button class="btn btn-ghost to-note-btn" title="Collapse this thread into a single note">↩ To note</button>
      </div>
    </div>
    <div class="messages"></div>
    <div class="queued-messages" aria-live="polite"></div>
    <div class="reply-row">
      <textarea class="reply" rows="1" placeholder="Reply…  Enter to send, Shift+Enter for newline"></textarea>
      <button class="btn btn-primary send">Send</button>
      <button class="btn btn-ghost interrupt-btn" hidden>Interrupt</button>
      <div class="thread-close-actions">
        <button class="btn btn-ghost summarize-thread-btn">Summarize thread</button>
        <button class="btn btn-ghost close-with-last-btn">Close with last response</button>
      </div>
      <button class="btn btn-ghost delete-btn">Delete</button>
    </div>`;
  const messagesEl = card.querySelector<HTMLElement>('.messages')!;
  for (const m of thread.messages) {
    const el = document.createElement('div');
    el.className = `msg ${m.role}`;
    if (m.role === 'assistant') el.innerHTML = renderMarkdown(m.text);
    else el.textContent = m.text;
    messagesEl.appendChild(el);
  }
  const streamEl = document.createElement('div');
  streamEl.className = 'streaming';
  messagesEl.appendChild(streamEl);
  renderQueuedQueries(card, thread.id);
  if (getTurnState(thread.id).active) showStreamingPlaceholder(card, thread.id);
  (card.querySelector('.interrupt-btn') as HTMLButtonElement).addEventListener('click', () => {
    void interruptThread(thread.id, card!);
  });
  (card.querySelector('.send') as HTMLButtonElement).addEventListener('click', async () => {
    const input = card!.querySelector<HTMLTextAreaElement>('.reply')!;
    const text = input.value.trim(); if (!text) return;
    input.value = '';
    autogrowTextarea(input);
    await submitReply(card!, thread.id, text);
  });
  const replyTa = card.querySelector('.reply') as HTMLTextAreaElement;
  installShiftArrowTextareaSelection(replyTa);
  replyTa.addEventListener('keydown', (e) => {
    // Enter submits; Shift+Enter inserts a newline. Cmd/Ctrl+Enter kept for back-compat.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      (card!.querySelector('.send') as HTMLButtonElement).click();
    }
    if (e.key === 'ArrowUp' && getTurnState(thread.id).queued.length > 0 &&
      (replyTa.value.length === 0 || replyTa.selectionStart === 0)) {
      e.preventDefault();
      const queued = getTurnState(thread.id).queued.pop()!;
      replyTa.value = queued;
      autogrowTextarea(replyTa);
      renderQueuedQueries(card!, thread.id);
      replyTa.selectionStart = replyTa.selectionEnd = replyTa.value.length;
    }
  });
  replyTa.addEventListener('input', () => autogrowTextarea(replyTa));
  const toNoteBtn = card.querySelector('.to-note-btn') as HTMLButtonElement;
  toNoteBtn.addEventListener('click', async () => {
    const ok = await modalConfirm({
      title: 'Convert thread to note?',
      body: 'The full transcript is discarded.\n\nThe note keeps the last assistant reply (or the last user message if there is none yet).',
      primaryLabel: 'Convert',
    });
    if (!ok) return;
    toNoteBtn.disabled = true;
    try {
      const r = await fetch(`/api/threads/${thread.id}/convert`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ to: 'note' }),
      });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      showCardError(card, `Failed to convert to note: ${err instanceof Error ? err.message : String(err)}`);
      toNoteBtn.disabled = false;
    }
  });
  const deleteBtn = card.querySelector('.delete-btn') as HTMLButtonElement;
  deleteBtn.addEventListener('click', async () => {
    const ok = await modalConfirm({
      title: 'Delete thread?',
      body: 'The transcript will be discarded and NOT archived into the doc.',
      primaryLabel: 'Delete',
      primaryVariant: 'danger',
    });
    if (!ok) return;
    deleteBtn.disabled = true;
    try {
      const r = await fetch(`/api/threads/${thread.id}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      showCardError(card, `Failed to delete thread: ${err instanceof Error ? err.message : String(err)}`);
      deleteBtn.disabled = false;
    }
  });
  const summarizeBtn = card.querySelector('.summarize-thread-btn') as HTMLButtonElement;
  summarizeBtn.addEventListener('click', async () => {
    setCloseActionsPending(card, summarizeBtn, 'Summarizing…');
    try {
      await summarizeThread(thread);
    } catch (err) {
      console.error('summarizeThread failed', err);
      showCardError(card, `Failed to summarize thread: ${err instanceof Error ? err.message : String(err)}`);
      restoreCloseActions(card, thread);
    }
  });

  const closeWithLastBtn = card.querySelector('.close-with-last-btn') as HTMLButtonElement;
  syncCloseWithLastButton(card, thread);
  closeWithLastBtn.addEventListener('click', async () => {
    const current = state.threads.get(thread.id) ?? thread;
    const lastAssistant = getLastAssistantMessage(current);
    if (!lastAssistant) {
      showCardError(card, 'No agent response is available yet.');
      syncCloseWithLastButton(card, current);
      return;
    }
    setCloseActionsPending(card, closeWithLastBtn, 'Closing…');
    try {
      await closeThread(thread.id, lastAssistant.text);
    } catch (err) {
      showCardError(card, `Failed to close thread with last response: ${err instanceof Error ? err.message : String(err)}`);
      restoreCloseActions(card, current);
    }
  });
}

function removeThreadCard(threadId: string): void {
  const card = document.getElementById(`thread-${threadId}`);
  if (!card) return;
  clearNoteOverlayTimer(card);
  card.remove();
}

function resolvedThreadsForDisplay(): Thread[] {
  const live = [...state.threads.values()].filter((thread) => thread.status === 'closed');
  const archived = [...state.archived.values()].filter((thread) =>
    !isArchivedThreadDuplicate(thread, state.threads.values()),
  );
  return [...live, ...archived];
}

function addThreadAction(
  summary: HTMLElement,
  label: string,
  action: () => void,
): void {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-ghost archived-thread-action';
  button.textContent = label;
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    action();
  });
  summary.querySelector('.archived-thread-actions')?.appendChild(button);
}

function deleteResolvedThread(thread: Thread, details: HTMLDetailsElement, button: HTMLButtonElement): void {
  void (async () => {
    const archived = thread.status === 'archived';
    const ok = await modalConfirm({
      title: archived ? 'Delete archived thread?' : 'Delete thread?',
      body: 'The archived thread block will be removed from the doc.\n\nThis cannot be undone.',
      primaryLabel: 'Delete',
      primaryVariant: 'danger',
    });
    if (!ok) return;
    button.disabled = true;
    const path = archived ? `?path=${encodeURIComponent(state.documentPath)}` : '';
    try {
      const r = await fetch(`/api/threads/${thread.id}${path}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      details.open = true;
      showCardError(details, `Failed to delete thread: ${err instanceof Error ? err.message : String(err)}`);
      button.disabled = false;
    }
  })();
}

function decorateResolvedThreadDetails(): void {
  const details = [...document.querySelectorAll<HTMLDetailsElement>('#doc details')];
  const used = new Set<HTMLDetailsElement>();
  for (const thread of resolvedThreadsForDisplay()) {
    const detail = findThreadDetails(details.filter((candidate) => !used.has(candidate)), thread);
    if (!detail) continue;
    const summary = detail.querySelector<HTMLElement>(':scope > summary');
    if (!summary) continue;
    used.add(detail);
    summary.querySelector('.archived-thread-actions')?.remove();
    const actions = document.createElement('span');
    actions.className = 'archived-thread-actions';
    summary.appendChild(actions);
    detail.classList.add('inline-thread-details');
    if (thread.status === 'closed' && thread.kind === 'thread') {
      addThreadAction(summary, 'Edit', () => openConclusionEditor(thread, detail));
    }
    if (thread.status === 'archived' || (thread.status === 'closed' && thread.kind === 'thread')) {
      const deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'btn btn-ghost archived-thread-action delete-thread-action';
      deleteButton.textContent = 'Delete';
      deleteButton.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        deleteResolvedThread(thread, detail, deleteButton);
      });
      actions.appendChild(deleteButton);
    }
  }
}

function renderNoteCard(card: HTMLElement, thread: Thread): void {
  const noteText = thread.messages[0]?.text ?? '';
  card.innerHTML = `
    <div class="thread-header">
      <div class="thread-label"><span class="thread-icon">📝</span> <strong>Note</strong> <span class="anchor-quote">${thread.anchor.quote ? `“${escapeHtml(thread.anchor.quote)}”` : 'entire block'}</span></div>
      <div class="thread-actions">
        <button class="btn btn-ghost edit-note-btn">Edit</button>
        <button class="btn btn-ghost to-thread-btn" title="Open an assistant conversation with this note as the first message">↪ Ask assistant</button>
        <button class="btn btn-ghost delete-btn">Delete note</button>
      </div>
    </div>
    <div class="messages">
      <div class="msg note">${renderMarkdown(noteText)}</div>
    </div>`;

  const deleteBtn = card.querySelector('.delete-btn') as HTMLButtonElement;
  deleteBtn.addEventListener('click', async () => {
    const ok = await modalConfirm({
      title: 'Delete note?',
      body: 'The text will be discarded and NOT archived into the doc.',
      primaryLabel: 'Delete',
      primaryVariant: 'danger',
    });
    if (!ok) return;
    deleteBtn.disabled = true;
    try {
      const r = await fetch(`/api/threads/${thread.id}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      showCardError(card, `Failed to delete note: ${err instanceof Error ? err.message : String(err)}`);
      deleteBtn.disabled = false;
    }
  });

  const toThreadBtn = card.querySelector('.to-thread-btn') as HTMLButtonElement;
  toThreadBtn.addEventListener('click', async () => {
    toThreadBtn.disabled = true;
    try {
      const r = await fetch(`/api/threads/${thread.id}/convert`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ to: 'thread' }),
      });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      showCardError(card, `Failed to ask assistant: ${err instanceof Error ? err.message : String(err)}`);
      toThreadBtn.disabled = false;
    }
  });

  const editBtn = card.querySelector('.edit-note-btn') as HTMLButtonElement;
  editBtn.addEventListener('click', () => {
    const msgEl = card.querySelector<HTMLElement>('.msg.note')!;
    msgEl.innerHTML = `
      <textarea class="note-edit-textarea" rows="3">${escapeHtml(noteText)}</textarea>
      <div class="note-edit-actions">
        <button class="btn btn-primary save-note-btn">Save</button>
        <button class="btn cancel-note-btn">Cancel</button>
      </div>`;
    const ta = msgEl.querySelector('textarea') as HTMLTextAreaElement;
    installShiftArrowTextareaSelection(ta);
    requestAnimationFrame(() => { ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; });
    autogrowTextarea(ta);
    ta.addEventListener('input', () => autogrowTextarea(ta));
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        (msgEl.querySelector('.save-note-btn') as HTMLButtonElement).click();
      }
      if (e.key === 'Escape') (msgEl.querySelector('.cancel-note-btn') as HTMLButtonElement).click();
    });
    const saveBtn = msgEl.querySelector('.save-note-btn') as HTMLButtonElement;
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      const next = ta.value;
      try {
        const r = await fetch(`/api/threads/${thread.id}/note`, {
          method: 'PATCH', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ message: next }),
        });
        if (!r.ok) throw new Error(`server responded ${r.status}`);
      } catch (err) {
        showCardError(card, `Failed to save note: ${err instanceof Error ? err.message : String(err)}`);
        saveBtn.disabled = false;
      }
    });
    (msgEl.querySelector('.cancel-note-btn') as HTMLButtonElement).addEventListener('click', () => {
      msgEl.innerHTML = renderMarkdown(noteText);
    });
  });
}

function showCardError(card: HTMLElement, message: string): void {
  const prev = card.querySelector('.thread-error');
  if (prev) prev.remove();
  const errDiv = document.createElement('div');
  errDiv.className = 'thread-error';
  errDiv.textContent = `⚠ ${message}`;
  card.appendChild(errDiv);
}

// Open an inline conclusion editor inside the persisted details element. Save
// PUTs the new conclusion, which rewrites that block server-side.
function openConclusionEditor(thread: Thread, details: HTMLDetailsElement): void {
  const current = thread.conclusion ?? '';
  details.open = true;
  details.querySelector('.conclusion-edit')?.remove();
  const section = document.createElement('div');
  section.className = 'conclusion-edit';
  section.innerHTML = `
    <div class="conclusion-label">Edit conclusion</div>
    <textarea rows="5">${escapeHtml(current)}</textarea>
    <div class="conclusion-preview-label">Preview</div>
    <div class="conclusion-preview"></div>
    <div class="conclusion-actions">
      <button class="btn btn-primary save-conclusion-btn">Save</button>
      <button class="btn btn-ghost delete-conclusion-btn">Delete</button>
      <button class="btn cancel-conclusion-btn">Cancel</button>
    </div>`;
  details.appendChild(section);
  const textarea = section.querySelector('textarea') as HTMLTextAreaElement;
  installShiftArrowTextareaSelection(textarea);
  const preview = section.querySelector('.conclusion-preview') as HTMLElement;
  const refreshPreview = (): void => { preview.innerHTML = renderMarkdown(textarea.value); };
  textarea.addEventListener('input', refreshPreview);
  refreshPreview();
  requestAnimationFrame(() => textarea.focus());

  (section.querySelector('.save-conclusion-btn') as HTMLButtonElement).addEventListener('click', async () => {
    (section.querySelector('.save-conclusion-btn') as HTMLButtonElement).disabled = true;
    try {
      const r = await fetch(`/api/threads/${thread.id}/conclusion`, {
        method: 'PUT', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ conclusion: textarea.value }),
      });
      if (!r.ok) throw new Error(`server responded ${r.status}`);
    } catch (err) {
      showCardError(details, `Failed to save conclusion: ${err instanceof Error ? err.message : String(err)}`);
      (section.querySelector('.save-conclusion-btn') as HTMLButtonElement).disabled = false;
    }
  });

  (section.querySelector('.delete-conclusion-btn') as HTMLButtonElement).addEventListener('click', async () => {
    const deleteBtn = section.querySelector('.delete-conclusion-btn') as HTMLButtonElement;
    deleteResolvedThread(thread, details, deleteBtn);
  });

  (section.querySelector('.cancel-conclusion-btn') as HTMLButtonElement).addEventListener('click', () => {
    section.remove();
  });
}

function appendMsg(card: HTMLElement, role: 'user' | 'assistant', text: string): void {
  const container = card.querySelector('.messages')!;
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (role === 'assistant') div.innerHTML = renderMarkdown(text);
  else div.textContent = text;
  container.insertBefore(div, container.querySelector('.streaming'));
}

function onDelta(evt: { threadId: string; delta: string }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const stream = card.querySelector<HTMLElement>('.streaming')!;
  if (!stream.classList.contains('msg')) {
    stream.classList.add('msg', 'assistant');
  }
  const turn = getTurnState(evt.threadId);
  turn.active = true;
  if (!turn.startedAt) turn.startedAt = Date.now();
  // Accumulate the raw markdown in a dataset attr and re-render on every
  // delta. marked tolerates half-finished markdown (unclosed fences, dangling
  // list markers, etc.), so partial renders degrade gracefully.
  const next = (stream.dataset.raw ?? '') + evt.delta;
  stream.dataset.raw = next;
  delete stream.dataset.agentStatus;
  renderStreamingMessage(stream);
}

function onStatus(evt: { threadId: string; status: string | null }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const stream = card.querySelector<HTMLElement>('.streaming');
  if (!stream) return;
  if (!stream.classList.contains('msg')) {
    stream.classList.add('msg', 'assistant');
  }
  const turn = getTurnState(evt.threadId);
  turn.active = true;
  if (!turn.startedAt) turn.startedAt = Date.now();
  if (evt.status && evt.status.trim()) stream.dataset.agentStatus = evt.status;
  else delete stream.dataset.agentStatus;
  renderStreamingMessage(stream);
}

function renderStreamingMessage(stream: HTMLElement): void {
  const raw = stream.dataset.raw ?? '';
  stream.innerHTML = raw ? renderMarkdown(raw) : '';
  const status = stream.dataset.agentStatus ?? stream.dataset.heartbeatStatus;
  if (!status) return;

  const statusEl = document.createElement('div');
  statusEl.className = 'agent-status';
  statusEl.textContent = status;
  stream.appendChild(statusEl);
  stream.append(document.createElement('br'), document.createElement('br'));
}

function onActivity(evt: { threadId: string; activity: AgentActivity }): void {
  const text = evt.activity.text.length > ACTIVITY_TEXT_LIMIT
    ? `${evt.activity.text.slice(0, ACTIVITY_TEXT_LIMIT)}…`
    : evt.activity.text;
  onStatus({ threadId: evt.threadId, status: text });
}

function onDone(evt: { threadId: string; message: { role: 'assistant'; text: string } }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const stream = card.querySelector<HTMLElement>('.streaming')!;
  if (!stream.classList.contains('msg')) {
    stream.classList.add('msg', 'assistant');
  }
  // Replace the streaming text with the markdown-rendered final message.
  stopTurnHeartbeat(evt.threadId, stream);
  delete stream.dataset.raw;
  delete stream.dataset.agentStatus;
  stream.innerHTML = renderMarkdown(evt.message.text);
  stream.classList.remove('streaming');
  const next = document.createElement('div'); next.className = 'streaming';
  card.querySelector('.messages')!.appendChild(next);
  const thread = state.threads.get(evt.threadId);
  if (thread) {
    thread.messages.push({ role: 'assistant', text: evt.message.text, ts: new Date().toISOString() });
    syncCloseWithLastButton(card, thread);
  }
  drainQueued(evt.threadId, card);
}

function onInterrupted(evt: { threadId: string }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const stream = card.querySelector<HTMLElement>('.streaming');
  if (!stream) return;
  stopTurnHeartbeat(evt.threadId, stream);
  stream.classList.add('msg', 'assistant', 'interrupted');
  stream.classList.remove('streaming');
  delete stream.dataset.raw;
  delete stream.dataset.agentStatus;
  stream.textContent = 'Turn interrupted. Press Enter to send another query.';
  const next = document.createElement('div'); next.className = 'streaming';
  card.querySelector('.messages')!.appendChild(next);
  delete card.dataset.interrupting;
  drainQueued(evt.threadId, card);
}

function onMessageError(evt: { threadId: string; error: string }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const stream = card.querySelector<HTMLElement>('.streaming');
  if (!stream) return;
  if (!stream.classList.contains('msg')) {
    stream.classList.add('msg', 'assistant');
  }
  stream.classList.remove('streaming');
  stream.classList.add('error');
  delete stream.dataset.raw;
  delete stream.dataset.agentStatus;
  stopTurnHeartbeat(evt.threadId, stream);
  stream.textContent = `⚠ Agent reply failed: ${evt.error}`;
  const next = document.createElement('div'); next.className = 'streaming';
  card.querySelector('.messages')!.appendChild(next);
  delete card.dataset.interrupting;
  drainQueued(evt.threadId, card);
}

function getLastAssistantMessage(thread: Thread): { text: string } | null {
  const lastAssistant = [...thread.messages].reverse().find((m) => m.role === 'assistant');
  return lastAssistant ? { text: lastAssistant.text } : null;
}

function closeActionButtons(card: HTMLElement): HTMLButtonElement[] {
  return [...card.querySelectorAll<HTMLButtonElement>('.summarize-thread-btn, .close-with-last-btn')];
}

function setCloseActionsPending(card: HTMLElement, activeButton: HTMLButtonElement, label: string): void {
  for (const button of closeActionButtons(card)) {
    if (button.dataset.origLabel === undefined) button.dataset.origLabel = button.textContent ?? '';
    button.disabled = true;
    if (button === activeButton) button.innerHTML = `<span class="spinner" aria-hidden="true"></span> ${label}`;
  }
}

function restoreCloseActions(card: HTMLElement, thread: Thread): void {
  for (const button of closeActionButtons(card)) {
    button.textContent = button.dataset.origLabel ?? button.textContent ?? '';
    button.disabled = false;
  }
  syncCloseWithLastButton(card, thread);
}

function syncCloseWithLastButton(card: HTMLElement, thread: Thread): void {
  if (card.querySelector('.conclusion-edit')) return;
  const button = card.querySelector<HTMLButtonElement>('.close-with-last-btn');
  if (!button) return;
  const hasLastAssistant = getLastAssistantMessage(thread) !== null;
  button.disabled = !hasLastAssistant;
  button.title = hasLastAssistant
    ? 'Close the thread and use the last agent response as the summary'
    : 'No agent response yet';
}

async function summarizeThread(thread: Thread): Promise<void> {
  const r = await fetch(`/api/threads/${thread.id}/propose-conclusion`, { method: 'POST' });
  if (!r.ok) throw new Error(`server responded ${r.status}`);
}

async function closeThread(threadId: string, conclusion: string): Promise<void> {
  const r = await fetch(`/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion }),
  });
  if (!r.ok) throw new Error(`server responded ${r.status}`);
}

function onConclusion(evt: { threadId: string; conclusion: string }): void {
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (!card) return;
  const t = state.threads.get(evt.threadId);
  if (t && t.status === 'closed') return;
  // Spinner / "Summarizing…" label did its job — swap close action labels back
  // to their original text (they stay disabled while the editor is open).
  for (const b of closeActionButtons(card)) {
    const fallbackLabel = b.classList.contains('summarize-thread-btn') ? 'Summarize thread' : 'Close with last response';
    b.textContent = b.dataset.origLabel ?? fallbackLabel;
    b.disabled = true;
  }
  const existing = card.querySelector<HTMLElement>('.conclusion-edit');
  if (existing) {
    const textarea = existing.querySelector<HTMLTextAreaElement>('textarea');
    if (textarea) {
      textarea.value = evt.conclusion;
      textarea.dispatchEvent(new Event('input'));
    }
    return;
  }
  const section = document.createElement('div');
  section.className = 'conclusion-edit';
  section.innerHTML = `
    <div class="conclusion-label">Assistant's proposed summary (edit before saving):</div>
    <textarea rows="5">${escapeHtml(evt.conclusion)}</textarea>
    <div class="conclusion-preview-label">Preview</div>
    <div class="conclusion-preview"></div>
    <div class="conclusion-actions">
      <button class="btn btn-primary save">Close with summary</button>
      <button class="btn cancel">Cancel</button>
    </div>`;
  card.appendChild(section);
  recomputeApplyEnabled();
  const textarea = section.querySelector('textarea') as HTMLTextAreaElement;
  installShiftArrowTextareaSelection(textarea);
  const preview = section.querySelector('.conclusion-preview') as HTMLElement;
  const refreshPreview = (): void => { preview.innerHTML = renderMarkdown(textarea.value); };
  textarea.addEventListener('input', refreshPreview);
  refreshPreview();
  (section.querySelector('.save') as HTMLButtonElement).addEventListener('click', async () => {
    await closeThread(evt.threadId, textarea.value);
  });
  (section.querySelector('.cancel') as HTMLButtonElement).addEventListener('click', async () => {
    // OK → close thread with empty conclusion; Cancel → keep editor open to revisit.
    const discard = await modalConfirm({
      title: 'Discard proposed summary?',
      body: 'Closing the thread without a summary — the proposal is lost.',
      primaryLabel: 'Discard',
      secondaryLabel: 'Keep editing',
      primaryVariant: 'danger',
    });
    if (!discard) return;
    section.remove();
    recomputeApplyEnabled();
    try {
      await closeThread(evt.threadId, '');
    } catch (err) {
      showCardError(card, `Failed to close thread: ${err instanceof Error ? err.message : String(err)}`);
      const current = state.threads.get(evt.threadId);
      if (current) restoreCloseActions(card, current);
    }
  });
}

function onClosed(evt: { threadId: string; conclusion?: string }): void {
  const t = state.threads.get(evt.threadId); if (!t) return;
  t.status = 'closed';
  if (evt.conclusion !== undefined) t.conclusion = evt.conclusion;
  renderThread(t);
  decorateResolvedThreadDetails();
  recomputeApplyEnabled();
}

function onDeleted(evt: { threadId: string }): void {
  // Live and archived threads live in separate maps — drop from either.
  state.threads.delete(evt.threadId);
  state.archived.delete(evt.threadId);
  const card = document.getElementById(`thread-${evt.threadId}`);
  if (card) {
    clearNoteOverlayTimer(card);
    card.remove();
  }
  applyQuoteHighlights();
  recomputeApplyEnabled();
}

// Replaces our local Thread with the server copy and re-renders the card.
// Used after note edits, conclusion edits, and thread↔note conversions. The
// conversion case is why we re-apply quote highlights: the <mark> class
// encodes `kind` (quote-highlight-thread vs quote-highlight-note), so the
// highlight color must follow the new kind.
function onUpdated(evt: { threadId: string; thread: Thread }): void {
  state.threads.set(evt.threadId, evt.thread);
  renderThread(evt.thread);
  decorateResolvedThreadDetails();
  applyQuoteHighlights();
  recomputeApplyEnabled();
}

function onDocUpdated(evt: { html: string; blockIds: string[]; title?: string; archivedThreads?: Thread[] }): void {
  const scroll = window.scrollY;
  if (evt.title !== undefined) applyTitle(evt.title);
  removeNoteOverlays();
  removeComposerOverlays();
  renderDoc(evt.html);
  installBlockPluses();
  // When the server re-parsed archives (e.g. after deleting one), the remaining
  // threads get fresh `archived-N` ids — replace our mirror so future deletes
  // point at the right blocks.
  if (evt.archivedThreads) {
    state.archived.clear();
    for (const t of evt.archivedThreads) state.archived.set(t.id, t);
  }
  for (const t of state.threads.values()) renderThread(t);
  decorateResolvedThreadDetails();
  applyQuoteHighlights();
  window.scrollTo({ top: scroll });
  scrollToLocationHash();
}

function applyTitle(title: string): void {
  const clean = title.trim() || 'Inline Discussion';
  document.title = `${clean} — Inline Discussion`;
  const el = document.getElementById('title');
  if (el) el.textContent = clean;
}

function onFinished(_evt: { result: unknown }): void {
  document.body.innerHTML = '<div class="finished"><h2>Discussion complete</h2><p>You can close this tab.</p></div>';
  try { window.close(); } catch { /* ignore */ }
}

function onPaused(_evt: { result: unknown }): void {
  document.body.innerHTML = '<div class="finished"><h2>Discussion paused</h2><p>Reopen this discussion to resume its open threads.</p></div>';
}

function renderExistingThreads(): void {
  for (const t of state.threads.values()) renderThread(t);
  decorateResolvedThreadDetails();
  applyQuoteHighlights();
}

async function finish(): Promise<void> {
  const open = [...state.threads.values()].filter((t) => t.status === 'open');
  if (open.length > 0) {
    const openThreads = open.filter((t) => t.kind !== 'note').length;
    const openNotes = open.filter((t) => t.kind === 'note').length;
    const parts: string[] = [];
    if (openThreads > 0) parts.push(`${openThreads} open thread(s) will be auto-closed now and appended to the doc with an assistant-generated conclusion`);
    if (openNotes > 0) parts.push(`${openNotes} open note(s) will be appended to the doc as-is`);
    const ok = await modalConfirm({
      title: 'Finish discussion?',
      body: `${parts.join('; ')}.\n\nAny threads you already closed this session are already in the doc.`,
      primaryLabel: 'Finish',
      secondaryLabel: 'Cancel',
    });
    if (!ok) return;
  }
  await fetch('/api/finish', { method: 'POST' });
}

async function pause(): Promise<void> {
  const r = await fetch('/api/pause', { method: 'POST' });
  if (!r.ok) throw new Error(`server responded ${r.status}`);
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
