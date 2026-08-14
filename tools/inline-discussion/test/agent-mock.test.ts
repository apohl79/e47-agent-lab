// test/agent-mock.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  appendTurnContext,
  codexAgentFactory,
  discussionAgentEnvironment,
  discoverCodexInferenceCatalog,
  dispatchSdkMessage,
  mockAgentFactory,
  readOnlyMcpConfigFromEnv,
  THREAD_AGENT_BASE_INSTRUCTIONS,
  type DispatchState,
  type StreamChunk,
} from '../src/agent.ts';

function chunkLabel(c: StreamChunk): string {
  if (c.type === 'status') return `status:${c.status ?? ''}`;
  if (c.type === 'activity') return `activity:${c.activity.kind}:${c.activity.title}:${c.activity.text}`;
  if (c.type === 'interrupted') return 'interrupted';
  return `${c.type}:${c.text}`;
}

function isDelta(c: StreamChunk): c is Extract<StreamChunk, { type: 'delta' }> {
  return c.type === 'delta';
}

function isDone(c: StreamChunk): c is Extract<StreamChunk, { type: 'done' }> {
  return c.type === 'done';
}

async function collectChunkLabels(stream: AsyncIterable<StreamChunk>): Promise<string[]> {
  const chunks: string[] = [];
  for await (const chunk of stream) chunks.push(chunkLabel(chunk));
  return chunks;
}

function dispatchEvents(events: unknown[], state: DispatchState): { chunks: StreamChunk[]; endSeen: boolean } {
  const chunks: StreamChunk[] = [];
  let endSeen = false;
  for (const event of events) {
    const result = dispatchSdkMessage(event, state);
    chunks.push(...result.chunks);
    endSeen ||= result.endOfTurn;
  }
  return { chunks, endSeen };
}

test('mockAgentFactory streams a scripted reply and records messages', async () => {
  const factory = mockAgentFactory({ reply: 'hi', conclusion: 'done' });
  const agent = factory({ systemPreamble: '', tools: [] });
  const chunks = await collectChunkLabels(agent.send('hello'));
  assert.deepEqual(chunks, ['delta:hi', 'done:hi']);
  const snap = agent.snapshot();
  assert.equal(snap.length, 2);
  assert.equal(snap[0]!.role, 'user');
  assert.equal(snap[1]!.role, 'assistant');
});

test('appendTurnContext makes the document and anchor explicit without duplicating the thread contract', () => {
  const payload = appendTurnContext('Explain ALT B.', 'Document under discussion: docs/sap-oem-voice-auth.md\nAnchor block: 97ed12e755');
  assert.match(payload, /^Explain ALT B\./);
  assert.match(payload, /<inline-discussion-turn-context>/);
  assert.match(payload, /docs\/sap-oem-voice-auth\.md/);
  assert.match(payload, /97ed12e755/);
  assert.doesNotMatch(payload, /inline discussion thread agent, not the main agent/i);
});

test('readOnlyMcpConfigFromEnv exposes configured MCP tools for interactive approval', () => {
  assert.equal(readOnlyMcpConfigFromEnv({}), null);
  assert.deepEqual(readOnlyMcpConfigFromEnv({ IND_MCP_URL: 'http://127.0.0.1:3100/mcp' }), {
    serverName: 'inline-mcp',
    url: 'http://127.0.0.1:3100/mcp',
    toolNames: [],
  });
  const config = readOnlyMcpConfigFromEnv({
    IND_MCP_URL: 'http://127.0.0.1:3100/mcp',
    IND_MCP_SERVER_NAME: 'gateway',
    IND_MCP_READONLY_TOOLS: 'notion__notion-search,notion__notion-fetch,notion__notion-create-pages',
  });
  assert.deepEqual(config, {
    serverName: 'gateway',
    url: 'http://127.0.0.1:3100/mcp',
    toolNames: ['notion__notion-search', 'notion__notion-fetch', 'notion__notion-create-pages'],
  });
});

test('thread agent base instructions prohibit implementation and require a main-agent handoff', () => {
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /inline discussion thread agent, not the main agent/i);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Never apply a requested repository, discussion-document, project-context, or code change or fix/);
  assert.match(THREAD_AGENT_BASE_INSTRUCTIONS, /Your response is the handoff to the main agent/);
});

test('discussion agents disable project-context-curator while preserving their environment', () => {
  assert.deepEqual(
    discussionAgentEnvironment({ KEEP_ME: 'yes', PROJECT_CONTEXT_CURATOR_DISABLED: '0' }),
    { KEEP_ME: 'yes', PROJECT_CONTEXT_CURATOR_DISABLED: '1' },
  );
});

test('codexAgentFactory streams app-server deltas and records replies', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-agent-'));
  const fakeServer = join(root, 'fake-codex-app-server.mjs');
  writeFileSync(fakeServer, fakeCodexAppServer());

  const factory = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root });
  const agent = factory({ systemPreamble: 'preamble', tools: [] });
  const chunks = await collectChunkLabels(agent.send('hello'));

  assert.deepEqual(chunks, [
    'status:Using Read...',
    'status:',
    'activity:tool:Tool:Used Read',
    'delta:streamed ',
    'delta:codex ',
    'delta:reply',
    'done:streamed codex reply',
  ]);
  assert.deepEqual(
    agent.snapshot().map((m) => [m.role, m.text]),
    [['user', 'hello'], ['assistant', 'streamed codex reply']],
  );
  await agent.close?.();
});

test('codexAgentFactory separates Codex activity from final answer text', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-activity-agent-'));
  const fakeServer = join(root, 'fake-codex-app-server.mjs');
  writeFileSync(fakeServer, fakeCodexActivityAppServer());

  const factory = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root });
  const agent = factory({ systemPreamble: 'preamble', tools: [] });
  const chunks = await collectChunkLabels(agent.send('hello'));

  assert.deepEqual(chunks, [
    'activity:commentary:Commentary:Checking context.',
    'activity:reasoning:Reasoning:Reading files.',
    'status:Running pwd...',
    'status:',
    'activity:tool:Tool:Ran pwd (exit 0)',
    'delta:Final answer',
    'done:Final answer',
  ]);
  assert.deepEqual(
    agent.snapshot().map((m) => [m.role, m.text]),
    [['user', 'hello'], ['assistant', 'Final answer']],
  );
  await agent.close?.();
});

test('codexAgentFactory uses app-server thread history for conclusions', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-conclusion-agent-'));
  const fakeServer = join(root, 'fake-codex-app-server.mjs');
  writeFileSync(fakeServer, fakeCodexAppServer());

  const factory = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root });
  const agent = factory({ systemPreamble: 'preamble', tools: [] });
  const chunks = await collectChunkLabels(agent.proposeConclusion());

  assert.deepEqual(chunks, ['delta:summary', 'done:summary']);
  await agent.close?.();
});

test('codexAgentFactory applies explicit inference settings to new and subsequent turns', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-inference-agent-'));
  const fakeServer = join(root, 'fake-codex-inference-server.mjs');
  writeFileSync(fakeServer, fakeCodexInferenceAppServer());
  const factory = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root });
  const agent = factory({
    systemPreamble: 'preamble',
    tools: [],
    inferenceSettings: { provider: 'openai', model: 'model-a', reasoningEffort: 'medium' },
  });
  try {
    assert.equal((await collectChunkLabels(agent.send('first'))).at(-1), 'done:model-a:medium');
    agent.setInferenceSettings?.({ provider: 'openai', model: 'model-b', reasoningEffort: 'high' });
    assert.equal((await collectChunkLabels(agent.send('second'))).at(-1), 'done:model-b:high');
  } finally {
    await agent.close?.();
  }
});

test('codexAgentFactory keeps retryable provider errors as status and exposes final details', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-provider-errors-'));
  const fakeServer = join(root, 'fake-codex-provider-errors.mjs');
  writeFileSync(fakeServer, fakeCodexProviderErrorAppServer());
  const agent = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root })({
    systemPreamble: 'preamble',
    tools: [],
  });
  try {
    assert.deepEqual(await collectChunkLabels(agent.send('retry')), [
      'status:Reconnecting... 1/5',
      'status:',
      'delta:recovered',
      'done:recovered',
    ]);
    await assert.rejects(() => collectChunkLabels(agent.send('fail')), /Request failed: provider unavailable/);
  } finally {
    await agent.close?.();
  }
});

test('discoverCodexInferenceCatalog uses the app-server runtime default', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-inference-catalog-'));
  const fakeServer = join(root, 'fake-codex-catalog-server.mjs');
  writeFileSync(fakeServer, fakeCodexCatalogAppServer());
  const catalog = await discoverCodexInferenceCatalog({ command: process.execPath, args: [fakeServer], cwd: root });
  assert.deepEqual(catalog.defaultSettings, {
    provider: 'openai',
    model: 'model-default',
    reasoningEffort: 'high',
  });
  assert.equal(catalog.models.length, 2);
  assert.equal(catalog.models[1]?.hidden, true);
});

test('codexAgentFactory routes MCP approval elicitations through the host callback', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-codex-mcp-approval-'));
  const fakeServer = join(root, 'fake-codex-mcp-app-server.mjs');
  writeFileSync(fakeServer, fakeCodexMcpApprovalAppServer());
  const requests: Array<{ provider: string; toolKey: string; toolName: string; input: Record<string, unknown> }> = [];

  const factory = codexAgentFactory({ command: process.execPath, args: [fakeServer], cwd: root });
  const agent = factory({
    systemPreamble: 'preamble',
    tools: [],
    requestToolApproval: async (request) => {
      requests.push(request);
      return { approved: true };
    },
  });
  try {
    const chunks = await collectChunkLabels(agent.send('use notion'));

    assert.equal(requests.length, 1);
    assert.equal(requests[0]?.provider, 'codex');
    assert.equal(requests[0]?.toolKey, 'mcp__notion__create_page');
    assert.equal(requests[0]?.toolName, 'create_page');
    assert.deepEqual(requests[0]?.input, { title: 'Roadmap' });
    assert.equal(chunks.at(-1), 'done:approved by user');
  } finally {
    await agent.close?.();
  }
});

function fakeCodexAppServer(): string {
  return `
import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const threadId = 'thread-1';
let turnSeq = 0;

function send(message) {
  console.log(JSON.stringify(message));
}

function fail(id, message) {
  send({ id, error: { message } });
}

rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.method === 'initialize') {
    if (msg.params.capabilities?.experimentalApi !== true) return fail(msg.id, 'experimental app-server API must be enabled');
    send({ id: msg.id, result: { userAgent: 'fake', codexHome: '.', platformFamily: 'unix', platformOs: 'macos' } });
    return;
  }
  if (msg.method === 'initialized') return;
  if (msg.method === 'thread/start') {
    if (process.env.PROJECT_CONTEXT_CURATOR_DISABLED !== '1') return fail(msg.id, 'project-context curator must be disabled');
    if (msg.params.approvalPolicy !== 'never') return fail(msg.id, 'approval policy missing');
    if (msg.params.sandbox !== 'read-only') return fail(msg.id, 'read-only thread sandbox missing');
    if (!String(msg.params.developerInstructions).includes('preamble')) return fail(msg.id, 'developer instructions missing');
    if (!String(msg.params.developerInstructions).includes('inline discussion thread agent, not the main agent')) return fail(msg.id, 'thread role instructions missing');
    send({ id: msg.id, result: { thread: { id: threadId } } });
    send({ method: 'thread/started', params: { threadId } });
    return;
  }
  if (msg.method === 'turn/start') {
    if (msg.params.approvalPolicy !== 'never') return fail(msg.id, 'turn approval policy missing');
    if (msg.params.sandboxPolicy?.type !== 'readOnly') return fail(msg.id, 'read-only turn sandbox missing');
    if (msg.params.sandboxPolicy?.networkAccess !== false) return fail(msg.id, 'network access must be disabled');
    turnSeq += 1;
    const turnId = \`turn-\${turnSeq}\`;
    const text = msg.params.input?.[0]?.text ?? '';
    const deltas = text.startsWith('Conclude this thread now.') ? ['summary'] : ['streamed ', 'codex ', 'reply'];
    const answer = deltas.join('');
    send({ id: msg.id, result: {} });
    send({ method: 'turn/started', params: { threadId, turn: { id: turnId, status: 'inProgress' } } });
    if (!text.startsWith('Conclude this thread now.')) {
      send({ method: 'item/started', params: { threadId, turnId, item: { id: 'tool-1', type: 'mcpToolCall', toolName: 'Read' } } });
      send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'tool-1', type: 'mcpToolCall', toolName: 'Read' } } });
    }
    for (const delta of deltas) {
      send({ method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'item-1', delta } });
    }
    send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'item-1', type: 'agentMessage', text: answer } } });
    send({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
    return;
  }
  fail(msg.id, \`unexpected method: \${msg.method}\`);
});
`;
}

function fakeCodexInferenceAppServer(): string {
  return `
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let turnSeq = 0;
function send(message) { console.log(JSON.stringify(message)); }
function fail(id, message) { send({ id, error: { message } }); }
rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.method === 'initialize') return send({ id: msg.id, result: {} });
  if (msg.method === 'initialized') return;
  if (msg.method === 'thread/start') {
    if (msg.params.model !== 'model-a' || msg.params.modelProvider !== 'openai') return fail(msg.id, 'thread settings missing');
    return send({ id: msg.id, result: { thread: { id: 'thread-1' } } });
  }
  if (msg.method === 'turn/start') {
    turnSeq += 1;
    const expected = turnSeq === 1
      ? { model: 'model-a', effort: 'medium' }
      : { model: 'model-b', effort: 'high' };
    if (msg.params.modelProvider !== 'openai' || msg.params.model !== expected.model || msg.params.effort !== expected.effort) {
      return fail(msg.id, 'turn settings missing');
    }
    const collaborationMode = msg.params.collaborationMode;
    if (collaborationMode?.mode !== 'default') return fail(msg.id, 'default collaboration mode missing');
    if (collaborationMode.settings?.model !== expected.model || collaborationMode.settings?.reasoning_effort !== expected.effort) {
      return fail(msg.id, 'collaboration inference settings missing');
    }
    if (!String(collaborationMode.settings?.developer_instructions).includes('inline discussion thread agent, not the main agent')) {
      return fail(msg.id, 'turn developer instructions missing');
    }
    const turnId = 'turn-' + turnSeq;
    const text = expected.model + ':' + expected.effort;
    send({ id: msg.id, result: {} });
    send({ method: 'turn/started', params: { threadId: 'thread-1', turn: { id: turnId, status: 'inProgress' } } });
    send({ method: 'item/agentMessage/delta', params: { threadId: 'thread-1', turnId, itemId: 'item-1', delta: text } });
    send({ method: 'turn/completed', params: { threadId: 'thread-1', turn: { id: turnId, status: 'completed' } } });
    return;
  }
  fail(msg.id, 'unexpected method: ' + msg.method);
});
`;
}

function fakeCodexProviderErrorAppServer(): string {
  return `
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let turnSeq = 0;
function send(message) { console.log(JSON.stringify(message)); }
rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.method === 'initialize') return send({ id: msg.id, result: {} });
  if (msg.method === 'initialized') return;
  if (msg.method === 'thread/start') return send({ id: msg.id, result: { thread: { id: 'thread-1' } } });
  if (msg.method !== 'turn/start') return;
  turnSeq += 1;
  const turnId = 'turn-' + turnSeq;
  send({ id: msg.id, result: {} });
  send({ method: 'turn/started', params: { threadId: 'thread-1', turn: { id: turnId, status: 'inProgress' } } });
  if (turnSeq === 1) {
    send({ method: 'error', params: { error: { message: 'Reconnecting... 1/5', additionalDetails: 'temporary' }, willRetry: true, threadId: 'thread-1', turnId } });
    send({ method: 'item/agentMessage/delta', params: { threadId: 'thread-1', turnId, itemId: 'item-1', delta: 'recovered' } });
    send({ method: 'turn/completed', params: { threadId: 'thread-1', turn: { id: turnId, status: 'completed' } } });
    return;
  }
  send({ method: 'error', params: { error: { message: 'Request failed', additionalDetails: 'provider unavailable' }, willRetry: false, threadId: 'thread-1', turnId } });
});
`;
}

function fakeCodexCatalogAppServer(): string {
  return `
import readline from 'node:readline';
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
function send(message) { console.log(JSON.stringify(message)); }
rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.method === 'initialize') return send({ id: msg.id, result: {} });
  if (msg.method === 'initialized') return;
  if (msg.method === 'model/list') return send({ id: msg.id, result: { data: [
    { providerId: 'openai', model: 'model-default', displayName: 'Default', description: '', hidden: false, isDefault: true, defaultReasoningEffort: 'medium', supportedReasoningEfforts: [
      { reasoningEffort: 'medium', description: '' }, { reasoningEffort: 'high', description: '' }
    ] },
    { providerId: 'openai', model: 'model-hidden', displayName: 'Hidden', description: '', hidden: true, isDefault: false, defaultReasoningEffort: 'medium', supportedReasoningEfforts: [
      { reasoningEffort: 'medium', description: '' }
    ] }
  ] } });
  if (msg.method === 'config/read') return send({ id: msg.id, result: { config: {
    model_provider: 'openai', model: 'model-default', model_reasoning_effort: 'high'
  } } });
});
`;
}

function fakeCodexActivityAppServer(): string {
  return `
import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const threadId = 'thread-1';

function send(message) {
  console.log(JSON.stringify(message));
}

function fail(id, message) {
  send({ id, error: { message } });
}

rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.method === 'initialize') {
    send({ id: msg.id, result: { userAgent: 'fake', codexHome: '.', platformFamily: 'unix', platformOs: 'macos' } });
    return;
  }
  if (msg.method === 'initialized') return;
  if (msg.method === 'thread/start') {
    send({ id: msg.id, result: { thread: { id: threadId } } });
    send({ method: 'thread/started', params: { threadId } });
    return;
  }
  if (msg.method === 'turn/start') {
    const turnId = 'turn-1';
    send({ id: msg.id, result: {} });
    send({ method: 'turn/started', params: { threadId, turn: { id: turnId, status: 'inProgress' } } });
    send({ method: 'item/started', params: { threadId, turnId, item: { id: 'comment-1', type: 'agentMessage', text: '', phase: 'commentary' } } });
    send({ method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'comment-1', delta: 'Checking context.' } });
    send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'comment-1', type: 'agentMessage', text: 'Checking context.', phase: 'commentary' } } });
    send({ method: 'item/started', params: { threadId, turnId, item: { id: 'reason-1', type: 'reasoning', summary: [], content: [] } } });
    send({ method: 'item/reasoning/summaryTextDelta', params: { threadId, turnId, itemId: 'reason-1', delta: 'Reading files.', summaryIndex: 0 } });
    send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'reason-1', type: 'reasoning', summary: ['Reading files.'], content: [] } } });
    send({ method: 'item/started', params: { threadId, turnId, item: { id: 'cmd-1', type: 'commandExecution', command: '/bin/zsh -lc pwd', commandActions: [{ type: 'unknown', command: 'pwd' }], status: 'inProgress' } } });
    send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'cmd-1', type: 'commandExecution', command: '/bin/zsh -lc pwd', commandActions: [{ type: 'unknown', command: 'pwd' }], status: 'completed', exitCode: 0 } } });
    send({ method: 'item/started', params: { threadId, turnId, item: { id: 'answer-1', type: 'agentMessage', text: '', phase: 'final_answer' } } });
    send({ method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'answer-1', delta: 'Final answer' } });
    send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'answer-1', type: 'agentMessage', text: 'Final answer', phase: 'final_answer' } } });
    send({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
    return;
  }
  fail(msg.id, \`unexpected method: \${msg.method}\`);
});
`;
}

function fakeCodexMcpApprovalAppServer(): string {
  return `
import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const threadId = 'thread-approval';
const turnId = 'turn-approval';

function send(message) { console.log(JSON.stringify(message)); }
function complete() {
  send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'call-1', type: 'mcpToolCall', server: 'notion', tool: 'create_page', arguments: { title: 'Roadmap' }, status: 'completed' } } });
  send({ method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'answer-1', delta: 'approved by user' } });
  send({ method: 'item/completed', params: { threadId, turnId, item: { id: 'answer-1', type: 'agentMessage', text: 'approved by user' } } });
  send({ method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
}

rl.on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.id === 'approval-1' && !msg.method) {
    if (msg.result?.action !== 'accept') throw new Error('MCP call was not approved');
    complete();
    return;
  }
  if (msg.method === 'initialize') {
    send({ id: msg.id, result: { userAgent: 'fake', codexHome: '.', platformFamily: 'unix', platformOs: 'macos' } });
    return;
  }
  if (msg.method === 'initialized') return;
  if (msg.method === 'thread/start') {
    send({ id: msg.id, result: { thread: { id: threadId } } });
    return;
  }
  if (msg.method === 'turn/start') {
    send({ id: msg.id, result: {} });
    send({ method: 'turn/started', params: { threadId, turn: { id: turnId, status: 'inProgress' } } });
    send({ method: 'item/started', params: { threadId, turnId, item: { id: 'call-1', type: 'mcpToolCall', server: 'notion', tool: 'create_page', arguments: { title: 'Roadmap' }, status: 'inProgress' } } });
    send({
      id: 'approval-1',
      method: 'mcpServer/elicitation/request',
      params: {
        threadId,
        turnId,
        serverName: 'notion',
        mode: 'form',
        message: 'Allow Notion to create a page?',
        requestedSchema: { type: 'object', properties: {} },
        _meta: {
          codex_approval_kind: 'mcp_tool_call',
          tool_title: 'Create Page',
          tool_params: { title: 'Roadmap' },
        },
      },
    });
  }
});
`;
}

// Shape-only fixtures matching SDKMessage fields dispatchSdkMessage reads.
function deltaEvt(text: string): unknown {
  return { type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text } } };
}
function assistantEvt(text: string): unknown {
  return { type: 'assistant', message: { content: [{ type: 'text', text }] } };
}
function resultEvt(result?: string): unknown {
  return result === undefined
    ? { type: 'result', subtype: 'error_during_execution' }
    : { type: 'result', subtype: 'success', result };
}
function blockStartEvt(blockType: 'text' | 'tool_use'): unknown {
  return { type: 'stream_event', event: { type: 'content_block_start', index: 0, content_block: { type: blockType } } };
}
function toolStartEvt(name: string): unknown {
  return { type: 'stream_event', event: { type: 'content_block_start', index: 0, content_block: { type: 'tool_use', name } } };
}

test('dispatchSdkMessage ignores intermediate assistant messages (multi-turn tool use)', () => {
  const state: DispatchState = { accumulated: '' };

  // Simulated turn: partial text deltas → intermediate assistant (with tool_use) →
  // more text deltas → final result. Emitting `done` on the intermediate assistant
  // truncated replies at the first tool call (VC-0 regression).
  const { chunks: all, endSeen } = dispatchEvents([
    deltaEvt('Part 1.'),
    deltaEvt(' '),
    assistantEvt('Part 1. (intermediate)'),
    deltaEvt('Part 2.'),
    resultEvt('Part 1. Part 2.'),
  ], state);

  assert.equal(endSeen, true);
  const doneCount = all.filter(isDone).length;
  assert.equal(doneCount, 1, 'exactly one done per turn');
  const done = all.find(isDone)!;
  assert.equal(done.text, 'Part 1. Part 2.');
  assert.deepEqual(
    all.filter(isDelta).map((c) => c.text),
    ['Part 1.', ' ', 'Part 2.'],
  );
});

test('dispatchSdkMessage falls back to accumulated deltas on non-success result', () => {
  const state: DispatchState = { accumulated: '' };
  const { chunks: all } = dispatchEvents([
    deltaEvt('hello '),
    deltaEvt('world'),
    resultEvt(/* no success payload */),
  ], state);
  const done = all.find(isDone)!;
  assert.equal(done.text, 'hello world');
});

test('dispatchSdkMessage maps an interrupted SDK result to an interrupted chunk', () => {
  const state: DispatchState = { accumulated: '' };
  const result = dispatchSdkMessage({ type: 'result', subtype: 'interrupted' }, state);
  assert.deepEqual(result.chunks, [{ type: 'interrupted' }]);
  assert.equal(result.endOfTurn, true);
});

test('dispatchSdkMessage inserts paragraph break at new text-block boundaries (tool-pause spacing)', () => {
  const state: DispatchState = { accumulated: '' };
  // First text block opens, model writes a sentence, tool runs, second text block opens.
  // Without the boundary insert, the streamed text would read "Foo.Bar." with no space.
  const { chunks: all } = dispatchEvents([
    blockStartEvt('text'),
    deltaEvt('Foo.'),
    blockStartEvt('tool_use'),
    blockStartEvt('text'),
    deltaEvt('Bar.'),
  ], state);
  assert.equal(state.accumulated, 'Foo.\n\nBar.');
  assert.deepEqual(
    all.filter(isDelta).map((c) => c.text),
    ['Foo.', '\n\n', 'Bar.'],
  );
});

test('dispatchSdkMessage emits and clears tool-use status', () => {
  const state: DispatchState = { accumulated: '' };
  const { chunks: all } = dispatchEvents([toolStartEvt('WebSearch'), deltaEvt('Found it.')], state);
  assert.deepEqual(all.map(chunkLabel), ['status:Using WebSearch...', 'status:', 'delta:Found it.']);
});

test('dispatchSdkMessage does not insert a paragraph break before the first text block', () => {
  const state: DispatchState = { accumulated: '' };
  const { chunks: all } = dispatchEvents([blockStartEvt('text'), deltaEvt('Hello.')], state);
  assert.equal(state.accumulated, 'Hello.');
  assert.deepEqual(all.filter(isDelta).map((c) => c.text), ['Hello.']);
});

test('dispatchSdkMessage does not double-up newlines when prior block already ends with \\n\\n', () => {
  const state: DispatchState = { accumulated: '' };
  dispatchEvents([
    blockStartEvt('text'),
    deltaEvt('First.\n\n'),
    blockStartEvt('text'),
    deltaEvt('Second.'),
  ], state);
  assert.equal(state.accumulated, 'First.\n\nSecond.');
});

test('dispatchSdkMessage resets accumulator between turns', () => {
  const state: DispatchState = { accumulated: '' };
  // Turn 1
  dispatchSdkMessage(deltaEvt('first'), state);
  dispatchSdkMessage(resultEvt('first'), state);
  assert.equal(state.accumulated, '');
  assert.equal(state.status, null);
  // Turn 2
  dispatchSdkMessage(deltaEvt('second'), state);
  const { chunks } = dispatchSdkMessage(resultEvt(), state);
  const done = chunks.find(isDone)!;
  assert.equal(done.text, 'second');
});
