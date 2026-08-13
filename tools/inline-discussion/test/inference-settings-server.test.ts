import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { EventEmitter, once } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { AgentFactory, ThreadAgent } from '../src/agent.ts';
import { createServer } from '../src/server.ts';
import type { InferenceCatalog, InferenceSettings } from '../src/types.ts';

const catalog: InferenceCatalog = {
  defaultSettings: { provider: 'openai', model: 'model-a', reasoningEffort: 'medium' },
  models: [
    {
      provider: 'openai', model: 'model-a', displayName: 'Model A', description: '', hidden: false, isDefault: true,
      defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [
        { reasoningEffort: 'medium', description: '' },
        { reasoningEffort: 'high', description: '' },
      ],
    },
    {
      provider: 'other', model: 'model-b', displayName: 'Model B', description: '', hidden: false, isDefault: true,
      defaultReasoningEffort: 'high',
      supportedReasoningEfforts: [{ reasoningEffort: 'high', description: '' }],
    },
  ],
};

async function patchSettings(port: number, path: string, settings: InferenceSettings): Promise<Response> {
  return fetch(`http://127.0.0.1:${port}${path}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(settings),
  });
}

function providerSwitchFactory(
  events: EventEmitter,
  created: Array<{ settings: InferenceSettings; preamble: string }>,
  closed: number[],
): AgentFactory {
  return (options): ThreadAgent => {
    assert.ok(options.inferenceSettings);
    const agentIndex = created.length;
    created.push({ settings: options.inferenceSettings, preamble: options.systemPreamble });
    if (agentIndex === 1) events.emit('replacement');
    return {
      async *send() {
        if (agentIndex === 0) {
          events.emit('turn-started');
          await once(events, 'release-turn');
        }
        yield { type: 'done', text: 'done' };
      },
      async *proposeConclusion() { yield { type: 'done', text: 'done' }; },
      snapshot: () => [],
      close: async () => { closed.push(agentIndex); },
    };
  };
}

test('page defaults affect only new threads and a thread override updates that agent', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-inference-server-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# Title\n\nParagraph.\n');
  const created: InferenceSettings[] = [];
  const changed: InferenceSettings[] = [];
  const factory: AgentFactory = (options): ThreadAgent => {
    assert.ok(options.inferenceSettings);
    created.push(options.inferenceSettings);
    return {
      async *send() { yield { type: 'done', text: 'done' }; },
      async *proposeConclusion() { yield { type: 'done', text: 'done' }; },
      snapshot: () => [],
      setInferenceSettings: (settings) => changed.push(settings),
    };
  };
  const inherited = { provider: 'openai', model: 'model-a', reasoningEffort: 'high' } as const;
  const { port, close } = await createServer({
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    agentFactory: factory,
    inferenceCatalog: catalog,
    initialInferenceSettings: inherited,
    shutdownOnFinish: false,
  });
  try {
    const bootstrap = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as {
      blockIds: string[];
      defaultInferenceSettings: InferenceSettings;
    };
    assert.deepEqual(bootstrap.defaultInferenceSettings, inherited);
    const anchor = { blockId: bootstrap.blockIds[1] };
    const first = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor, message: 'first' }),
    });
    const firstId = ((await first.json()) as { threadId: string }).threadId;
    assert.deepEqual(created[0], inherited);

    const nextDefault = { provider: 'other', model: 'model-b', reasoningEffort: 'high' } as const;
    assert.equal((await patchSettings(port, '/api/inference-settings', nextDefault)).status, 200);
    await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor, message: 'second' }),
    });
    assert.deepEqual(created, [inherited, nextDefault]);

    const firstOverride = { provider: 'openai', model: 'model-a', reasoningEffort: 'medium' } as const;
    assert.equal((await patchSettings(port, `/api/threads/${firstId}/inference-settings`, firstOverride)).status, 200);
    assert.deepEqual(changed, [firstOverride]);
    const latest = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as {
      threads: Array<{ id: string; inferenceSettings: InferenceSettings }>;
    };
    assert.deepEqual(latest.threads.find((thread) => thread.id === firstId)?.inferenceSettings, firstOverride);
  } finally {
    await close();
  }
});

test('unsupported inference settings are rejected', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-inference-invalid-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# Title\n');
  const factory: AgentFactory = () => ({
    async *send() { yield { type: 'done', text: 'done' }; },
    async *proposeConclusion() { yield { type: 'done', text: 'done' }; },
    snapshot: () => [],
  });
  const { port, close } = await createServer({
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    agentFactory: factory,
    inferenceCatalog: catalog,
    shutdownOnFinish: false,
  });
  try {
    const response = await patchSettings(port, '/api/inference-settings', {
      provider: 'openai', model: 'missing', reasoningEffort: 'medium',
    });
    assert.equal(response.status, 400);
  } finally {
    await close();
  }
});

test('a provider change replaces the backing agent after the active turn completes', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-inference-provider-switch-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# Title\n\nParagraph.\n');
  const events = new EventEmitter();
  const created: Array<{ settings: InferenceSettings; preamble: string }> = [];
  const closed: number[] = [];
  const factory = providerSwitchFactory(events, created, closed);
  const { port, close } = await createServer({
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    agentFactory: factory,
    inferenceCatalog: catalog,
    shutdownOnFinish: false,
  });
  try {
    const bootstrap = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as { blockIds: string[] };
    const turnStarted = once(events, 'turn-started');
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: bootstrap.blockIds[1] }, message: 'first' }),
    });
    const threadId = ((await response.json()) as { threadId: string }).threadId;
    await turnStarted;
    const replacement = once(events, 'replacement');
    const next = { provider: 'other', model: 'model-b', reasoningEffort: 'high' } as const;
    assert.equal((await patchSettings(port, `/api/threads/${threadId}/inference-settings`, next)).status, 200);
    assert.equal(created.length, 1);
    assert.deepEqual(closed, []);
    events.emit('release-turn');
    await replacement;
    assert.deepEqual(created.map((agent) => agent.settings), [catalog.defaultSettings, next]);
    const replacementAgent = created[1];
    assert.ok(replacementAgent);
    assert.match(replacementAgent.preamble, /User: first/);
    assert.match(replacementAgent.preamble, /Assistant: done/);
    assert.deepEqual(closed, [0]);
  } finally {
    await close();
  }
});
