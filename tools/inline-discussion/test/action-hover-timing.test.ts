import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import {
  HOVER_ACTION_DISMISS_MS,
  SELECTION_ACTION_DISMISS_MS,
} from '../src/web/action-hover-timing.ts';

test('block action controls delay their reveal while note indicators align with the block', () => {
  const stylesheet = readFileSync(new URL('../src/web/app.css', import.meta.url), 'utf8');
  const dom = new JSDOM(`<style>${stylesheet}</style><div data-block-id="example"><button class="block-plus"></button><button class="block-copy"></button><button class="block-note-indicator"></button></div>`);
  const button = dom.window.document.querySelector<HTMLElement>('.block-plus');
  const copy = dom.window.document.querySelector<HTMLElement>('.block-copy');
  const indicator = dom.window.document.querySelector<HTMLElement>('.block-note-indicator');
  assert.ok(button);
  assert.ok(copy);
  assert.ok(indicator);
  const style = dom.window.getComputedStyle(button);
  const copyStyle = dom.window.getComputedStyle(copy);
  const indicatorStyle = dom.window.getComputedStyle(indicator);

  assert.match(stylesheet, /\[data-block-id\]\s*\{[^}]*border-radius: var\(--radius-sm\)/);
  assert.match(stylesheet, /\[data-block-id\]:hover\s*\{[^}]*background: var\(--bg-chip\)/);
  assert.match(stylesheet, /:root\[data-theme="dark"\] \[data-block-id\]:hover\s*\{[^}]*background: color-mix\(in srgb, var\(--bg-chip\) 45%, var\(--bg\)\)/);
  assert.match(stylesheet, /\[data-block-id\]:hover > \.block-plus,[\s\S]*?transition-delay: var\(--block-plus-show-delay\)/);
  assert.deepEqual(
    {
      highlightActionsMs: HOVER_ACTION_DISMISS_MS,
      selectionActionsMs: SELECTION_ACTION_DISMISS_MS,
      blockPlusDelay: style.getPropertyValue('--block-plus-hide-delay').trim(),
      blockPlusShowDelay: style.getPropertyValue('--block-plus-show-delay').trim(),
      blockPlusPointerEvents: style.pointerEvents,
      copyDelay: copyStyle.getPropertyValue('--block-plus-hide-delay').trim(),
      copyShowDelay: copyStyle.getPropertyValue('--block-plus-show-delay').trim(),
      blockPlusTop: style.top,
      copyTop: copyStyle.top,
      noteIndicatorTop: indicatorStyle.top,
    },
    {
      highlightActionsMs: 2_000,
      selectionActionsMs: 5_000,
      blockPlusDelay: '2s',
      blockPlusShowDelay: '300ms',
      blockPlusPointerEvents: 'auto',
      copyDelay: '2s',
      copyShowDelay: '300ms',
      blockPlusTop: '38px',
      copyTop: '74px',
      noteIndicatorTop: '2px',
    },
  );
  dom.window.close();
});
