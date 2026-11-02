import { test } from 'node:test';
import assert from 'node:assert/strict';
import { formatSourceRange, parseSourceReference, selectedSourceText } from '../src/source-reference.ts';

test('parseSourceReference accepts line and inclusive character ranges', () => {
  assert.deepEqual(parseSourceReference('/docs/guide.md:40'), {
    pathPart: '/docs/guide.md',
    range: { startLine: 40, endLine: 40 },
  });
  assert.deepEqual(parseSourceReference('/docs/guide.md:40-45'), {
    pathPart: '/docs/guide.md',
    range: { startLine: 40, endLine: 45 },
  });
  assert.deepEqual(parseSourceReference('/docs/guide.md:40:5-45:12'), {
    pathPart: '/docs/guide.md',
    range: { startLine: 40, startColumn: 5, endLine: 45, endColumn: 12 },
  });
});

test('parseSourceReference leaves invalid reversed ranges in the path', () => {
  assert.deepEqual(parseSourceReference('/docs/guide.md:45-40'), {
    pathPart: '/docs/guide.md:45-40',
    range: null,
  });
  assert.deepEqual(parseSourceReference('/docs/guide.md:4:9-4:2'), {
    pathPart: '/docs/guide.md:4:9-4:2',
    range: null,
  });
});

test('selectedSourceText applies one-based inclusive columns across lines', () => {
  const source = 'alpha beta\ngamma delta\nomega';
  assert.equal(selectedSourceText(source, { startLine: 1, endLine: 2 }), 'alpha beta\ngamma delta');
  assert.equal(selectedSourceText(source, {
    startLine: 1,
    startColumn: 7,
    endLine: 2,
    endColumn: 5,
  }), 'beta\ngamma');
});

test('formatSourceRange preserves the supported link suffixes', () => {
  assert.equal(formatSourceRange({ startLine: 40, endLine: 40 }), ':40');
  assert.equal(formatSourceRange({ startLine: 40, endLine: 45 }), ':40-45');
  assert.equal(formatSourceRange({ startLine: 40, startColumn: 5, endLine: 45, endColumn: 12 }), ':40:5-45:12');
});
