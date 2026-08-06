import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';

type ThreadCreatedEvent = {
  thread: {
    messages: Array<{ role: string; text: string }>;
  };
};

function scratchSession(doc: string) {
  const root = mkdtempSync(join(tmpdir(), 'ind-thread-creation-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, doc);
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, '{"type":"user","text":"hi"}');
  return {
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    transcriptPath,
  };
}

async function connectThreadCreated(port: number): Promise<{
  event: Promise<ThreadCreatedEvent>;
  close: () => void;
}> {
  const controller = new AbortController();
  const response = await fetch(`http://127.0.0.1:${port}/events`, { signal: controller.signal });
  const reader = response.body?.getReader();
  if (!reader) throw new Error('SSE response has no body');
  let resolveEvent: (event: ThreadCreatedEvent) => void = () => undefined;
  let rejectEvent: (error: Error) => void = () => undefined;
  const event = new Promise<ThreadCreatedEvent>((resolve, reject) => {
    resolveEvent = resolve;
    rejectEvent = reject;
  });
  const decoder = new TextDecoder();
  let buffer = '';
  void (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value);
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const eventName = frame.match(/^event: (.+)$/m)?.[1];
          if (eventName === 'thread.created') {
            const data = frame.match(/^data: (.+)$/m)?.[1];
            if (data) resolveEvent(JSON.parse(data) as ThreadCreatedEvent);
            return;
          }
          boundary = buffer.indexOf('\n\n');
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) rejectEvent(error instanceof Error ? error : new Error(String(error)));
    }
  })();
  return { event, close: () => controller.abort() };
}

test('thread.created includes the initial user message for a new assistant thread', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const events = await connectThreadCreated(port);
  try {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'why?' }),
    });
    assert.equal(response.status, 200);
    const created = await events.event;
    assert.equal(created.thread.messages[0]?.role, 'user');
    assert.equal(created.thread.messages[0]?.text, 'why?');
  } finally {
    events.close();
    await close();
  }
});
