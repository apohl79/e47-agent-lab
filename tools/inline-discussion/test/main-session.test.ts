import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createServer, type Socket } from 'node:net';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  appServerSocketPath,
  createAppServerSessionBridge,
  listAppServerSessions,
} from '../src/main-session.ts';

test('appServerSocketPath isolates Xedoc from Codex environment variables', () => {
  assert.equal(
    appServerSocketPath('xedoc', { XEDOC_HOME: '/tmp/xedoc-home', CODEX_APP_SERVER_SOCKET: '/tmp/codex.sock' }, '/tmp/home'),
    '/tmp/xedoc-home/app-server-control/app-server-control.sock',
  );
  assert.equal(
    appServerSocketPath('xedoc', { XEDOC_APP_SERVER_SOCKET: '/tmp/xedoc.sock' }, '/tmp/home'),
    '/tmp/xedoc.sock',
  );
  assert.equal(
    appServerSocketPath('xedoc', {}, '/tmp/home'),
    '/tmp/home/.xedoc/app-server-control/app-server-control.sock',
  );
});

test('listAppServerSessions discovers sessions through the app-server websocket protocol', async () => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'ind-app-server-')), 'control.sock');
  const server = createServer((socket) => serveAppServer(socket));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => resolve());
  });

  try {
    const sessions = await listAppServerSessions({ socketPath, timeoutMs: 1_000 });
    assert.deepEqual(sessions, [
      { id: 'thread-1', name: 'Main session', status: { type: 'idle' }, canAcceptDirectInput: true },
      { id: 'thread-2', name: null, status: { type: 'active' }, canAcceptDirectInput: false },
    ]);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('app-server bridge steers an active paginated thread from resume data', async () => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'ind-app-server-')), 'control.sock');
  const requests: Record<string, unknown>[] = [];
  const server = createServer((socket) => serveAppServer(socket, (request) => requests.push(request)));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => resolve());
  });

  try {
    await createAppServerSessionBridge({ threadId: 'thread-1', socketPath, timeoutMs: 1_000 })
      .send('Handle the Apply signal.');

    assert.deepEqual(requests.map((request) => request['method']), [
      'initialize',
      'thread/resume',
      'turn/steer',
    ]);
    assert.deepEqual(requests.at(-1)?.['params'], {
      threadId: 'thread-1',
      input: [{ type: 'text', text: 'Handle the Apply signal.' }],
      expectedTurnId: 'turn-1',
    });
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('app-server bridge steers the explicit main-session turn', async () => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'ind-app-server-')), 'control.sock');
  const requests: Record<string, unknown>[] = [];
  const server = createServer((socket) => serveAppServer(socket, (request) => requests.push(request)));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => resolve());
  });

  try {
    await createAppServerSessionBridge({ threadId: 'thread-1', socketPath, timeoutMs: 1_000 })
      .steer!('turn-7', 'Follow-up.');
    assert.deepEqual(requests.map((request) => request['method']), ['initialize', 'thread/resume', 'turn/steer']);
    assert.deepEqual(requests.at(-1)?.['params'], {
      threadId: 'thread-1',
      input: [{ type: 'text', text: 'Follow-up.' }],
      expectedTurnId: 'turn-7',
    });
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('app-server bridge streams the started main-session turn back', async () => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'ind-app-server-')), 'control.sock');
  const requests: Record<string, unknown>[] = [];
  const server = createServer((socket) => serveAppServer(socket, (request) => requests.push(request), 'idle'));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => resolve());
  });

  try {
    const startedTurns: string[] = [];
    const bridge = createAppServerSessionBridge({ threadId: 'thread-1', socketPath, timeoutMs: 1_000 });
    const chunks = [];
    for await (const chunk of bridge.stream!('Explain this selection.', (turnId) => startedTurns.push(turnId))) {
      chunks.push(chunk);
    }

    assert.deepEqual(requests.map((request) => request['method']), ['initialize', 'thread/resume', 'turn/start']);
    assert.deepEqual(startedTurns, ['turn-2']);
    assert.deepEqual(chunks, [
      { type: 'delta', text: 'Hel' },
      { type: 'activity', activity: { kind: 'commentary', title: 'Commentary', text: 'Checking' } },
      { type: 'delta', text: 'lo' },
      { type: 'done', text: 'Hello' },
    ]);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('app-server bridge persists authoritative text when joining an active turn mid-item', async () => {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'ind-app-server-')), 'control.sock');
  const server = createServer((socket) => serveAppServer(socket, () => undefined, 'active-stream'));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => resolve());
  });

  try {
    const bridge = createAppServerSessionBridge({ threadId: 'thread-1', socketPath, timeoutMs: 1_000 });
    const chunks = [];
    for await (const chunk of bridge.stream!('Continue here.')) chunks.push(chunk);
    assert.deepEqual(chunks, [
      { type: 'activity', activity: { kind: 'commentary', title: 'Commentary', text: 'thinking' } },
      { type: 'delta', text: 'prefix tail' },
      { type: 'done', text: 'prefix tail' },
    ]);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

type AppServerScenario = 'active' | 'active-stream' | 'idle';

function serveAppServer(
  socket: Socket,
  onRequest: (request: Record<string, unknown>) => void = () => undefined,
  scenario: AppServerScenario = 'active',
): void {
  let buffer = Buffer.alloc(0);
  let upgraded = false;
  socket.on('data', (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk]);
    if (!upgraded) {
      const end = buffer.indexOf(Buffer.from('\r\n\r\n'));
      if (end === -1) return;
      const request = buffer.subarray(0, end + 4).toString('latin1');
      buffer = buffer.subarray(end + 4);
      const key = request.match(/Sec-WebSocket-Key: ([^\r\n]+)/i)?.[1];
      if (!key) return socket.destroy();
      const accept = createHash('sha1')
        .update(`${key.trim()}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest('base64');
      socket.write(
        `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
      );
      upgraded = true;
    }
    while (upgraded) {
      const message = readClientFrame();
      if (!message) return;
      handleRequest(message, socket, onRequest, scenario);
    }
  });
  socket.on('error', () => undefined);

  function readClientFrame(): Record<string, unknown> | null {
    if (buffer.length < 2) return null;
    const first = buffer[0]!;
    const second = buffer[1]!;
    let length = second & 0x7f;
    let headerLength = 2;
    if (length === 126) {
      if (buffer.length < 4) return null;
      length = buffer.readUInt16BE(2);
      headerLength = 4;
    }
    const maskOffset = headerLength;
    if ((second & 0x80) === 0 || buffer.length < maskOffset + 4 + length) return null;
    const mask = buffer.subarray(maskOffset, maskOffset + 4);
    const payloadStart = maskOffset + 4;
    const payload = buffer.subarray(payloadStart, payloadStart + length);
    buffer = buffer.subarray(payloadStart + length);
    if ((first & 0x0f) !== 0x1) return null;
    const unmasked = Buffer.from(payload.map((value, index) => value ^ mask[index % 4]!));
    return JSON.parse(unmasked.toString('utf8')) as Record<string, unknown>;
  }
}

function handleRequest(
  message: Record<string, unknown>,
  socket: Socket,
  onRequest: (request: Record<string, unknown>) => void,
  scenario: AppServerScenario,
): void {
  const id = message['id'];
  const method = message['method'];
  if (typeof id !== 'number') return;
  onRequest(message);
  if (method === 'initialize') return send(socket, { id, result: {} });
  if (method === 'thread/loaded/list') return send(socket, { id, result: { data: ['thread-1', 'thread-2'] } });
  if (method === 'thread/read') {
    const threadId = (message['params'] as Record<string, unknown>)['threadId'];
    const thread = threadId === 'thread-1'
      ? { id: threadId, name: 'Main session', status: { type: 'idle' }, canAcceptDirectInput: true }
      : { id: threadId, status: { type: 'active' }, canAcceptDirectInput: false };
    return send(socket, { id, result: { thread } });
  }
  if (method === 'thread/resume') {
    return send(socket, {
      id,
      result: {
        thread: {
          id: 'thread-1',
          turns: [{ id: 'turn-1', status: scenario === 'idle' ? 'completed' : 'inProgress' }],
        },
      },
    });
  }
  if (method === 'turn/steer') {
    send(socket, { id, result: {} });
    if (scenario !== 'active-stream') return;
    const threadId = 'thread-1';
    const turnId = 'turn-1';
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'm0', delta: 'tail' } });
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'c0', delta: 'thinking' } });
    send(socket, {
      method: 'item/completed',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'c0', text: 'thinking', phase: 'commentary' } },
    });
    send(socket, {
      method: 'item/completed',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'm0', text: 'prefix tail', phase: 'final_answer' } },
    });
    return send(socket, { method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
  }
  if (method === 'turn/start') {
    const threadId = 'thread-1';
    const turnId = 'turn-2';
    send(socket, {
      method: 'item/started',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'm1', text: '', phase: 'final_answer' } },
    });
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'm1', delta: 'Hel' } });
    send(socket, { id, result: { turn: { id: turnId, status: 'inProgress' } } });
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId: 'turn-x', itemId: 'x1', delta: 'Other' } });
    send(socket, {
      method: 'item/started',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'c1', text: '', phase: 'commentary' } },
    });
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'c1', delta: 'Checking' } });
    send(socket, {
      method: 'item/completed',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'c1', text: 'Checking', phase: 'commentary' } },
    });
    send(socket, { method: 'item/agentMessage/delta', params: { threadId, turnId, itemId: 'm1', delta: 'lo' } });
    send(socket, {
      method: 'item/completed',
      params: { threadId, turnId, item: { type: 'agentMessage', id: 'm1', text: 'Hello', phase: 'final_answer' } },
    });
    return send(socket, { method: 'turn/completed', params: { threadId, turn: { id: turnId, status: 'completed' } } });
  }
  send(socket, { id, error: { message: `unexpected method: ${String(method)}` } });
}

function send(socket: Socket, message: Record<string, unknown>): void {
  const payload = Buffer.from(JSON.stringify(message), 'utf8');
  if (payload.length > 0xffff) throw new Error('test response unexpectedly large');
  const header = payload.length < 126
    ? Buffer.from([0x81, payload.length])
    : Buffer.from([0x81, 126, payload.length >> 8, payload.length & 0xff]);
  socket.write(Buffer.concat([header, payload]));
}
