import assert from 'node:assert/strict';
import { test } from 'node:test';
import { THREAD_AGENT_BASE_INSTRUCTIONS } from '../src/agent.ts';

test('thread agent instructions require an imperative handoff without permission questions', () => {
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /response itself is the handoff/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /directly executable instructions/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /imperative form/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Never offer to do work/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Want me to/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /not as an invitation for another thread turn/);
});
