import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  composerKeyAction,
  composerNoteModifierActive,
  detectComposerPlatform,
  type ComposerKeyInput,
} from '../src/web/composer-shortcuts.ts';

function key(overrides: Partial<ComposerKeyInput> = {}): ComposerKeyInput {
  return {
    key: 'Enter',
    shiftKey: false,
    metaKey: false,
    ctrlKey: false,
    isComposing: false,
    ...overrides,
  };
}

test('Meta+Enter creates a note', () => {
  assert.equal(composerKeyAction(key({ metaKey: true }), 'macos'), 'note');
});

test('macOS Ctrl+Enter has no composer action', () => {
  assert.equal(composerKeyAction(key({ ctrlKey: true }), 'macos'), 'none');
});

test('other platforms preserve Ctrl+Enter as the note shortcut', () => {
  assert.equal(composerKeyAction(key({ ctrlKey: true }), 'other'), 'note');
});

test('plain Enter creates a thread', () => {
  assert.equal(composerKeyAction(key(), 'macos'), 'thread');
});

test('Shift+Enter preserves a newline', () => {
  assert.equal(composerKeyAction(key({ shiftKey: true }), 'macos'), 'none');
});

test('composing Enter has no composer action', () => {
  assert.equal(composerKeyAction(key({ isComposing: true }), 'macos'), 'none');
});

test('non-Enter keys have no composer action', () => {
  assert.equal(composerKeyAction(key({ key: 'a', metaKey: true }), 'macos'), 'none');
});

test('macOS Ctrl does not arm the note button', () => {
  assert.equal(composerNoteModifierActive({ metaKey: false, ctrlKey: true }, 'macos'), false);
});

test('macOS Meta arms the note button', () => {
  assert.equal(composerNoteModifierActive({ metaKey: true, ctrlKey: false }, 'macos'), true);
});

test('combined macOS Meta+Ctrl does not arm the note button', () => {
  assert.equal(composerNoteModifierActive({ metaKey: true, ctrlKey: true }, 'macos'), false);
});

test('other platforms arm the note button for Ctrl', () => {
  assert.equal(composerNoteModifierActive({ metaKey: false, ctrlKey: true }, 'other'), true);
});

test('MacIntel is detected as macOS', () => {
  assert.equal(detectComposerPlatform('MacIntel'), 'macos');
});

test('Linux is detected as another platform', () => {
  assert.equal(detectComposerPlatform('Linux x86_64'), 'other');
});
