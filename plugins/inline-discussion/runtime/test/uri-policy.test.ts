import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ALLOWED_URI_REGEXP } from '../src/uri-policy.ts';

test('allows Slack deep links while rejecting unsafe URI schemes', () => {
  assert.equal(ALLOWED_URI_REGEXP.test('slack://channel?team=T012TSW2ML3&id=C0AJ32EB8G3'), true);
  assert.equal(ALLOWED_URI_REGEXP.test('javascript:alert(1)'), false);
});
