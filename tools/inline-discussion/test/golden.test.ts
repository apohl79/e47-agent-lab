// test/golden.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';

test('golden: two threads, one closed by user, finish auto-closes the other', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-golden-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# T\n\nFirst para.\n\nSecond para.\n');
  const jsonl = join(root, 'session.jsonl');
  writeFileSync(jsonl, JSON.stringify({ type: 'user', text: 'go' }));
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');

  const { port, close } = await createServer({
    docPath, mainJsonlPath: jsonl, sessionDir, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'answer', conclusion: 'auto conclusion' }),
  });

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const paraIds = boot.blockIds.slice(1); // skip heading

  const t1 = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: paraIds[0] }, message: 'q1' }),
  })).json()) as { threadId: string };
  const t2 = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: paraIds[1] }, message: 'q2' }),
  })).json()) as { threadId: string };

  await new Promise((r) => setTimeout(r, 50));
  await fetch(`http://127.0.0.1:${port}/api/threads/${t1.threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'User conclusion.' }),
  });

  const finish = (await (await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' })).json()) as {
    conclusions: Array<{ threadId: string; conclusion: string; closedBy: string }>;
    archivedThreadCount: number;
  };
  assert.equal(finish.conclusions.length, 2);
  const byUser = finish.conclusions.find((c) => c.threadId === t1.threadId)!;
  const byAuto = finish.conclusions.find((c) => c.threadId === t2.threadId)!;
  assert.equal(byUser.conclusion, 'User conclusion.');
  assert.equal(byUser.closedBy, 'user');
  assert.equal(byAuto.conclusion, 'auto conclusion');
  assert.equal(byAuto.closedBy, 'auto');

  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /User conclusion\./);
  assert.match(after, /auto conclusion/);

  await close();
});
