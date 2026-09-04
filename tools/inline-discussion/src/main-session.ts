import { createHash, randomBytes } from 'node:crypto';
import { createConnection, type Socket } from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';

const DEFAULT_TIMEOUT_MS = 10_000;
const MAX_MESSAGE_BYTES = 128 * 1024 * 1024;
const WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

export interface MainSessionBridge {
  send(prompt: string): Promise<void>;
  close?(): Promise<void>;
}

export interface AppServerSessionBridgeOptions {
  threadId: string;
  socketPath?: string;
  timeoutMs?: number;
  harness?: AppServerHarness;
}

export interface AppServerSession {
  id: string;
  name: string | null;
  status: unknown;
  canAcceptDirectInput?: boolean;
}

export interface AppServerSessionListOptions {
  socketPath?: string;
  timeoutMs?: number;
  harness?: AppServerHarness;
}

export type AppServerHarness = 'codex' | 'xedoc';

export function createAppServerSessionBridge(options: AppServerSessionBridgeOptions): MainSessionBridge {
  return new AppServerSessionBridge(options);
}

export async function listAppServerSessions(
  options: AppServerSessionListOptions = {},
): Promise<AppServerSession[]> {
  const client = new AppServerRpcClient(
    options.socketPath ?? appServerSocketPath(options.harness),
    options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  );
  try {
    await client.connect();
    await initializeAppServerClient(client);
    const loaded = await client.request('thread/loaded/list', {});
    const threadIds = loaded['data'];
    if (!Array.isArray(threadIds) || !threadIds.every((threadId) => typeof threadId === 'string')) {
      throw new Error('app-server returned invalid loaded thread ids');
    }

    const sessions: AppServerSession[] = [];
    for (const threadId of threadIds) {
      const response = await client.request('thread/read', { threadId, includeTurns: false });
      const thread = recordField(response, 'thread');
      if (!thread) throw new Error('app-server returned an invalid thread object');
      sessions.push({
        id: typeof thread['id'] === 'string' ? thread['id'] : threadId,
        name: typeof thread['name'] === 'string' ? thread['name'] : null,
        status: thread['status'],
        canAcceptDirectInput: typeof thread['canAcceptDirectInput'] === 'boolean'
          ? thread['canAcceptDirectInput']
          : undefined,
      });
    }
    return sessions;
  } finally {
    await client.close();
  }
}

class AppServerSessionBridge implements MainSessionBridge {
  constructor(private readonly options: AppServerSessionBridgeOptions) {}

  async send(prompt: string): Promise<void> {
    const client = new AppServerRpcClient(
      this.options.socketPath ?? appServerSocketPath(this.options.harness),
      this.options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    );
    try {
      await client.connect();
      await initializeAppServerClient(client);
      await client.request('thread/resume', { threadId: this.options.threadId });
      const read = await client.request('thread/read', {
        threadId: this.options.threadId,
        includeTurns: true,
      });
      const thread = recordField(read, 'thread');
      const activeTurn = arrayField(thread, 'turns')
        .map(asRecord)
        .find((turn) => turn?.['status'] === 'inProgress');
      if (typeof activeTurn?.['id'] === 'string') {
        await client.request('turn/steer', {
          threadId: this.options.threadId,
          input: [{ type: 'text', text: prompt }],
          expectedTurnId: activeTurn['id'],
        });
      } else {
        await client.request('turn/start', {
          threadId: this.options.threadId,
          input: [{ type: 'text', text: prompt }],
        });
      }
    } finally {
      await client.close();
    }
  }
}

async function initializeAppServerClient(client: AppServerRpcClient): Promise<void> {
  await client.request('initialize', {
    clientInfo: {
      name: 'inline_discussion',
      title: 'Inline Discussion Main Session Bridge',
      version: '0.1.0',
    },
    capabilities: { experimentalApi: true },
  });
  client.notify('initialized', {});
}

class AppServerRpcClient {
  private socket: Socket | null = null;
  private buffer = Buffer.alloc(0);
  private chunks: Buffer[] = [];
  private waiters: Array<(chunk: Buffer) => void> = [];
  private failure: Error | null = null;
  private nextRequestId = 1;

  constructor(private readonly socketPath: string, private readonly timeoutMs: number) {}

  async connect(): Promise<void> {
    const socket = createConnection(this.socketPath);
    this.socket = socket;
    socket.setTimeout(this.timeoutMs, () => this.fail(new Error('app-server socket timed out')));
    socket.on('data', (chunk: Buffer) => this.pushChunk(chunk));
    socket.on('error', (error) => this.fail(error instanceof Error ? error : new Error(String(error))));
    socket.on('end', () => this.fail(new Error('app-server socket closed')));
    await new Promise<void>((resolve, reject) => {
      socket.once('connect', resolve);
      socket.once('error', reject);
    });

    const key = randomBytes(16).toString('base64');
    this.writeRaw(
      `GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
    );
    const response = (await this.readUntil(Buffer.from('\r\n\r\n'))).toString('latin1');
    const accept = createHash('sha1').update(`${key}${WEBSOCKET_GUID}`).digest('base64');
    if (!response.startsWith('HTTP/1.1 101') || !response.toLowerCase().includes(`sec-websocket-accept: ${accept.toLowerCase()}`)) {
      throw new Error('app-server rejected the websocket handshake');
    }
  }

  async request(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const id = this.nextRequestId++;
    this.sendJson({ method, id, params });
    while (true) {
      const message = asRecord(JSON.parse((await this.readMessage()).toString('utf8')));
      if (typeof message?.['method'] === 'string') continue;
      if (message?.['id'] !== id) continue;
      const error = asRecord(message['error']);
      if (error) throw new Error(String(error['message'] ?? 'app-server request failed'));
      return asRecord(message['result']) ?? {};
    }
  }

  notify(method: string, params: Record<string, unknown>): void {
    this.sendJson({ method, params });
  }

  async close(): Promise<void> {
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    socket.destroy();
  }

  private sendJson(value: Record<string, unknown>): void {
    this.sendFrame(0x1, Buffer.from(JSON.stringify(value), 'utf8'));
  }

  private sendFrame(opcode: number, payload: Buffer): void {
    if (payload.length > MAX_MESSAGE_BYTES) throw new Error('app-server message is too large');
    const mask = randomBytes(4);
    const masked = Buffer.from(payload.map((value, index) => value ^ mask[index % 4]!));
    const length = payload.length;
    const header = length < 126
      ? Buffer.from([0x80 | opcode, 0x80 | length])
      : length <= 0xffff
        ? Buffer.concat([Buffer.from([0x80 | opcode, 0x80 | 126]), Buffer.alloc(2)])
        : Buffer.concat([Buffer.from([0x80 | opcode, 0x80 | 127]), Buffer.alloc(8)]);
    if (length > 125 && length <= 0xffff) header.writeUInt16BE(length, 2);
    if (length > 0xffff) header.writeBigUInt64BE(BigInt(length), 2);
    this.writeRaw(Buffer.concat([header, mask, masked]));
  }

  private async readMessage(): Promise<Buffer> {
    const fragments: Buffer[] = [];
    let opcode: number | null = null;
    while (true) {
      const header = await this.readExact(2);
      const first = header[0]!;
      const second = header[1]!;
      const frameOpcode = first & 0x0f;
      let length = second & 0x7f;
      if (length === 126) length = (await this.readExact(2)).readUInt16BE(0);
      if (length === 127) {
        const largeLength = (await this.readExact(8)).readBigUInt64BE(0);
        if (largeLength > BigInt(MAX_MESSAGE_BYTES)) throw new Error('app-server message is too large');
        length = Number(largeLength);
      }
      const mask = (second & 0x80) === 0 ? null : await this.readExact(4);
      let payload = await this.readExact(length);
      if (mask) payload = Buffer.from(payload.map((value, index) => value ^ mask[index % 4]!));
      if (frameOpcode === 0x9) {
        this.sendFrame(0xa, payload);
        continue;
      }
      if (frameOpcode === 0x8) throw new Error('app-server closed the websocket');
      if (frameOpcode === 0x1) opcode = frameOpcode;
      if (frameOpcode !== 0x0 && frameOpcode !== 0x1) throw new Error('unsupported app-server websocket frame');
      if (opcode === null) throw new Error('invalid app-server websocket continuation');
      fragments.push(payload);
      const total = fragments.reduce((sum, fragment) => sum + fragment.length, 0);
      if (total > MAX_MESSAGE_BYTES) throw new Error('app-server message is too large');
      if (first & 0x80) return Buffer.concat(fragments);
    }
  }

  private async readUntil(marker: Buffer): Promise<Buffer> {
    while (true) {
      const index = this.buffer.indexOf(marker);
      if (index !== -1) {
        const end = index + marker.length;
        const result = this.buffer.subarray(0, end);
        this.buffer = this.buffer.subarray(end);
        return result;
      }
      this.buffer = Buffer.concat([this.buffer, await this.readChunk()]);
      if (this.buffer.length > 64 * 1024) throw new Error('app-server websocket handshake is too large');
    }
  }

  private async readExact(length: number): Promise<Buffer> {
    while (this.buffer.length < length) this.buffer = Buffer.concat([this.buffer, await this.readChunk()]);
    const result = this.buffer.subarray(0, length);
    this.buffer = this.buffer.subarray(length);
    return result;
  }

  private readChunk(): Promise<Buffer> {
    if (this.failure) return Promise.reject(this.failure);
    const chunk = this.chunks.shift();
    if (chunk) return Promise.resolve(chunk);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  private pushChunk(chunk: Buffer): void {
    const waiter = this.waiters.shift();
    if (waiter) waiter(chunk);
    else this.chunks.push(chunk);
  }

  private fail(error: Error): void {
    this.failure ??= error;
    const waiters = this.waiters.splice(0);
    for (const waiter of waiters) waiter(Buffer.alloc(0));
  }

  private writeRaw(value: string | Buffer): void {
    if (!this.socket) throw new Error('app-server socket is not connected');
    this.socket.write(value);
  }
}

export function appServerSocketPath(
  harness: AppServerHarness = 'codex',
  env: NodeJS.ProcessEnv = process.env,
  homeDirectory = homedir(),
): string {
  const prefix = harness.toUpperCase();
  const harnessHome = env[`${prefix}_HOME`] || join(homeDirectory, `.${harness}`);
  return env[`${prefix}_APP_SERVER_SOCKET`] || join(harnessHome, 'app-server-control', 'app-server-control.sock');
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function recordField(value: Record<string, unknown> | null, key: string): Record<string, unknown> | null {
  return asRecord(value?.[key]);
}

function arrayField(value: Record<string, unknown> | null, key: string): unknown[] {
  const field = value?.[key];
  return Array.isArray(field) ? field : [];
}
