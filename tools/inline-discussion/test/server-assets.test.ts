import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';

test('serves media stored beside a converted discussion document', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-assets-'));
  mkdirSync(join(root, '.git'));
  const docDir = join(root, 'docs', 'discussions');
  mkdirSync(join(docDir, 'media'), { recursive: true });
  const png = Buffer.from('89504e470d0a1a0a', 'hex');
  writeFileSync(join(docDir, 'media', 'chart.png'), png);
  const docPath = join(docDir, 'note.md');
  writeFileSync(docPath, '# Note\n\n![](media/chart.png)\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));

  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/media/chart.png`);

  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'image/png');
  assert.deepEqual(Buffer.from(await res.arrayBuffer()), png);
  await close();
});

test('serves relative media for the selected document path', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-selected-assets-'));
  mkdirSync(join(root, '.git'));
  const mainPath = join(root, 'main.md');
  const docDir = join(root, 'docs', 'discussions');
  const diagramsDir = join(docDir, 'diagrams');
  mkdirSync(diagramsDir, { recursive: true });
  const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>');
  writeFileSync(join(diagramsDir, 'diagram.svg'), svg);
  const documentPath = join(docDir, 'decision.md');
  writeFileSync(documentPath, '# Decision\n\n![Diagram](./diagrams/diagram.svg)\n');
  writeFileSync(mainPath, '# Main\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));

  const { port, close } = await createServer({
    docPath: mainPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const bootstrap = await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent(documentPath)}`);
  assert.equal(bootstrap.status, 200);
  const body = await bootstrap.json() as { html: string };
  const imageMatch = body.html.match(/<img src="([^"]+)"/);
  assert.ok(imageMatch);
  const imageUrl = imageMatch[1]!.replaceAll('&amp;', '&');
  assert.match(imageUrl, /^\/api\/assets\?/);
  const res = await fetch(`http://127.0.0.1:${port}${imageUrl}`);

  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'image/svg+xml');
  assert.deepEqual(Buffer.from(await res.arrayBuffer()), svg);
  await close();
});
