import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const runtime = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outputs = ['server.js', 'hosted-panel.js', 'session-control.js', 'task-control.js', 'index.html', 'app.js', 'app.css', 'favicon.svg'];

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'ind-prepare-'));
  for (const directory of ['bin', 'src', 'fake-bin']) mkdirSync(join(root, directory));
  for (const name of ['prepare-runtime.mjs', 'inline-discussion', 'xedoc-panel']) {
    copyFileSync(join(runtime, 'bin', name), join(root, 'bin', name));
  }
  for (const name of ['package.json', 'package-lock.json', 'tsconfig.json', 'tsconfig.web.json']) {
    writeFileSync(join(root, name), '{}');
  }
  writeFileSync(join(root, 'esbuild.config.mjs'), '');
  writeFileSync(join(root, 'src/server.ts'), 'initial');
  const npm = join(root, 'fake-bin/npm');
  writeFileSync(npm, `#!${process.execPath}
const fs = require('node:fs');
fs.appendFileSync('npm-calls', process.argv[2] + '\\n');
if (process.env.FAIL_PREPARATION) process.exit(1);
if (process.argv[2] === 'ci') {
  fs.rmSync('node_modules', { recursive: true, force: true });
  fs.mkdirSync('node_modules');
} else {
  fs.mkdirSync('dist', { recursive: true });
  for (const name of ${JSON.stringify(outputs)}) {
    fs.writeFileSync('dist/' + name, "console.log('prepared runtime');");
  }
}
`);
  chmodSync(npm, 0o755);
  const env = { ...process.env, PATH: `${join(root, 'fake-bin')}:${process.env.PATH}` };
  const run = (command: string, args: string[], extraEnv = {}) => spawnSync(command, args, {
    cwd: root, env: { ...env, ...extraEnv }, encoding: 'utf8',
  });
  const prepare = (extraEnv = {}) => run(process.execPath, [join(root, 'bin/prepare-runtime.mjs')], extraEnv);
  const calls = () => readFileSync(join(root, 'npm-calls'), 'utf8').trim().split('\n');
  return { root, run, prepare, calls };
}

test('panel and standalone reuse the same pinned dependencies and renderer', () => {
  const f = fixture();
  try {
    assert.equal(f.run('python3', ['bin/xedoc-panel', '--prepare']).status, 0);
    const standalone = f.run('bash', ['bin/inline-discussion', 'tasks']);
    assert.equal(standalone.status, 0, standalone.stderr);
    assert.match(standalone.stdout, /prepared runtime/);
    const panel = f.run('python3', ['bin/xedoc-panel']);
    assert.equal(panel.status, 0, panel.stderr);
    assert.match(panel.stdout, /prepared runtime/);
    assert.deepEqual(f.calls(), ['ci', 'run']);
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});

test('source content, build configuration, missing bundles, and lock changes invalidate preparation', () => {
  const f = fixture();
  try {
    assert.equal(f.prepare().status, 0);
    utimesSync(join(f.root, 'src/server.ts'), new Date(), new Date());
    assert.equal(f.prepare().status, 0);
    assert.deepEqual(f.calls(), ['ci', 'run']);
    writeFileSync(join(f.root, 'src/server.ts'), 'changed');
    utimesSync(join(f.root, 'src/server.ts'), 1, 1);
    assert.equal(f.prepare().status, 0);
    writeFileSync(join(f.root, 'esbuild.config.mjs'), '// changed');
    assert.equal(f.prepare().status, 0);
    rmSync(join(f.root, 'dist/hosted-panel.js'));
    assert.equal(f.prepare().status, 0);
    writeFileSync(join(f.root, 'package-lock.json'), '{"changed":true}');
    assert.equal(f.prepare().status, 0);
    assert.deepEqual(f.calls(), ['ci', 'run', 'run', 'run', 'run', 'ci', 'run']);
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});

test('failed dependency preparation is retried rather than marked ready', () => {
  const f = fixture();
  try {
    assert.equal(f.prepare({ FAIL_PREPARATION: '1' }).status, 5);
    assert.equal(existsSync(join(f.root, 'node_modules/.inline-discussion-dependencies')), false);
    assert.equal(f.prepare().status, 0);
    assert.deepEqual(f.calls(), ['ci', 'ci', 'run']);
  } finally {
    rmSync(f.root, { recursive: true, force: true });
  }
});
