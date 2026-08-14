import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { focusSourceRange } from '../src/web/source-navigation.ts';

test('focusSourceRange highlights and centers a source line range', () => {
  const dom = new JSDOM('<main><pre data-block-id="line-2"><span class="source-line-code">alpha</span></pre><pre data-block-id="line-3"><span class="source-line-code">beta</span></pre></main>');
  const first = dom.window.document.querySelector<HTMLElement>('[data-block-id="line-2"]')!;
  let scrollBlock = '';
  first.scrollIntoView = (options) => { scrollBlock = (options as ScrollIntoViewOptions).block ?? ''; };

  assert.equal(focusSourceRange({ startLine: 2, endLine: 3 }, null, dom.window.document), true);
  assert.equal(dom.window.document.querySelectorAll('.source-line-target').length, 2);
  assert.equal(scrollBlock, 'center');
  dom.window.close();
});

test('focusSourceRange marks inclusive character columns in source lines', () => {
  const dom = new JSDOM('<main><pre data-block-id="line-2"><span class="source-line-code">alpha beta</span></pre></main>');
  const line = dom.window.document.querySelector<HTMLElement>('[data-block-id="line-2"]')!;
  line.scrollIntoView = () => {};

  assert.equal(focusSourceRange({
    startLine: 2,
    startColumn: 7,
    endLine: 2,
    endColumn: 10,
  }, 'beta', dom.window.document), true);
  assert.equal(dom.window.document.querySelector('.source-range-target')?.textContent, 'beta');
  dom.window.close();
});

test('focusSourceRange maps a Markdown source range to its rendered block and exact text', () => {
  const dom = new JSDOM('<main><ul data-source-start-line="3" data-source-end-line="4"><li>first</li><li>second</li></ul></main>');
  const block = dom.window.document.querySelector<HTMLElement>('ul')!;
  block.scrollIntoView = () => {};

  assert.equal(focusSourceRange({ startLine: 4, endLine: 4 }, 'second', dom.window.document), true);
  assert.equal(block.classList.contains('source-block-target'), true);
  assert.equal(dom.window.document.querySelector('.source-range-target')?.textContent, 'second');
  dom.window.close();
});
