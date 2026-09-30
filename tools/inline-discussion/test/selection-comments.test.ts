import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createSelectionComments,
  selectionCommentError,
  type SelectionCommentTarget,
} from '../src/web/selection-comments.ts';

test('creates comments for every selected block in order', async () => {
  const targets: SelectionCommentTarget[] = [
    { blockId: 'block-1', quote: 'first', occurrence: 1 },
    { blockId: 'block-2', quote: 'second', occurrence: 1 },
  ];
  const created: SelectionCommentTarget[] = [];

  await createSelectionComments(targets, async (target) => {
    created.push(target);
  });

  assert.deepEqual(created, targets);
});

test('rejects Main agent for multi-block comments before creation', () => {
  assert.equal(
    selectionCommentError('main-agent', 2),
    'Main agent comments support one selected block at a time. Select Thread agent to comment on all selected blocks.',
  );
});

test('allows Thread agent for multi-block comments and Main agent for one block', () => {
  assert.equal(selectionCommentError('thread-agent', 2), undefined);
  assert.equal(selectionCommentError('main-agent', 1), undefined);
});
