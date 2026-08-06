import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { updateBlockNoteIndicator } from '../src/web/block-note-indicator.ts';

function setupDom(): JSDOM {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  (globalThis as unknown as { document: Document }).document = dom.window.document;
  return dom;
}

test('whole-block note indicator stays visible and reveals its notes', () => {
  const dom = setupDom();
  const anchor = document.createElement('p');
  anchor.dataset.blockId = 'line-1';
  document.body.appendChild(anchor);
  let revealed = 0;

  updateBlockNoteIndicator(anchor, 1, () => { revealed += 1; });
  const indicator = anchor.querySelector<HTMLButtonElement>('.block-note-indicator');
  assert.ok(indicator);
  assert.equal(indicator.getAttribute('aria-label'), 'Show 1 note for this block');
  assert.equal(indicator.dataset.noteCount, '1');
  indicator.click();
  assert.equal(revealed, 1);

  updateBlockNoteIndicator(anchor, 2, () => { revealed += 1; });
  assert.equal(indicator.dataset.noteCount, '2');
  assert.equal(indicator.getAttribute('aria-label'), 'Show 2 notes for this block');
  updateBlockNoteIndicator(anchor, 0, () => undefined);
  assert.equal(anchor.querySelector('.block-note-indicator'), null);
  dom.window.close();
});
