// test/transcript.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readCodexSessionInferenceSettings, trimTranscript, readJsonl } from '../src/transcript.ts';
import type { InferenceCatalog } from '../src/types.ts';

const inferenceCatalog: InferenceCatalog = {
  defaultSettings: { provider: 'openai', model: 'gpt-default', reasoningEffort: 'medium' },
  models: [
    {
      provider: 'openai', model: 'gpt-current', displayName: 'GPT Current', description: '', hidden: false,
      isDefault: true, defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: '' }],
    },
    {
      provider: 'anthropic', model: 'claude-opus-5', displayName: 'Claude Opus 5', description: '', hidden: false,
      isDefault: false, defaultReasoningEffort: 'high',
      supportedReasoningEfforts: [{ reasoningEffort: 'xhigh', description: '' }],
    },
    {
      provider: 'openai', model: 'shared-model', displayName: 'Shared OpenAI', description: '', hidden: false,
      isDefault: false, defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: '' }],
    },
    {
      provider: 'other', model: 'shared-model', displayName: 'Shared Other', description: '', hidden: false,
      isDefault: false, defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: '' }],
    },
  ],
};

function makeFixture(): string {
  const big = 'X'.repeat(5000);
  const lines = [
    { type: 'user', text: 'Research the auth middleware.' },
    { type: 'assistant', text: "I'll take a look." },
    { type: 'tool_use', name: 'Read', args: '{"file_path":"/a/middleware.ts"}' },
    { type: 'tool_result', kind: 'Read', text: 'SHORT RESULT' },
    { type: 'tool_use', name: 'Grep', args: '{"pattern":"token"}' },
    { type: 'tool_result', kind: 'Grep', text: big },
    { type: 'assistant', text: "Here's my analysis..." },
  ];
  const dir = join(tmpdir(), `ind-test-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'session.jsonl');
  writeFileSync(path, lines.map((l) => JSON.stringify(l)).join('\n'));
  return path;
}

test('readJsonl parses lines', () => {
  const path = makeFixture();
  const entries = readJsonl(path);
  assert.equal(entries.length, 7);
  assert.equal(entries[0]!.type, 'user');
});

test('trimTranscript elides tool results > 4 KB', () => {
  const path = makeFixture();
  const entries = readJsonl(path);
  const text = trimTranscript(entries);
  assert.match(text, /Research the auth middleware./);
  assert.match(text, /SHORT RESULT/);
  assert.match(text, /\[tool result elided — Grep:/);
  assert.doesNotMatch(text, /X{4096,}/);
});

test('trimTranscript always keeps first user prompt and last assistant', () => {
  const path = makeFixture();
  const entries = readJsonl(path);
  const text = trimTranscript(entries, { maxBytes: 200 });
  assert.match(text, /Research the auth middleware./);
  assert.match(text, /Here's my analysis/);
});

import { redactSecrets } from '../src/transcript.ts';

test('redactSecrets replaces anthropic-style keys', () => {
  const out = redactSecrets('My key is sk-ant-abcDEFghiJKLmnoPQRstuVWXyz1234567890 please.');
  assert.match(out, /\[secret redacted: anthropic-or-openai-key\]/);
  assert.doesNotMatch(out, /sk-ant-abc/);
});

test('redactSecrets replaces password= assignments', () => {
  const out = redactSecrets('db: password="hunter2shouldnotleak"');
  assert.match(out, /\[secret redacted: password-assignment\]/);
  assert.doesNotMatch(out, /hunter2shouldnotleak/);
});

test('trimTranscript output has secrets redacted', () => {
  const path = makeFixture();
  const entries = readJsonl(path);
  // Insert a user message with a secret.
  entries.splice(1, 0, { type: 'user', text: 'here: sk-ant-SECRETKEY0123456789abcdef' });
  const text = trimTranscript(entries);
  assert.doesNotMatch(text, /SECRETKEY/);
  assert.match(text, /\[secret redacted:/);
});

test('readJsonl normalizes Codex session response items', () => {
  const dir = join(tmpdir(), `ind-codex-test-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'codex.jsonl');
  const lines = [
    { type: 'session_meta', payload: { id: 'thread' } },
    {
      type: 'response_item',
      payload: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: 'Review this plan.' }],
      },
    },
    {
      type: 'response_item',
      payload: {
        type: 'function_call',
        name: 'exec_command',
        arguments: '{"cmd":"rg TODO"}',
      },
    },
    {
      type: 'response_item',
      payload: {
        type: 'function_call_output',
        output: 'no matches',
      },
    },
    {
      type: 'response_item',
      payload: {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: 'The plan is coherent.' }],
      },
    },
  ];
  writeFileSync(path, lines.map((line) => JSON.stringify(line)).join('\n'));

  const entries = readJsonl(path);
  assert.deepEqual(entries.map((entry) => entry.type), ['user', 'tool_use', 'tool_result', 'assistant']);
  const text = trimTranscript(entries);
  assert.match(text, /USER: Review this plan/);
  assert.match(text, /TOOL exec_command/);
  assert.match(text, /TOOL_RESULT codex: no matches/);
  assert.match(text, /ASSISTANT: The plan is coherent/);
});

test('readCodexSessionInferenceSettings reads the latest host turn settings', () => {
  const dir = join(tmpdir(), `ind-codex-settings-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'codex.jsonl');
  const lines = [
    { type: 'session_meta', payload: { model_provider: 'openai' } },
    { type: 'turn_context', payload: { model: 'gpt-old', effort: 'medium' } },
    { type: 'turn_context', payload: { model: 'gpt-current', effort: 'medium' } },
  ];
  writeFileSync(path, lines.map((line) => JSON.stringify(line)).join('\n'));

  assert.deepEqual(readCodexSessionInferenceSettings(path, inferenceCatalog), {
    provider: 'openai',
    model: 'gpt-current',
    reasoningEffort: 'medium',
  });
});

test('readCodexSessionInferenceSettings resolves a switched model through the catalog', () => {
  const dir = join(tmpdir(), `ind-codex-settings-switched-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'codex.jsonl');
  const lines = [
    { type: 'session_meta', payload: { model_provider: 'openai' } },
    { type: 'turn_context', payload: { model: 'claude-opus-5', effort: 'xhigh' } },
  ];
  writeFileSync(path, lines.map((line) => JSON.stringify(line)).join('\n'));

  assert.deepEqual(readCodexSessionInferenceSettings(path, inferenceCatalog), {
    provider: 'anthropic',
    model: 'claude-opus-5',
    reasoningEffort: 'xhigh',
  });
});

test('readCodexSessionInferenceSettings uses session provider only to disambiguate model names', () => {
  const dir = join(tmpdir(), `ind-codex-settings-ambiguous-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'codex.jsonl');
  const lines = [
    { type: 'session_meta', payload: { model_provider: 'other' } },
    { type: 'turn_context', payload: { model: 'shared-model', effort: 'medium' } },
  ];
  writeFileSync(path, lines.map((line) => JSON.stringify(line)).join('\n'));

  assert.deepEqual(readCodexSessionInferenceSettings(path, inferenceCatalog), {
    provider: 'other',
    model: 'shared-model',
    reasoningEffort: 'medium',
  });
});

test('readCodexSessionInferenceSettings rejects incomplete host metadata', () => {
  const dir = join(tmpdir(), `ind-codex-settings-incomplete-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'codex.jsonl');
  writeFileSync(path, JSON.stringify({ type: 'turn_context', payload: { model: 'gpt-current' } }));
  assert.equal(readCodexSessionInferenceSettings(path, inferenceCatalog), undefined);
});
