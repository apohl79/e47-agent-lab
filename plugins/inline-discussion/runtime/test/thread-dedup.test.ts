import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Thread } from '../src/types.ts';
import { isArchivedThreadDuplicate } from '../src/web/thread-dedup.ts';

function makeThread(overrides: Partial<Thread> = {}): Thread {
  return {
    id: 'thread-1',
    kind: 'thread',
    anchor: { blockId: 'line-1', quote: 'technical evidence', occurrence: 1 },
    status: 'closed',
    messages: [
      { role: 'user', text: 'Please review this.', ts: '2026-08-05T10:00:00Z' },
      { role: 'assistant', text: 'Looks good.', ts: '2026-08-05T10:01:00Z' },
    ],
    conclusion: 'Keep the evidence comparable.',
    createdAt: '2026-08-05T10:00:00Z',
    ...overrides,
  };
}

test('identifies an archived copy of a closed live thread', () => {
  const live = makeThread();
  const archived = makeThread({ id: 'archived-1', status: 'archived' });

  assert.equal(isArchivedThreadDuplicate(archived, [live]), true);
});

test('keeps archived threads with different content or anchors', () => {
  const live = makeThread();
  const differentConclusion = makeThread({ id: 'archived-1', status: 'archived', conclusion: 'Different conclusion.' });
  const differentAnchor = makeThread({
    id: 'archived-2',
    status: 'archived',
    anchor: { blockId: 'line-2', quote: 'technical evidence', occurrence: 1 },
  });

  assert.equal(isArchivedThreadDuplicate(differentConclusion, [live]), false);
  assert.equal(isArchivedThreadDuplicate(differentAnchor, [live]), false);
});
