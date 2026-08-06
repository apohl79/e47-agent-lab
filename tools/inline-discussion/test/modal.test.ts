import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

function setupDom(): JSDOM {
  const dom = new JSDOM('<!doctype html><html><body><main id="doc">Doc</main></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  // jsdom doesn't expose globals — wire them for the module.
  const { window } = dom;
  (globalThis as unknown as { window: Window }).window = window as unknown as Window;
  (globalThis as unknown as { document: Document }).document = window.document;
  (globalThis as unknown as { HTMLElement: typeof HTMLElement }).HTMLElement = window.HTMLElement;
  return dom;
}

test('modalConfirm resolves true on primary, false on secondary', async () => {
  setupDom();
  const { modalConfirm } = await import('../src/web/modal.ts');

  const p1 = modalConfirm({ title: 'T', body: 'B', primaryLabel: 'OK' });
  document.querySelector<HTMLButtonElement>('.modal-btn-primary')!.click();
  assert.equal(await p1, true);

  const p2 = modalConfirm({ title: 'T', body: 'B', primaryLabel: 'OK' });
  document.querySelector<HTMLButtonElement>('.modal-btn-secondary')!.click();
  assert.equal(await p2, false);
});

test('modalChoice resolves the clicked option value; last option is the primary', async () => {
  setupDom();
  const { modalChoice } = await import('../src/web/modal.ts');

  const opts = {
    title: 'Apply changes', body: 'B',
    options: [
      { label: 'Cancel', value: 'cancel' },
      { label: 'Apply & keep', value: 'keep' },
      { label: 'Apply & remove all', value: 'remove', variant: 'danger' as const },
    ],
    cancelValue: 'cancel',
  };

  // The last option renders as the primary (danger) button.
  const pRemove = modalChoice(opts);
  const primary = document.querySelector<HTMLButtonElement>('.modal-btn-primary')!;
  assert.equal(primary.textContent, 'Apply & remove all');
  assert.equal(primary.classList.contains('modal-btn-danger'), true);
  primary.click();
  assert.equal(await pRemove, 'remove');

  // A non-last option resolves its own value.
  const pKeep = modalChoice(opts);
  [...document.querySelectorAll<HTMLButtonElement>('.modal-btn')]
    .find((b) => b.textContent === 'Apply & keep')!.click();
  assert.equal(await pKeep, 'keep');
});

test('modalChoice resolves cancelValue on ESC and backdrop', async () => {
  setupDom();
  const { modalChoice } = await import('../src/web/modal.ts');
  const opts = {
    title: 'T', body: 'B',
    options: [{ label: 'Keep', value: 'keep' }, { label: 'Remove', value: 'remove' }],
    cancelValue: 'cancel',
  };

  const pEsc = modalChoice(opts);
  document.dispatchEvent(new (window as Window & typeof globalThis).KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(await pEsc, 'cancel');

  const pBackdrop = modalChoice(opts);
  document.querySelector<HTMLElement>('.modal-backdrop')!.dispatchEvent(
    new (window as Window & typeof globalThis).MouseEvent('mousedown', { bubbles: true }),
  );
  assert.equal(await pBackdrop, 'cancel');
});

test('modalChoice resolves null on ESC when no cancelValue given', async () => {
  setupDom();
  const { modalChoice } = await import('../src/web/modal.ts');
  const p = modalChoice({
    title: 'T', body: 'B',
    options: [{ label: 'A', value: 'a' }, { label: 'B', value: 'b' }],
  });
  document.dispatchEvent(new (window as Window & typeof globalThis).KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(await p, null);
});

test('modalConfirm resolves false on ESC', async () => {
  setupDom();
  const { modalConfirm } = await import('../src/web/modal.ts');
  const p = modalConfirm({ title: 'T', body: 'B', primaryLabel: 'OK' });
  document.dispatchEvent(new (window as Window & typeof globalThis).KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(await p, false);
});

test('modalConfirm resolves false on backdrop mousedown', async () => {
  setupDom();
  const { modalConfirm } = await import('../src/web/modal.ts');
  const p = modalConfirm({ title: 'T', body: 'B', primaryLabel: 'OK' });
  document.querySelector<HTMLElement>('.modal-backdrop')!.dispatchEvent(
    new (window as Window & typeof globalThis).MouseEvent('mousedown', { bubbles: true }),
  );
  assert.equal(await p, false);
});

test('modalConfirm sets inert on #doc while open and removes on close', async () => {
  setupDom();
  const { modalConfirm } = await import('../src/web/modal.ts');
  const doc = document.getElementById('doc')!;
  assert.equal(doc.hasAttribute('inert'), false);
  const p = modalConfirm({ title: 'T', body: 'B', primaryLabel: 'OK' });
  assert.equal(doc.hasAttribute('inert'), true);
  document.querySelector<HTMLButtonElement>('.modal-btn-primary')!.click();
  await p;
  assert.equal(doc.hasAttribute('inert'), false);
});

test('modalStatus updates checklist and ignores ESC and backdrop until error', async () => {
  setupDom();
  const { modalStatus } = await import('../src/web/modal.ts');
  const h = modalStatus({ title: 'Applying', initialStatus: 'Closing threads…' });
  const labels = () => [...document.querySelectorAll('.modal-task-label')].map((el) => el.textContent);
  assert.deepEqual(labels(), ['Closing threads…']);
  assert.notEqual(document.querySelector('.modal-task-active .modal-spinner'), null);
  h.setStatus('Applying changes…');
  assert.deepEqual(labels(), ['Closing threads…', 'Applying changes…']);
  assert.notEqual(document.querySelector('.modal-task-done .modal-task-check'), null);

  // ESC ignored
  document.dispatchEvent(new (window as Window & typeof globalThis).KeyboardEvent('keydown', { key: 'Escape' }));
  assert.notEqual(document.querySelector('.modal-card'), null);

  // Backdrop click ignored
  (document.querySelector<HTMLElement>('.modal-backdrop')!).click();
  assert.notEqual(document.querySelector('.modal-card'), null);

  // Error transition exposes a Dismiss button.
  h.setError('boom');
  assert.deepEqual(labels(), ['Closing threads…', 'Apply failed: boom']);
  assert.notEqual(document.querySelector('.modal-task-error'), null);
  const dismiss = document.querySelector<HTMLButtonElement>('.modal-btn-primary');
  assert.notEqual(dismiss, null);
  dismiss!.click();
  assert.equal(document.querySelector('.modal-card'), null);
});

test('modalStatus renders progress statuses as checklist tasks without a progress bar', async () => {
  setupDom();
  const { modalStatus } = await import('../src/web/modal.ts');
  const h = modalStatus({
    title: 'Applying',
    initialStatus: 'Scanning',
    initialProgress: { status: 'Scanning', percent: null },
  });
  const labels = () => [...document.querySelectorAll('.modal-task-label')].map((el) => el.textContent);
  assert.equal(document.querySelector('.modal-progress'), null);
  assert.deepEqual(labels(), ['Scanning']);

  h.setProgress({ status: 'Editing', percent: 42 });
  assert.deepEqual(labels(), ['Scanning', 'Editing']);
  assert.equal(document.querySelectorAll('.modal-task-done').length, 1);
  assert.equal(document.querySelectorAll('.modal-task-active').length, 1);

  h.setProgress({ status: 'Waiting', percent: null });
  assert.deepEqual(labels(), ['Scanning', 'Editing', 'Waiting']);

  h.setTasks([
    { id: 'a', label: 'Scanning', state: 'done' },
    { id: 'b', label: 'Waiting for monitoring', state: 'active' },
  ]);
  assert.deepEqual(labels(), ['Scanning', 'Waiting for monitoring']);
  assert.equal(document.querySelectorAll('.modal-task-done').length, 1);
  assert.notEqual(document.querySelector('.modal-task-active .modal-spinner'), null);
});

test('modalStatus.dismiss() closes the modal', async () => {
  setupDom();
  const { modalStatus } = await import('../src/web/modal.ts');
  const h = modalStatus({ title: 'X', initialStatus: 'Y' });
  assert.notEqual(document.querySelector('.modal-card'), null);
  h.dismiss();
  assert.equal(document.querySelector('.modal-card'), null);
});
