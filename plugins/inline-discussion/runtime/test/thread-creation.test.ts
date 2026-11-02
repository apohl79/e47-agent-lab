import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { request as httpRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import { mockAgentFactory, type AgentFactory } from '../src/agent.ts';
import type { MainSessionBridge } from '../src/main-session.ts';

type ThreadCreatedEvent = {
  thread: {
    messages: Array<{ role: string; text: string }>;
  };
};

function scratchSession(doc: string) {
  const root = mkdtempSync(join(tmpdir(), 'ind-thread-creation-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, doc);
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, '{"type":"user","text":"hi"}');
  return {
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    transcriptPath,
  };
}

async function connectThreadCreated(port: number): Promise<{
  event: Promise<ThreadCreatedEvent>;
  close: () => void;
}> {
  const controller = new AbortController();
  const response = await fetch(`http://127.0.0.1:${port}/events`, { signal: controller.signal });
  const reader = response.body?.getReader();
  if (!reader) throw new Error('SSE response has no body');
  let resolveEvent: (event: ThreadCreatedEvent) => void = () => undefined;
  let rejectEvent: (error: Error) => void = () => undefined;
  const event = new Promise<ThreadCreatedEvent>((resolve, reject) => {
    resolveEvent = resolve;
    rejectEvent = reject;
  });
  const decoder = new TextDecoder();
  let buffer = '';
  void (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value);
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const eventName = frame.match(/^event: (.+)$/m)?.[1];
          if (eventName === 'thread.created') {
            const data = frame.match(/^data: (.+)$/m)?.[1];
            if (data) resolveEvent(JSON.parse(data) as ThreadCreatedEvent);
            return;
          }
          boundary = buffer.indexOf('\n\n');
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) rejectEvent(error instanceof Error ? error : new Error(String(error)));
    }
  })();
  return { event, close: () => controller.abort() };
}

test('thread.created includes the initial user message for a new assistant thread', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const events = await connectThreadCreated(port);
  try {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'why?' }),
    });
    assert.equal(response.status, 200);
    const created = await events.event;
    assert.equal(created.thread.messages[0]?.role, 'user');
    assert.equal(created.thread.messages[0]?.text, 'why?');
  } finally {
    events.close();
    await close();
  }
});

test('POST /api/threads streams a main-agent thread through the live main session', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const prompts: string[] = [];
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    mainSession: {
      send: async (prompt) => { prompts.push(prompt); },
      async *stream(prompt) {
        prompts.push(prompt);
        yield { type: 'delta', text: 'Main ' };
        yield { type: 'done', text: `Main reply ${prompts.length}` };
      },
    },
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  type BootThread = {
    recipient?: string;
    inferenceSettings?: unknown;
    messages: Array<{ role: string; text: string }>;
  };
  const threadsAfter = async (messageCount: number): Promise<BootThread[]> => {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { threads: BootThread[] };
      if ((boot.threads[0]?.messages.length ?? 0) >= messageCount) return boot.threads;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error(`thread did not reach ${messageCount} messages`);
  };
  try {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
      blockIds: string[];
      canSendToMainSession: boolean;
    };
    assert.equal(boot.canSendToMainSession, true);
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        anchor: { blockId: boot.blockIds[1], quote: 'Anchor paragraph.' },
        message: 'Please revise this directly.',
        recipient: 'main-agent',
      }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { threadId: 't-1', kind: 'thread' });
    const [thread] = await threadsAfter(2);
    assert.equal(thread?.recipient, 'main-agent');
    assert.equal(thread?.inferenceSettings, undefined);
    assert.deepEqual(thread?.messages.map((message) => [message.role, message.text]), [
      ['user', 'Please revise this directly.'],
      ['assistant', 'Main reply 1'],
    ]);
    assert.match(prompts[0]!, /Treat it as a request in this main session/);
    assert.match(prompts[0]!, /inline-discussion thread t-1/);
    assert.match(prompts[0]!, /Anchor paragraph\./);
    assert.match(prompts[0]!, /Please revise this directly\./);

    const followUp = await fetch(`http://127.0.0.1:${port}/api/threads/t-1/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message: 'And the next paragraph?' }),
    });
    assert.equal(followUp.status, 202);
    const [continued] = await threadsAfter(4);
    assert.equal(continued?.messages.at(-1)?.text, 'Main reply 2');
    assert.match(prompts[1]!, /And the next paragraph\?/);
    assert.match(prompts[1]!, /Anchor paragraph\./);
  } finally {
    await close();
  }
});

test('POST /api/threads lets only one main-agent thread own the main-session turn', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  let releaseTurn: () => void = () => undefined;
  const turnReleased = new Promise<void>((resolve) => { releaseTurn = resolve; });
  const interrupts: string[] = [];
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    mainSession: {
      send: async () => undefined,
      interrupt: async (turnId) => { interrupts.push(turnId); },
      async *stream(_prompt, onTurnStarted) {
        await turnReleased;
        onTurnStarted?.('turn-1');
        yield { type: 'done', text: 'Main reply' };
      },
    },
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const createMainThread = async (message: string): Promise<Response> => {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
    return fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message, recipient: 'main-agent' }),
    });
  };
  try {
    assert.equal((await createMainThread('first')).status, 200);
    const busy = await createMainThread('second');
    assert.equal(busy.status, 409);
    const payload = (await busy.json()) as { error: string; message: string };
    assert.equal(payload.error, 'main-agent-busy');
    assert.match(payload.message, /thread t-1/);
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { threads: unknown[] };
    assert.equal(boot.threads.length, 1);
    const deleted = await fetch(`http://127.0.0.1:${port}/api/threads/t-1`, { method: 'DELETE' });
    assert.equal(deleted.status, 409);
    assert.equal(((await deleted.json()) as { error: string }).error, 'main-agent-reply-active');
    const converted = await fetch(`http://127.0.0.1:${port}/api/threads/t-1/convert`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ to: 'note' }),
    });
    assert.equal(converted.status, 409);
    assert.equal((await createMainThread('second')).status, 409);
    const post = (path: string, body: unknown = {}) => fetch(`http://127.0.0.1:${port}${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const closed = await post('/api/threads/t-1/close', { conclusion: 'partial' });
    assert.equal(closed.status, 409);
    assert.equal(((await closed.json()) as { error: string }).error, 'main-agent-reply-active');
    assert.equal((await post('/api/apply')).status, 409);
    assert.equal((await post('/api/finish')).status, 409);
    const interrupted = await post('/api/threads/t-1/interrupt');
    assert.equal(interrupted.status, 502);
    assert.deepEqual(interrupts, []);

    releaseTurn();
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const response = await createMainThread('second');
      if (response.status === 200) {
        const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
          threads: Array<{ id: string; messages: Array<{ role: string; text: string }> }>;
        };
        assert.equal(boot.threads.find((thread) => thread.id === 't-1')?.messages.at(-1)?.text, 'Main reply');
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.fail('second main-agent thread was never accepted');
  } finally {
    await close();
  }
});

test('POST /api/threads rejects main-agent recipients without a live main-session bridge', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  try {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
      blockIds: string[];
      canSendToMainSession: boolean;
    };
    assert.equal(boot.canSendToMainSession, false);
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        anchor: { blockId: boot.blockIds[1] },
        message: 'Please revise this directly.',
        recipient: 'main-agent',
      }),
    });
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), {
      ok: false,
      error: 'main-session-direct-input-unavailable',
    });
  } finally {
    await close();
  }
});

type Gate = Readonly<{ wait: Promise<void>; open: () => void }>;

function gate(): Gate {
  let open: () => void = () => undefined;
  const wait = new Promise<void>((resolve) => { open = resolve; });
  return { wait, open };
}

function partialPost(port: number, path: string, firstChunk: string): {
  finish: (rest: string) => Promise<{ status: number; body: Record<string, unknown> }>;
} {
  let pending: (rest: string) => void = () => undefined;
  const response = new Promise<{ status: number; body: Record<string, unknown> }>((resolve, reject) => {
    const req = httpRequest({
      host: '127.0.0.1',
      port,
      path,
      method: 'POST',
      headers: { 'content-type': 'application/json' },
    }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk: string) => { raw += chunk; });
      res.on('end', () => resolve({ status: res.statusCode ?? 0, body: raw ? JSON.parse(raw) as Record<string, unknown> : {} }));
    });
    req.on('error', reject);
    req.write(firstChunk);
    pending = (rest) => req.end(rest);
  });
  return {
    finish: async (rest) => {
      pending(rest);
      return response;
    },
  };
}

async function waitFor(check: () => Promise<boolean>): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail('condition was never met');
}

async function startMainSessionServer(mainSession: MainSessionBridge, agentFactory?: AgentFactory) {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const server = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    mainSession,
    agentFactory: agentFactory ?? mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const base = `http://127.0.0.1:${server.port}`;
  const boot = (await (await fetch(`${base}/api/bootstrap`)).json()) as { blockIds: string[] };
  const post = (path: string, body: unknown) => fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const createThread = (message: string, recipient: 'main-agent' | 'thread-agent' = 'main-agent') =>
    post('/api/threads', { anchor: { blockId: boot.blockIds[1] }, message, recipient });
  const threads = async () =>
    ((await (await fetch(`${base}/api/bootstrap`)).json()) as {
      threads: Array<{ id: string; status: string; messages: Array<{ role: string; text: string }> }>;
    }).threads;
  return { ...server, post, createThread, threads };
}

test('thread close rechecks main-session ownership after reading its body', async () => {
  const secondTurn = gate();
  let turns = 0;
  const server = await startMainSessionServer({
    send: async () => undefined,
    async *stream() {
      turns += 1;
      if (turns > 1) await secondTurn.wait;
      yield { type: 'done', text: `answer ${turns}` };
    },
  });
  try {
    assert.equal((await server.createThread('first')).status, 200);
    await waitFor(async () => (await server.threads())[0]?.messages.length === 2);
    const close = partialPost(server.port, '/api/threads/t-1/close', '{"conclusion":');
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal((await server.post('/api/threads/t-1/messages', { message: 'second question' })).status, 202);
    const closed = await close.finish('"partial"}');
    assert.equal(closed.status, 409);
    assert.equal(closed.body['error'], 'main-agent-reply-active');
    secondTurn.open();
    await waitFor(async () => (await server.threads())[0]?.messages.at(-1)?.text === 'answer 2');
    assert.equal((await server.threads())[0]?.status, 'open');
  } finally {
    secondTurn.open();
    await server.close();
  }
});

test('Apply rechecks main-session ownership after reading its body', async () => {
  const turn = gate();
  const handoffs: string[] = [];
  const server = await startMainSessionServer({
    send: async (prompt) => { handoffs.push(prompt); },
    async *stream() {
      await turn.wait;
      yield { type: 'done', text: 'answer' };
    },
  });
  try {
    const apply = partialPost(server.port, '/api/apply', '{"removeThreads":');
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal((await server.createThread('question')).status, 200);
    const applied = await apply.finish('false}');
    assert.equal(applied.status, 409);
    assert.equal(applied.body['error'], 'main-agent-busy');
    assert.deepEqual(handoffs, []);
  } finally {
    turn.open();
    await server.close();
  }
});

test('main-agent steering waits for the main-session turn id and targets it', async () => {
  const turnStarted = gate();
  const turnDone = gate();
  const steers: Array<[string, string]> = [];
  const sends: string[] = [];
  const server = await startMainSessionServer({
    send: async (prompt) => { sends.push(prompt); },
    steer: async (turnId, prompt) => { steers.push([turnId, prompt]); },
    async *stream(_prompt, onTurnStarted) {
      await turnStarted.wait;
      onTurnStarted?.('turn-1');
      await turnDone.wait;
      yield { type: 'done', text: 'main answer' };
    },
  });
  try {
    assert.equal((await server.createThread('first')).status, 200);
    const early = await server.post('/api/threads/t-1/messages', { message: 'too early' });
    assert.equal(early.status, 409);
    assert.deepEqual(steers, []);
    assert.deepEqual(sends, []);

    turnStarted.open();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal((await server.post('/api/threads/t-1/messages', { message: 'steer now' })).status, 202);
    assert.equal(steers.length, 1);
    assert.equal(steers[0]?.[0], 'turn-1');
    assert.match(steers[0]?.[1] ?? '', /steer now/);
    assert.deepEqual(sends, []);
  } finally {
    turnStarted.open();
    turnDone.open();
    await server.close();
  }
});

for (const admission of ['create', 'message'] as const) {
  test(`main-agent ${admission} admission rechecks Apply after reading its body`, async () => {
    const conclusion = gate();
    const handoffs: string[] = [];
    const streams: string[] = [];
    const slowConclusionFactory: AgentFactory = () => ({
      async *send() { yield { type: 'done', text: 'thread answer' }; },
      async *proposeConclusion() {
        await conclusion.wait;
        yield { type: 'done', text: 'thread conclusion' };
      },
      snapshot: () => [],
    });
    const server = await startMainSessionServer({
      send: async (prompt) => { handoffs.push(prompt); },
      async *stream(prompt) {
        streams.push(prompt);
        yield { type: 'done', text: 'main answer' };
      },
    }, slowConclusionFactory);
    try {
      assert.equal((await server.createThread('side question', 'thread-agent')).status, 200);
      if (admission === 'message') {
        assert.equal((await server.createThread('main question')).status, 200);
        await waitFor(async () => (await server.threads()).find((thread) => thread.id === 't-2')?.messages.length === 2);
      }
      await waitFor(async () => (await server.threads()).every((thread) => thread.messages.length === 2));
      const streamsBefore = streams.length;
      const boot = (await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json()) as { blockIds: string[] };
      const pending = admission === 'create'
        ? partialPost(server.port, '/api/threads', `{"anchor":{"blockId":"${boot.blockIds[1]}"},"recipient":"main-agent","message":`)
        : partialPost(server.port, '/api/threads/t-2/messages', '{"message":');
      await new Promise((resolve) => setTimeout(resolve, 20));
      const apply = server.post('/api/apply', {});
      await new Promise((resolve) => setTimeout(resolve, 20));
      const admitted = await pending.finish('"late question"}');
      assert.equal(admitted.status, 409);
      assert.equal(admitted.body['error'], 'applying');
      conclusion.open();
      assert.equal((await apply).status, 200);
      assert.equal(handoffs.length, 1);
      assert.equal(streams.length, streamsBefore);
    } finally {
      conclusion.open();
      await server.close();
    }
  });
}

test('Finish holds the main session through archiving and handoff', async () => {
  const conclusion = gate();
  const handoffs: string[] = [];
  const streams: string[] = [];
  const slowConclusionFactory: AgentFactory = () => ({
    async *send() { yield { type: 'done', text: 'thread answer' }; },
    async *proposeConclusion() {
      await conclusion.wait;
      yield { type: 'done', text: 'thread conclusion' };
    },
    snapshot: () => [],
  });
  const server = await startMainSessionServer({
    send: async (prompt) => { handoffs.push(prompt); },
    async *stream(prompt) {
      streams.push(prompt);
      yield { type: 'done', text: 'main answer' };
    },
  }, slowConclusionFactory);
  try {
    assert.equal((await server.createThread('side question', 'thread-agent')).status, 200);
    await waitFor(async () => (await server.threads())[0]?.messages.length === 2);
    const finish = server.post('/api/finish', {});
    await new Promise((resolve) => setTimeout(resolve, 20));
    const during = await server.createThread('late main question');
    assert.equal(during.status, 409);
    assert.equal(((await during.json()) as { error: string }).error, 'main-session-busy');
    conclusion.open();
    assert.equal((await finish).status, 200);
    assert.equal(handoffs.length, 1);
    assert.equal((await server.createThread('after finish')).status, 409);
    assert.deepEqual(streams, []);
  } finally {
    conclusion.open();
    await server.close();
  }
});
