import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import type { Thread } from '../src/types.ts';
import { findThreadDetails, scrollThreadToTop } from '../src/web/thread-details.ts';

function makeThread(overrides: Partial<Thread> = {}): Thread {
  return {
    id: 'thread-1',
    kind: 'thread',
    anchor: { blockId: 'line-1', quote: 'technical evidence', occurrence: 1 },
    status: 'closed',
    messages: [],
    conclusion: 'Keep the evidence comparable.',
    createdAt: '2026-08-05T10:00:00Z',
    ...overrides,
  };
}

test('finds the persisted details element by its anchor and summary', () => {
  const dom = new JSDOM('<p data-block-id="line-1">Anchor</p><details><summary>💬 Thread on "technical evidence" — 2026-08-05</summary></details>');
  const details = dom.window.document.querySelectorAll<HTMLDetailsElement>('details');

  assert.equal(findThreadDetails(details, makeThread()), details[0]);
  dom.window.close();
});

test('finds a details element by its persisted thread id', () => {
  const dom = new JSDOM('<details data-thread-id="thread-1"><summary>💬 Thread on another anchor — 2026-08-05</summary></details>');
  const details = dom.window.document.querySelectorAll<HTMLDetailsElement>('details');

  assert.equal(findThreadDetails(details, makeThread()), details[0]);
  dom.window.close();
});

test('scrolls a finished thread to the top of the viewport', () => {
  const dom = new JSDOM('<details><summary>Thread</summary></details>');
  const details = dom.window.document.querySelector('details')!;
  let block = '';
  details.scrollIntoView = (options) => {
    block = typeof options === 'object' ? options.block ?? '' : '';
  };

  scrollThreadToTop(details);

  assert.equal(block, 'start');
  dom.window.close();
});
