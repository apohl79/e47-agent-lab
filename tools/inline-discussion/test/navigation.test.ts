import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { scrollToFragment } from '../src/web/navigation.ts';

test('scrollToFragment resolves encoded heading ids and scrolls the target', () => {
  const dom = new JSDOM('<main><h1 id="2026-02-dining">Dining</h1></main>');
  const target = dom.window.document.getElementById('2026-02-dining')!;
  let scrolled = false;
  target.scrollIntoView = () => { scrolled = true; };

  assert.equal(scrollToFragment('#2026-02-dining', dom.window.document), true);
  assert.equal(scrolled, true);
});

test('scrollToFragment ignores malformed or missing fragments', () => {
  const dom = new JSDOM('<main><h1 id="section">Section</h1></main>');

  assert.equal(scrollToFragment('', dom.window.document), false);
  assert.equal(scrollToFragment('#missing', dom.window.document), false);
  assert.equal(scrollToFragment('#%', dom.window.document), false);
});
