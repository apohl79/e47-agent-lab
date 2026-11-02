import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { replaceComposerError } from '../src/web/composer-errors.ts';

test('replaces an existing composer error instead of appending duplicates', () => {
  const dom = new JSDOM('<div></div>');
  const box = dom.window.document.querySelector('div')!;

  replaceComposerError(box, 'first');
  replaceComposerError(box, 'second');

  assert.equal(box.querySelectorAll('.composer-error').length, 1);
  assert.equal(box.querySelector('.composer-error')?.textContent, 'second');
  dom.window.close();
});
