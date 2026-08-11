import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { mockAgentFactory } from '../src/agent.ts';
import { createServer } from '../src/server.ts';

test('bootstrap reports the global count of notes and threads that Apply will process', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-apply-count-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'subdoc.md');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(docPath, '# Main\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# Subdocument\n\nSubdocument paragraph.\n');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const server = await createServer({
    docPath,
    projectRoot: root,
    sessionDir: join(root, 'session'),
    mainJsonlPath: transcriptPath,
    prefsPath: join(root, 'prefs.json'),
    agentFactory: mockAgentFactory({ reply: 'reply', conclusion: 'conclusion' }),
    shutdownOnFinish: false,
  });

  const mainBootstrap = await bootstrap(server.port);
  const subdocumentBootstrap = await bootstrap(server.port, '/subdoc.md');
  await createNote(server.port, docPath, mainBootstrap.blockIds[1] ?? '');
  await createNote(server.port, subdocPath, subdocumentBootstrap.blockIds[1] ?? '');

  const result = await bootstrap(server.port);
  assert.deepEqual(
    { visibleThreads: result.threads.length, applyAvailable: result.applyAvailable, applyCount: result.applyCount },
    { visibleThreads: 1, applyAvailable: true, applyCount: 2 },
  );
  await server.close();
});

async function bootstrap(port: number, path = ''): Promise<{
  blockIds: string[];
  threads: unknown[];
  applyAvailable: boolean;
  applyCount: number;
}> {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const response = await fetch(`http://127.0.0.1:${port}/api/bootstrap${query}`);
  assert.equal(response.status, 200);
  return response.json() as Promise<{
    blockIds: string[];
    threads: unknown[];
    applyAvailable: boolean;
    applyCount: number;
  }>;
}

async function createNote(port: number, documentPath: string, blockId: string): Promise<void> {
  const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ documentPath, anchor: { blockId }, message: 'note', kind: 'note' }),
  });
  assert.equal(response.status, 200);
}
