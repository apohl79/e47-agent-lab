import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  canFinishThread,
  recoverMainAgentThreadAnchor,
} from '../src/main-agent-thread.ts';
import type { Thread } from '../src/types.ts';

function thread(recipient: Thread['recipient']): Thread {
  return {
    id: 't-1',
    kind: 'thread',
    recipient,
    anchor: { blockId: 'target', quote: 'Selected text', occurrence: 1 },
    status: 'open',
    messages: [],
    createdAt: '2026-10-01T00:00:00.000Z',
  };
}

test('moves a lost Main agent anchor to the nearest surviving following block', () => {
  const recovered = recoverMainAgentThreadAnchor(
    thread('main-agent'),
    ['heading', 'target', 'following'],
    ['heading', 'replacement', 'following'],
  );

  assert.equal(recovered.anchor.blockId, 'following');
  assert.equal(recovered.anchor.quote, 'Selected text');
});

test('moves a lost final Main agent anchor to the closest preceding block', () => {
  const recovered = recoverMainAgentThreadAnchor(
    thread('main-agent'),
    ['heading', 'target', 'following'],
    ['heading', 'replacement'],
  );

  assert.equal(recovered.anchor.blockId, 'heading');
});

test('uses the nearest surviving pre-edit block instead of a newly inserted block', () => {
  const recovered = recoverMainAgentThreadAnchor(
    thread('main-agent'),
    ['heading', 'target', 'following'],
    ['heading', 'inserted-first', 'inserted-second', 'following'],
  );

  assert.equal(recovered.anchor.blockId, 'following');
});

test('uses the same document position when no original block survives', () => {
  const recovered = recoverMainAgentThreadAnchor(
    thread('main-agent'),
    ['target'],
    ['replacement'],
  );

  assert.equal(recovered.anchor.blockId, 'replacement');
});

test('leaves thread-agent anchors unchanged and hides Finish only for Main agent threads', () => {
  const original = thread('thread-agent');
  const recovered = recoverMainAgentThreadAnchor(
    original,
    ['heading', 'target'],
    ['heading', 'replacement'],
  );

  assert.equal(recovered, original);
  assert.equal(canFinishThread('main-agent'), false);
  assert.equal(canFinishThread('thread-agent'), true);
  assert.equal(canFinishThread(undefined), true);
});
