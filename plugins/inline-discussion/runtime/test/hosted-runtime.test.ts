import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const runtime = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const protocol = 'xedoc.plugin-panel-runtime/v1';

test('warm hosted runtime isolates document servers and survives instance close, pause, and errors', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-warm-'));
  const child = spawn('python3', [join(runtime, 'bin/xedoc-panel')], {
    env: { ...process.env, XEDOC_PLUGIN_PANEL_REUSE: 'session' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let diagnostics = '';
  child.stderr.on('data', (data) => { diagnostics = (diagnostics + String(data)).slice(-65536); });
  const output = createInterface({ input: child.stdout });
  const read = async () => {
    const [line] = await once(output, 'line', { signal: AbortSignal.timeout(25_000) });
    return JSON.parse(line as string);
  };
  const command = async (body: Record<string, unknown>) => {
    const reply = read();
    child.stdin.write(`${JSON.stringify({ protocol, ...body })}\n`);
    return await reply;
  };
  const context = (name: string, effort = 'medium') => {
    const doc = join(root, `${name}.md`);
    writeFileSync(doc, `# ${name}\n\n${name} paragraph.\n`);
    return {
      protocol: 'xedoc.plugin-panel/v1', pluginId: 'inline-discussion@fixture',
      panelId: 'discussion', threadId: 'fixture', projectRoot: root,
      target: doc, home: join(root, 'home'), sessionDir: join(root, `session-${name}`),
      sessionSocket: join(root, 'absent.sock'),
      inference: {
        threadId: 'fixture', model: 'fixture', modelProvider: 'fixture', reasoningEffort: effort,
        models: [{
          providerId: 'fixture', model: 'fixture', defaultReasoningEffort: 'medium',
          supportedReasoningEfforts: [
            { reasoningEffort: 'medium', description: 'medium' },
            { reasoningEffort: 'high', description: 'high' },
          ],
        }],
      },
    };
  };
  const bootstrap = async (url: string) => {
    const response = await fetch(`${url}api/bootstrap`, { signal: AbortSignal.timeout(5_000) });
    assert.equal(response.status, 200);
    return await response.json() as {
      documentPath: string; html: string; blockIds: string[]; threads: unknown[];
      defaultInferenceSettings: { reasoningEffort: string };
    };
  };
  try {
    assert.deepEqual(await read(), { protocol, ready: true }, diagnostics);
    const firstContext = context('first');
    const secondContext = context('second', 'high');
    let first = await command({ op: 'open', instanceId: 'first', context: firstContext });
    const second = await command({ op: 'open', instanceId: 'second', context: secondContext });
    assert.notEqual(first.url, second.url);
    const firstBoot = await bootstrap(first.url);
    const secondBoot = await bootstrap(second.url);
    assert.equal(firstBoot.documentPath, firstContext.target);
    assert.equal(secondBoot.documentPath, secondContext.target);
    assert.equal(firstBoot.defaultInferenceSettings.reasoningEffort, 'medium');
    assert.equal(secondBoot.defaultInferenceSettings.reasoningEffort, 'high');
    const note = await fetch(`${first.url}api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: firstBoot.blockIds[1] }, message: 'First only', kind: 'note' }),
    });
    assert.equal(note.status, 200);
    assert.equal((await bootstrap(first.url)).threads.length, 1);
    assert.equal((await bootstrap(second.url)).threads.length, 0);

    assert.deepEqual(await command({ op: 'close', instanceId: 'first' }), { protocol, instanceId: 'first', ok: true });
    await assert.rejects(fetch(`${first.url}api/bootstrap`));
    assert.equal((await bootstrap(second.url)).documentPath, secondContext.target);
    first = await command({ op: 'open', instanceId: 'first', context: firstContext });
    assert.equal((await bootstrap(first.url)).threads.length, 1);

    const paused = await fetch(`${first.url}api/pause`, { method: 'POST' });
    assert.equal(paused.status, 200);
    for (let attempt = 0; ; attempt++) {
      const status = await command({ op: 'status' });
      if (!status.instances.includes('first')) break;
      assert.ok(attempt < 50, 'paused server was not removed');
      await new Promise((accept) => setTimeout(accept, 10));
    }
    assert.equal((await bootstrap(second.url)).documentPath, secondContext.target);
    first = await command({ op: 'open', instanceId: 'first', context: firstContext });
    assert.equal((await bootstrap(first.url)).threads.length, 1);
    const wrongSession = await command({
      op: 'open', instanceId: 'wrong',
      context: { ...secondContext, threadId: 'another' },
    });
    assert.match(wrongSession.error, /different session/);
    const missing = await command({
      op: 'open', instanceId: 'missing',
      context: { ...secondContext, target: join(root, 'missing.md') },
    });
    assert.match(missing.error, /ENOENT/);
    assert.equal((await bootstrap(second.url)).threads.length, 0);
    assert.deepEqual((await command({ op: 'status' })).instances.sort(), ['first', 'second']);

    const exited = once(child, 'exit', { signal: AbortSignal.timeout(5_000) });
    child.stdin.end();
    assert.deepEqual(await exited, [0, null], diagnostics);
    await assert.rejects(fetch(`${second.url}api/bootstrap`));
  } finally {
    output.close();
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit', { signal: AbortSignal.timeout(5_000) });
      child.kill('SIGTERM');
      await exited;
    }
    rmSync(root, { recursive: true, force: true });
  }
});
