import { test } from 'node:test';
import assert from 'node:assert/strict';
import { diagnosticJson } from '../src/diagnostics.ts';

test('diagnosticJson emits structured timestamps and redacts sensitive error text', () => {
  const record = JSON.parse(diagnosticJson('thread.turn.error', {
    threadId: 't-1',
    inputLength: 12,
    error: 'token=secret-value',
    omitted: undefined,
  })) as Record<string, unknown>;

  assert.equal(record.component, 'inline-discussion');
  assert.equal(record.event, 'thread.turn.error');
  assert.equal(record.threadId, 't-1');
  assert.equal(record.inputLength, 12);
  assert.equal(record.error, '[REDACTED]');
  assert.equal('omitted' in record, false);
  assert.equal(typeof record.ts, 'string');
});
