import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, mkdirSync, closeSync, openSync, chmodSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER_DIR = resolve(HERE, '..');
const LAUNCHER = join(SERVER_DIR, 'bin', 'inline-discussion');

// The launcher runs `npm install` on first launch if this marker is missing.
// In the test environment node_modules/ is already installed by whoever ran
// `npm test`, so we touch the marker to skip that 3–30s step.
const INSTALL_MARKER = join(SERVER_DIR, 'node_modules', '.inline-discussion-install-complete');
if (!existsSync(INSTALL_MARKER)) {
  mkdirSync(dirname(INSTALL_MARKER), { recursive: true });
  closeSync(openSync(INSTALL_MARKER, 'w'));
}

// A tiny stand-in for dist/server.js used only by the launcher test.
// The real server needs a built bundle and agent SDK wiring; the launcher
// contract we care about is tool-agnostic: boot → print URL → exit on POST
// /api/finish. IND_SERVER_JS lets the launcher point at this file instead.
const STUB_SERVER = `
import { createServer } from 'node:http';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
writeFileSync(join(process.env.IND_SESSION_DIR, 'agent.txt'), process.env.IND_AGENT ?? '');
writeFileSync(join(process.env.IND_SESSION_DIR, 'main-session.txt'), process.env.IND_MAIN_SESSION_ID ?? '');
const server = createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/api/finish') {
    const result = { ok: true, finishedAt: new Date().toISOString() };
    writeFileSync(join(process.env.IND_SESSION_DIR, 'result.json'), JSON.stringify(result));
    res.end('{}');
    setTimeout(() => process.exit(0), 50).unref();
    return;
  }
  if (req.method === 'POST' && req.url === '/api/pause') {
    const result = { ok: true, pausedAt: new Date().toISOString() };
    writeFileSync(join(process.env.IND_SESSION_DIR, 'pause.json'), JSON.stringify(result));
    res.end('{}');
    setTimeout(() => process.exit(0), 50).unref();
    return;
  }
  res.end('ok');
});
server.listen(0, '127.0.0.1', () => {
  const addr = server.address();
  console.log('http://127.0.0.1:' + addr.port + '/');
});
`;

function scratch() {
  const root = mkdtempSync(join(tmpdir(), 'ind-launch-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# T\n\nBody.\n');
  const jsonlPath = join(root, 'main.jsonl');
  writeFileSync(jsonlPath, '{}\n');
  const sessionDir = join(root, 'session');
  const stubPath = join(root, 'stub-server.mjs');
  writeFileSync(stubPath, STUB_SERVER);
  return { root, docPath, jsonlPath, sessionDir, stubPath };
}

function runStart(
  s: ReturnType<typeof scratch>,
  extraArgs: string[] = [],
  extraEnv: NodeJS.ProcessEnv = {},
): { stdout: string; stderr: string; status: number | null } {
  const r = spawnSync(
    'bash',
    [
      LAUNCHER, 'start',
      '--doc', s.docPath,
      '--main-jsonl', s.jsonlPath,
      '--session-dir', s.sessionDir,
      ...extraArgs,
    ],
    { env: { ...process.env, IND_SERVER_JS: s.stubPath, ...extraEnv }, encoding: 'utf8' },
  );
  return { stdout: r.stdout ?? '', stderr: r.stderr ?? '', status: r.status };
}

function writeFakeCli(s: ReturnType<typeof scratch>, name: 'claude' | 'codex', output: string): string {
  const binDir = join(s.root, 'bin');
  mkdirSync(binDir, { recursive: true });
  const cliPath = join(binDir, name);
  writeFileSync(cliPath, `#!/usr/bin/env bash
node -e 'const fs = require("node:fs"); fs.writeFileSync(process.env.AGENT_ARGV_LOG, JSON.stringify(process.argv.slice(1)));' -- "$@"
printf '%s\\n' ${JSON.stringify(output)}
`);
  chmodSync(cliPath, 0o755);
  return binDir;
}

function runQuickWithFakeCli(
  s: ReturnType<typeof scratch>,
  args: string[],
  binDir: string,
): { stdout: string; stderr: string; status: number | null; argv: string[] } {
  const argvLog = join(s.root, 'agent-argv.json');
  const r = spawnSync(
    'bash',
    [LAUNCHER, ...args],
    {
      env: {
        ...process.env,
        PATH: `${binDir}:${process.env.PATH ?? ''}`,
        AGENT_ARGV_LOG: argvLog,
      },
      encoding: 'utf8',
    },
  );
  const argv = existsSync(argvLog) ? JSON.parse(readFileSync(argvLog, 'utf8')) as string[] : [];
  return { stdout: r.stdout ?? '', stderr: r.stderr ?? '', status: r.status, argv };
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function requiredArg(argv: string[], index: number): string {
  const value = argv[index];
  if (typeof value !== 'string') {
    assert.fail(`missing argv[${index}]`);
  }
  return value;
}

test('launch.sh start prints URL, writes url.txt and server.pid, exits 0', async () => {
  const s = scratch();
  const r = runStart(s);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);
  assert.match(r.stdout.trim(), /^http:\/\/127\.0\.0\.1:\d+\/$/, `stdout=${r.stdout}`);

  const urlFile = readFileSync(join(s.sessionDir, 'url.txt'), 'utf8').trim();
  assert.equal(urlFile, r.stdout.trim());

  const pid = parseInt(readFileSync(join(s.sessionDir, 'server.pid'), 'utf8').trim(), 10);
  assert.ok(pid > 0, `bad pid=${pid}`);
  // Server should still be alive right after start.
  assert.doesNotThrow(() => process.kill(pid, 0));

  // Bootstrap the stub just to prove the URL works.
  const res = await fetch(urlFile);
  assert.equal(res.status, 200);

  // Clean up the detached server.
  try { process.kill(pid); } catch { /* ignore */ }
});

test('launch.sh start rejects --agent gpt', () => {
  const s = scratch();
  const r = runStart(s, ['--agent', 'gpt']);
  assert.equal(r.status, 2);
  assert.match(r.stderr, /--agent must be claude or codex/);
});

test('launch.sh start forwards the main-session bridge id', () => {
  const s = scratch();
  const r = runStart(s, ['--main-session-id', 'thread-main']);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);
  assert.equal(readFileSync(join(s.sessionDir, 'main-session.txt'), 'utf8'), 'thread-main');
  const pid = parseInt(readFileSync(join(s.sessionDir, 'server.pid'), 'utf8').trim(), 10);
  try { process.kill(pid); } catch { /* ignore */ }
});

test('launch.sh sessions uses the generic session-control bundle and forwards its socket', () => {
  const s = scratch();
  const controlPath = join(s.root, 'session-control.mjs');
  writeFileSync(controlPath, `
process.stdout.write(JSON.stringify({ socket: process.env.IND_MAIN_SESSION_SOCKET }) + '\\n');
`);
  const r = spawnSync(
    'bash',
    [LAUNCHER, 'sessions', '--socket', '/tmp/codex-control.sock'],
    {
      env: {
        ...process.env,
        IND_SESSION_CONTROL_JS: controlPath,
      },
      encoding: 'utf8',
    },
  );
  assert.equal(r.status, 0, `sessions failed: ${r.stderr}`);
  assert.deepEqual(JSON.parse(r.stdout), { socket: '/tmp/codex-control.sock' });
});

test('launch.sh start --hold keeps the launcher alive until the server exits', async (t) => {
  const s = scratch();
  const startProc = spawn(
    'bash',
    [
      LAUNCHER, 'start',
      '--doc', s.docPath,
      '--main-jsonl', s.jsonlPath,
      '--session-dir', s.sessionDir,
      '--hold',
    ],
    { env: { ...process.env, IND_SERVER_JS: s.stubPath } },
  );
  let stdout = '';
  let stderr = '';
  startProc.stdout!.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
  startProc.stderr!.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });

  const exited = new Promise<number>((resolveExit, rejectExit) => {
    startProc.on('exit', (code) => resolveExit(code ?? -1));
    startProc.on('error', rejectExit);
  });
  const timer = setTimeout(() => { startProc.kill('SIGKILL'); }, 10_000);
  t.after(() => clearTimeout(timer));

  let url = '';
  for (let i = 0; i < 100; i += 1) {
    const match = stdout.match(/http:\/\/127\.0\.0\.1:\d+\//);
    if (match) {
      url = match[0];
      break;
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.match(url, /^http:\/\/127\.0\.0\.1:\d+\/$/, `stdout=${stdout}; stderr=${stderr}`);
  assert.equal(startProc.exitCode, null, `launcher exited early; stderr=${stderr}`);

  const res = await fetch(url);
  assert.equal(res.status, 200);

  const finishRes = await fetch(`${url}api/finish`, { method: 'POST' });
  assert.equal(finishRes.status, 200);

  const code = await exited;
  assert.equal(code, 0, `launcher exited ${code}; stderr=${stderr}`);
});

test('launch.sh wait blocks until server exits, then result.json is on disk', async (t) => {
  const s = scratch();
  const r = runStart(s);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);
  const url = r.stdout.trim();

  // wait runs as a detached child; don't let the test hang if it misbehaves.
  const waitProc = spawn('bash', [LAUNCHER, 'wait', '--session-dir', s.sessionDir]);
  let waitErr = '';
  waitProc.stderr!.on('data', (chunk: Buffer) => { waitErr += chunk.toString(); });

  const exited = new Promise<number>((resolveExit, rejectExit) => {
    waitProc.on('exit', (code) => resolveExit(code ?? -1));
    waitProc.on('error', rejectExit);
  });
  const timer = setTimeout(() => { waitProc.kill('SIGKILL'); }, 10_000);
  t.after(() => clearTimeout(timer));

  // Give wait a beat to start polling before we finish the server.
  await new Promise((r) => setTimeout(r, 100));
  const finishRes = await fetch(`${url}api/finish`, { method: 'POST' });
  assert.equal(finishRes.status, 200);

  const code = await exited;
  assert.equal(code, 0, `wait exited ${code}; stderr=${waitErr}`);

  const result = JSON.parse(readFileSync(join(s.sessionDir, 'result.json'), 'utf8')) as { ok: boolean };
  assert.equal(result.ok, true);

  // server.pid removed after successful wait.
  assert.equal(existsSync(join(s.sessionDir, 'server.pid')), false);
});

test('launch.sh wait returns pause.json when a discussion pauses', async (t) => {
  const s = scratch();
  const r = runStart(s);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);
  const url = r.stdout.trim();
  const waitProc = spawn('bash', [LAUNCHER, 'wait', '--session-dir', s.sessionDir]);
  let stdout = '';
  let stderr = '';
  waitProc.stdout!.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
  waitProc.stderr!.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
  const exited = new Promise<number>((resolveExit, rejectExit) => {
    waitProc.on('exit', (code) => resolveExit(code ?? -1));
    waitProc.on('error', rejectExit);
  });
  const timer = setTimeout(() => { waitProc.kill('SIGKILL'); }, 10_000);
  t.after(() => clearTimeout(timer));

  await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  const paused = await fetch(`${url}api/pause`, { method: 'POST' });
  assert.equal(paused.status, 200);
  assert.equal(await exited, 0, `wait exited early; stderr=${stderr}`);
  assert.equal(stdout.trim(), join(s.sessionDir, 'pause.json'));
  assert.equal(existsSync(join(s.sessionDir, 'server.pid')), false);
});

test('launch.sh wait errors cleanly when no server.pid exists', () => {
  const s = scratch();
  const r = spawnSync(
    'bash',
    [LAUNCHER, 'wait', '--session-dir', s.sessionDir],
    { encoding: 'utf8' },
  );
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /server\.pid/);
});

test('launch.sh wait --idle-exit-seconds returns 124 while server remains alive', () => {
  const s = scratch();
  const r = runStart(s);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);

  const wait = spawnSync(
    'bash',
    [LAUNCHER, 'wait', '--session-dir', s.sessionDir, '--idle-exit-seconds', '1'],
    { encoding: 'utf8' },
  );
  assert.equal(wait.status, 124, `wait stdout=${wait.stdout}; stderr=${wait.stderr}`);
  assert.equal(wait.stdout, '');
  assert.match(wait.stderr, /no signal yet/);

  const pid = parseInt(readFileSync(join(s.sessionDir, 'server.pid'), 'utf8'), 10);
  assert.doesNotThrow(() => process.kill(pid, 0));
  try { process.kill(pid); } catch { /* ignore */ }
});

test('launch.sh with unknown option prints usage and exits non-zero', () => {
  // `bogus` is now a valid quick-mode positional, so use a `--` flag the
  // launcher does not know to verify the typo-catching path.
  const r = spawnSync('bash', [LAUNCHER, '--bogus'], { encoding: 'utf8' });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /Usage:/);
});

test('launch.sh quick mode rejects a missing doc with a clear error', () => {
  const r = spawnSync('bash', [LAUNCHER, '/no/such/file.md'], { encoding: 'utf8' });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /doc missing/);
});

test('launch.sh quick -a defaults to the Codex main-agent shortcut', () => {
  const s = scratch();
  const binDir = writeFakeCli(s, 'codex', '{"type":"item.completed","item":{"type":"agent_message","text":"codex done"}}');
  const r = runQuickWithFakeCli(s, ['-a', s.docPath], binDir);
  assert.equal(r.status, 0, `codex shortcut failed: ${r.stderr}`);
  assert.match(r.stderr, /Invoking Codex agent/);
  assert.match(r.stdout, /codex done/);
  assert.deepEqual(r.argv.slice(0, 2), ['exec', '--json']);
  const prompt = requiredArg(r.argv, 2);
  assert.match(prompt, new RegExp(`/inline-discussion:discuss ${escapeRegExp(realpathSync(s.docPath))}`));
  assert.match(prompt, /never stop just because no signal has arrived yet/);
  assert.match(prompt, /Do not ask any follow-up questions/);
});

test('launch.sh quick -a claude uses the Claude main-agent shortcut', () => {
  const s = scratch();
  const binDir = writeFakeCli(s, 'claude', 'claude done');
  const r = runQuickWithFakeCli(s, ['-a', 'claude', s.docPath], binDir);
  assert.equal(r.status, 0, `claude shortcut failed: ${r.stderr}`);
  assert.match(r.stderr, /Invoking Claude agent/);
  assert.match(r.stdout, /claude done/);
  assert.deepEqual(r.argv.slice(0, 1), ['-p']);
  const prompt = requiredArg(r.argv, 1);
  assert.match(prompt, new RegExp(`/inline-discussion:discuss ${escapeRegExp(realpathSync(s.docPath))}`));
  assert.match(prompt, /never stop just because no signal has arrived yet/);
  assert.match(prompt, /do not invoke any further tools/);
});

test('launch.sh quick --agent=claude uses the Claude main-agent shortcut', () => {
  const s = scratch();
  const binDir = writeFakeCli(s, 'claude', 'claude done');
  const r = runQuickWithFakeCli(s, ['--agent=claude', s.docPath], binDir);
  assert.equal(r.status, 0, `claude shortcut failed: ${r.stderr}`);
  assert.match(r.stderr, /Invoking Claude agent/);
  assert.deepEqual(r.argv.slice(0, 1), ['-p']);
});

test('launch.sh quick rejects invalid --agent values', () => {
  const s = scratch();
  const r = spawnSync('bash', [LAUNCHER, '--agent=gpt', s.docPath], { encoding: 'utf8' });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /--agent must be claude or codex/);
});

test('launch.sh wait returns the apply-1.json path when only that file exists', async (t) => {
  const s = scratch();
  const r = runStart(s);
  assert.equal(r.status, 0, `start failed: ${r.stderr}`);

  const waitProc = spawn('bash', [LAUNCHER, 'wait', '--session-dir', s.sessionDir]);
  let waitStdout = '';
  waitProc.stdout!.on('data', (chunk: Buffer) => { waitStdout += chunk.toString(); });

  const exited = new Promise<number>((resolveExit, rejectExit) => {
    waitProc.on('exit', (code) => resolveExit(code ?? -1));
    waitProc.on('error', rejectExit);
  });
  const timer = setTimeout(() => { waitProc.kill('SIGKILL'); }, 10_000);
  t.after(() => clearTimeout(timer));

  await new Promise((r) => setTimeout(r, 200));
  // Drop an apply-1.json into the session dir to simulate /api/apply.
  writeFileSync(join(s.sessionDir, 'apply-1.json'), JSON.stringify({ mode: 'apply', applyIndex: 1 }));

  const code = await exited;
  assert.equal(code, 0);
  assert.match(waitStdout.trim(), /apply-1\.json$/);

  // Cleanup: kill the still-running server.
  const pid = parseInt(readFileSync(join(s.sessionDir, 'server.pid'), 'utf8'), 10);
  try { process.kill(pid); } catch { /* ignore */ }
});
