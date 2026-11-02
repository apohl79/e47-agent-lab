/// <reference lib="dom" />

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import {
  calculateShiftArrowSelection,
  installShiftArrowTextareaSelection,
} from '../src/web/textarea-selection.ts';

function setupTextarea(value: string): HTMLTextAreaElement {
  const dom = new JSDOM('<!doctype html><html><body><textarea></textarea></body></html>');
  const textarea = dom.window.document.querySelector('textarea');
  assert.ok(textarea);
  textarea.value = value;
  return textarea;
}

function dispatchKeydown(
  textarea: HTMLTextAreaElement,
  init: KeyboardEventInit,
): KeyboardEvent {
  const win = textarea.ownerDocument.defaultView;
  assert.ok(win);
  const event = new win.KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    ...init,
  });
  textarea.dispatchEvent(event);
  return event;
}

test('calculateShiftArrowSelection extends and shrinks horizontal selections', () => {
  assert.deepEqual(calculateShiftArrowSelection('abcd', 2, 2, 'none', 'ArrowLeft'), {
    start: 1,
    end: 2,
    direction: 'backward',
  });
  assert.deepEqual(calculateShiftArrowSelection('abcd', 2, 2, 'none', 'ArrowRight'), {
    start: 2,
    end: 3,
    direction: 'forward',
  });
  assert.deepEqual(calculateShiftArrowSelection('abcd', 1, 3, 'forward', 'ArrowLeft'), {
    start: 1,
    end: 2,
    direction: 'forward',
  });
  assert.deepEqual(calculateShiftArrowSelection('abcd', 1, 3, 'backward', 'ArrowRight'), {
    start: 2,
    end: 3,
    direction: 'backward',
  });
});

test('calculateShiftArrowSelection moves vertically across textarea lines', () => {
  const value = 'abcd\nxy\n12345';
  assert.deepEqual(calculateShiftArrowSelection(value, 6, 6, 'none', 'ArrowUp'), {
    start: 1,
    end: 6,
    direction: 'backward',
  });
  assert.deepEqual(calculateShiftArrowSelection(value, 1, 1, 'none', 'ArrowDown'), {
    start: 1,
    end: 6,
    direction: 'forward',
  });
});

test('installShiftArrowTextareaSelection handles plain Shift+Arrow keydown', () => {
  const textarea = setupTextarea('abcd');
  installShiftArrowTextareaSelection(textarea);
  textarea.setSelectionRange(2, 2, 'none');

  const event = dispatchKeydown(textarea, { key: 'ArrowLeft', shiftKey: true });

  assert.equal(event.defaultPrevented, true);
  assert.equal(textarea.selectionStart, 1);
  assert.equal(textarea.selectionEnd, 2);
  assert.equal(textarea.selectionDirection, 'backward');
});

test('installShiftArrowTextareaSelection leaves modified Shift+Arrow to the browser', () => {
  const textarea = setupTextarea('abcd');
  installShiftArrowTextareaSelection(textarea);
  textarea.setSelectionRange(2, 2, 'none');

  const event = dispatchKeydown(textarea, { key: 'ArrowLeft', shiftKey: true, altKey: true });

  assert.equal(event.defaultPrevented, false);
  assert.equal(textarea.selectionStart, 2);
  assert.equal(textarea.selectionEnd, 2);
});
