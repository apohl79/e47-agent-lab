import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { updateApplyCount } from '../src/web/apply-count.ts';

test('Apply buttons show the shared pending note and thread count', () => {
  const stylesheet = readFileSync(new URL('../src/web/app.css', import.meta.url), 'utf8');
  const dom = new JSDOM(`
    <style>${stylesheet}</style>
    <button id="top" class="btn apply-btn">Apply<span class="apply-count" hidden></span></button>
    <button id="bottom" class="btn apply-btn">Apply<span class="apply-count" hidden></span></button>
  `);
  const buttons = [...dom.window.document.querySelectorAll<HTMLButtonElement>('button')];

  updateApplyCount(buttons, 2);
  const badges = [...dom.window.document.querySelectorAll<HTMLElement>('.apply-count')];
  assert.deepEqual(badges.map((badge) => ({ text: badge.textContent, hidden: badge.hidden })), [
    { text: '2', hidden: false },
    { text: '2', hidden: false },
  ]);
  assert.deepEqual(buttons.map((button) => button.getAttribute('aria-label')), [
    'Apply 2 notes or threads',
    'Apply 2 notes or threads',
  ]);
  assert.deepEqual(
    {
      buttonPosition: dom.window.getComputedStyle(buttons[0]!).position,
      badgePosition: dom.window.getComputedStyle(badges[0]!).position,
      badgeTop: dom.window.getComputedStyle(badges[0]!).top,
      badgeRight: dom.window.getComputedStyle(badges[0]!).right,
      badgeRadius: dom.window.getComputedStyle(badges[0]!).borderRadius,
    },
    {
      buttonPosition: 'relative',
      badgePosition: 'absolute',
      badgeTop: '-8px',
      badgeRight: '-8px',
      badgeRadius: '999px',
    },
  );

  updateApplyCount(buttons, 1);
  assert.equal(buttons[0]!.getAttribute('aria-label'), 'Apply 1 note or thread');

  updateApplyCount(buttons, 0);
  assert.deepEqual(badges.map((badge) => badge.hidden), [true, true]);
  assert.deepEqual(buttons.map((button) => button.getAttribute('aria-label')), ['Apply', 'Apply']);
  dom.window.close();
});
