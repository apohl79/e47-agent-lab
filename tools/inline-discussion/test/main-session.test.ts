import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createServer, type Socket } from 'node:net';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { appServerSocketPath, listAppServerSessions } from '../src/main-session.ts';

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

function serveAppServer(socket: Socket): void {
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
      handleRequest(message, socket);
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

function handleRequest(message: Record<string, unknown>, socket: Socket): void {
  const id = message['id'];
  const method = message['method'];
  if (typeof id !== 'number') return;
  if (method === 'initialize') return send(socket, { id, result: {} });
  if (method === 'thread/loaded/list') return send(socket, { id, result: { data: ['thread-1', 'thread-2'] } });
  if (method === 'thread/read') {
    const threadId = (message['params'] as Record<string, unknown>)['threadId'];
    const thread = threadId === 'thread-1'
      ? { id: threadId, name: 'Main session', status: { type: 'idle' }, canAcceptDirectInput: true }
      : { id: threadId, status: { type: 'active' }, canAcceptDirectInput: false };
    return send(socket, { id, result: { thread } });
  }
  send(socket, { id, error: { message: `unexpected method: ${String(method)}` } });
}

function send(socket: Socket, message: Record<string, unknown>): void {
  const payload = Buffer.from(JSON.stringify(message), 'utf8');
  if (payload.length >= 126) throw new Error('test response unexpectedly large');
  socket.write(Buffer.concat([Buffer.from([0x81, payload.length]), payload]));
}
