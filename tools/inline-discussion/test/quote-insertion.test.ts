import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { appendQuoteToTextarea } from '../src/web/quote-insertion.ts';

function setupDom(): JSDOM {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  (globalThis as unknown as { window: Window }).window = window as unknown as Window;
  (globalThis as unknown as { document: Document }).document = window.document;
  (globalThis as unknown as { requestAnimationFrame: typeof requestAnimationFrame }).requestAnimationFrame =
    window.requestAnimationFrame.bind(window);
  return dom;
}

test('appendQuoteToTextarea restores focus and places the caret after the quote', async () => {
  const dom = setupDom();
  const textarea = document.createElement('textarea');
  textarea.value = 'draft';
  document.body.appendChild(textarea);

  appendQuoteToTextarea(textarea, 'quoted line');
  assert.equal(textarea.value, 'draft\n\n| quoted line\n\n');

  await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
  assert.equal(document.activeElement, textarea);
  assert.equal(textarea.selectionStart, textarea.value.length);
  assert.equal(textarea.selectionEnd, textarea.value.length);
  dom.window.close();
});
