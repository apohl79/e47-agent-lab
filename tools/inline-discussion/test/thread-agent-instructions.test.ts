import assert from 'node:assert/strict';
import { test } from 'node:test';
import { THREAD_AGENT_BASE_INSTRUCTIONS } from '../src/agent.ts';

test('thread agent instructions require concise high-level handoffs without implementation work', () => {
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /response itself is the handoff/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /one concise, high-level action item/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /target area, intended outcome, and essential findings or constraints/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /main agent owns detailed investigation, design, implementation, validation/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Do not provide patches, code or diagram drafts, ready-to-paste content/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /stop once the high-level action is justified/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Never offer to do work/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Want me to/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /not as an invitation for another thread turn/);
  assert.doesNotMatch(THREAD_AGENT_BASE_INSTRUCTIONS, /directly executable instructions/);
});
