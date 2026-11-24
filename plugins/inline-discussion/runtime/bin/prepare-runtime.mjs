#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const runtime = dirname(dirname(fileURLToPath(import.meta.url)));
const dependenciesMarker = join(runtime, 'node_modules/.inline-discussion-dependencies');
const buildMarker = join(runtime, 'dist/.inline-discussion-build');
const outputs = [
  'server.js', 'hosted-panel.js', 'session-control.js', 'task-control.js',
  'index.html', 'app.js', 'app.css', 'favicon.svg',
];

function contents(path) {
  return existsSync(path) ? readFileSync(path, 'utf8') : '';
}

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name))
    .flatMap((entry) => {
      const path = join(directory, entry.name);
      return entry.isDirectory() ? sourceFiles(path) : entry.isFile() ? [path] : [];
    });
}

function run(args) {
  const result = spawnSync('npm', args, {
    cwd: runtime,
    stdio: ['ignore', 2, 2],
    shell: process.platform === 'win32',
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`npm ${args.join(' ')} failed`);
}

try {
  const dependencyHash = createHash('sha256')
    .update(readFileSync(join(runtime, 'package.json')))
    .update(readFileSync(join(runtime, 'package-lock.json')))
    .digest('hex');
  const installedHash = contents(dependenciesMarker)
    || contents(join(runtime, 'node_modules/.xedoc-panel-lock'));
  if (installedHash !== dependencyHash) {
    console.error('Inline Discussion: preparing pinned plugin dependencies…');
    run(['ci', '--no-audit', '--no-fund', '--include=dev', '--ignore-scripts']);
  }
  if (contents(dependenciesMarker) !== dependencyHash) {
    writeFileSync(dependenciesMarker, dependencyHash);
  }

  const buildHash = createHash('sha256').update(dependencyHash);
  for (const path of [
    'esbuild.config.mjs', 'tsconfig.json', 'tsconfig.web.json',
    ...sourceFiles(join(runtime, 'src')).map((path) => relative(runtime, path)),
  ]) {
    buildHash.update(path).update('\0').update(readFileSync(join(runtime, path))).update('\0');
  }
  const fingerprint = buildHash.digest('hex');
  if (contents(buildMarker) !== fingerprint
    || outputs.some((name) => !existsSync(join(runtime, 'dist', name)))) {
    console.error('Inline Discussion: preparing renderer…');
    run(['run', '--silent', 'build']);
    mkdirSync(dirname(buildMarker), { recursive: true });
    writeFileSync(buildMarker, fingerprint);
  }
} catch (error) {
  console.error(`Inline Discussion preparation failed: ${error.message}`);
  process.exitCode = 5;
}

if (!process.exitCode && process.argv[2] === '--hosted-panel') {
  await import('../dist/hosted-panel.js');
}
