import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import {
  HOVER_ACTION_DISMISS_MS,
  SELECTION_ACTION_DISMISS_MS,
} from '../src/web/action-hover-timing.ts';

test('hover-only action controls keep a two-second dismissal window', () => {
  const stylesheet = readFileSync(new URL('../src/web/app.css', import.meta.url), 'utf8');
  const dom = new JSDOM(`<style>${stylesheet}</style><button class="block-plus"></button>`);
  const button = dom.window.document.querySelector<HTMLElement>('.block-plus');
  assert.ok(button);
  const style = dom.window.getComputedStyle(button);

  assert.deepEqual(
    {
      highlightActionsMs: HOVER_ACTION_DISMISS_MS,
      selectionActionsMs: SELECTION_ACTION_DISMISS_MS,
      blockPlusDelay: style.getPropertyValue('--block-plus-hide-delay').trim(),
      blockPlusPointerEvents: style.pointerEvents,
    },
    {
      highlightActionsMs: 2_000,
      selectionActionsMs: 5_000,
      blockPlusDelay: '2s',
      blockPlusPointerEvents: 'auto',
    },
  );
  dom.window.close();
});
