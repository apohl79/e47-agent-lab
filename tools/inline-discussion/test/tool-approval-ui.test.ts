import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toolApprovalModalOptions } from '../src/web/tool-approval.ts';

test('MCP approval overlay offers deny, one-time, session, and project decisions', () => {
  const options = toolApprovalModalOptions({
    id: 'approval-1',
    threadId: 'thread-1',
    provider: 'codex',
    toolName: 'create_page',
    input: { title: 'Roadmap' },
    title: 'Allow Notion to create a page?',
  });

  assert.equal(options.title, 'Approve MCP tool call');
  assert.match(options.body, /Allow Notion to create a page/);
  assert.match(options.body, /"title":"Roadmap"/);
  assert.deepEqual(options.options.map((option) => option.value), ['deny', 'project', 'session', 'once']);
  assert.equal(options.cancelValue, 'deny');
});
