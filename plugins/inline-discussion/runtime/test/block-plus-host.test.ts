import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { blockPlusHost } from '../src/web/block-plus-host.ts';

test('blockPlusHost wraps pre blocks so controls escape scrolling overflow', () => {
  const dom = new JSDOM('<main id="doc"><pre data-block-id="code">code</pre></main>');
  const pre = dom.window.document.querySelector<HTMLElement>('pre');
  assert.ok(pre);

  const host = blockPlusHost(pre);

  assert.equal(host.className, 'block-shell');
  assert.equal(host.querySelector('pre'), pre);
  assert.equal(pre.parentElement, host);
  assert.equal(pre.dataset.blockId, 'code');
});

test('blockPlusHost is idempotent for an already-hosted pre block', () => {
  const dom = new JSDOM('<main><div class="block-shell"><pre data-block-id="code">code</pre></div></main>');
  const pre = dom.window.document.querySelector<HTMLElement>('pre');
  assert.ok(pre);

  const host = blockPlusHost(pre);

  assert.equal(host, pre.parentElement);
  assert.equal(dom.window.document.querySelectorAll('.block-shell').length, 1);
});

test('blockPlusHost leaves non-overflow blocks in place', () => {
  const dom = new JSDOM('<main><p data-block-id="paragraph">text</p></main>');
  const paragraph = dom.window.document.querySelector<HTMLElement>('p');
  assert.ok(paragraph);

  assert.equal(blockPlusHost(paragraph), paragraph);
  assert.equal(dom.window.document.querySelector('.block-shell'), null);
});
