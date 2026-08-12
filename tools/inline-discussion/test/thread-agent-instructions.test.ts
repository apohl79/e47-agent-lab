import assert from 'node:assert/strict';
import { test } from 'node:test';
import { THREAD_AGENT_BASE_INSTRUCTIONS } from '../src/agent.ts';

test('thread agent instructions require cumulative high-level handoffs without implementation work', () => {
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /response itself is the handoff/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Action items for the main agent/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /complete list of still-valid action items forward at the end of every future response/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Add newly derived items/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /revise or remove an existing item only when later conversation or evidence changes the conclusion/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Never silently drop an action item/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /target area, intended outcome, and essential findings or constraints/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /main agent owns detailed investigation, design, implementation, validation/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Do not provide patches, code or diagram drafts, ready-to-paste content/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /stop once the high-level action is justified/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Never offer to do work/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Want me to/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /not as an invitation for another thread turn/);
  assert.doesNotMatch(THREAD_AGENT_BASE_INSTRUCTIONS, /directly executable instructions/);
});
