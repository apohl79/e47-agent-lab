import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const LAUNCHER = join(resolve(HERE, '..'), 'bin', 'inline-discussion');

test('watch resumes the Codex thread for an Apply signal', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-watch-'));
  const sessionDir = join(root, 'session');
  const docPath = join(root, 'doc.md');
  const binDir = join(root, 'bin');
  const argvLog = join(root, 'argv.json');
  mkdirSync(sessionDir);
  mkdirSync(binDir);
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(join(sessionDir, 'server.pid'), '999999');
  writeFileSync(join(sessionDir, 'apply-1.json'), '{}');
  const fakeCodex = join(binDir, 'codex');
  writeFileSync(fakeCodex, `#!/usr/bin/env bash
node -e 'const fs = require("node:fs"); fs.writeFileSync(process.env.IND_WATCH_ARGV, JSON.stringify(process.argv.slice(1))); fs.writeFileSync(process.env.IND_WATCH_SESSION + "/pause.json", "{}");' -- "$@"
`);
  chmodSync(fakeCodex, 0o755);
  const watcher = spawn('bash', [LAUNCHER, 'watch', '--foreground', '--session-dir', sessionDir, '--codex-thread-id', 'thread-123', '--doc', docPath, '--cwd', root], {
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH ?? ''}`,
      IND_WATCH_ARGV: argvLog,
      IND_WATCH_SESSION: sessionDir,
    },
  });
  const status = await new Promise<number>((resolveExit, rejectExit) => {
    watcher.on('exit', (code) => resolveExit(code ?? -1));
    watcher.on('error', rejectExit);
  });
  assert.equal(status, 0);
  assert.equal(existsSync(argvLog), true);
  const argv = JSON.parse(readFileSync(argvLog, 'utf8')) as string[];
  assert.deepEqual(argv.slice(0, 4), ['exec', 'resume', '--skip-git-repo-check', 'thread-123']);
  assert.match(argv[4] ?? '', new RegExp(`/inline-discussion:apply ${sessionDir}/apply-1\\.json`));
  assert.match(argv[4] ?? '', new RegExp(docPath));
});
