// test/server.test.ts
import { mock, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer, computeDocTitle, resolveAgentMode } from '../src/server.ts';
import { parseDoc } from '../src/markdown.ts';
import { mockAgentFactory, type AgentFactory, type ThreadAgent } from '../src/agent.ts';
import type { MainSessionBridge } from '../src/main-session.ts';

function titleOf(md: string, docPath: string): string {
  return computeDocTitle(parseDoc(md).blocks, docPath);
}

function scratchSession(doc: string) {
  const root = mkdtempSync(join(tmpdir(), 'ind-'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, doc);
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  return { docPath, sessionDir, transcriptPath, prefsPath };
}

test('GET /api/bootstrap returns html + block ids + empty thread list', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# Title\n\nPara.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/api/bootstrap`);
  const json = (await res.json()) as {
    html: string;
    blockIds: string[];
    threads: unknown[];
    archivedThreads: unknown[];
    readOnly: boolean;
  };
  assert.equal(res.status, 200);
  assert.match(json.html, /<h1[^>]*data-block-id="[0-9a-f]{10}"/);
  assert.equal(json.blockIds.length, 2);
  assert.equal(json.threads.length, 0);
  assert.equal(json.archivedThreads.length, 0);
  assert.equal(json.readOnly, false);
  await close();
});

test('rejects cross-origin mutations and non-JSON mutation bodies', async () => {
  const { docPath, sessionDir, prefsPath } = scratchSession('# Title\n\nPara.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const crossOrigin = await fetch(`http://127.0.0.1:${port}/api/prefs`, {
    method: 'POST',
    headers: { origin: 'https://attacker.invalid', 'content-type': 'application/json' },
    body: JSON.stringify({ theme: 'dark' }),
  });
  assert.equal(crossOrigin.status, 403);
  const wrongContentType = await fetch(`http://127.0.0.1:${port}/api/prefs`, {
    method: 'POST',
    body: JSON.stringify({ theme: 'dark' }),
  });
  assert.equal(wrongContentType.status, 415);
  assert.equal(wrongContentType.headers.get('x-content-type-options'), 'nosniff');
  await close();
});

test('does not expose files outside the project root or active repo content', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-file-boundary-'));
  const outside = join(tmpdir(), `ind-outside-${Date.now()}.ts`);
  writeFileSync(outside, 'export const secret = true;\n');
  writeFileSync(join(root, 'doc.md'), '# Discussion\n');
  writeFileSync(join(root, 'script.js'), 'alert(1);\n');
  const { port, close } = await createServer({
    docPath: join(root, 'doc.md'), projectRoot: root, sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'), agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const outsideBoot = await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent(`${outside}:1`)}`);
  assert.equal(outsideBoot.status, 200);
  assert.equal((await outsideBoot.json() as { sourceView: boolean }).sourceView, false);
  assert.equal((await fetch(`http://127.0.0.1:${port}/script.js`)).status, 404);
  await close();
});

test('POST /api/pause persists live threads and highlights for a later server restart', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# Title\n\nPara.\n');
  const options = {
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  };
  const first = await createServer(options);
  const boot = (await (await fetch(`http://127.0.0.1:${first.port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;

  await fetch(`http://127.0.0.1:${first.port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'Remember this', kind: 'note' }),
  });
  await fetch(`http://127.0.0.1:${first.port}/api/highlights`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'Para.' } }),
  });

  const paused = await fetch(`http://127.0.0.1:${first.port}/api/pause`, { method: 'POST' });
  assert.equal(paused.status, 200);
  assert.equal(existsSync(join(sessionDir, 'pause.json')), true);
  assert.equal(existsSync(join(sessionDir, 'live-session.json')), true);
  await first.close();

  const resumed = await createServer(options);
  const restored = (await (await fetch(`http://127.0.0.1:${resumed.port}/api/bootstrap`)).json()) as {
    threads: Array<{ kind: string; messages: Array<{ text: string }> }>;
    highlights: Array<{ anchor: { quote?: string } }>;
  };
  assert.equal(restored.threads.length, 1);
  assert.equal(restored.threads[0]!.kind, 'note');
  assert.equal(restored.threads[0]!.messages[0]!.text, 'Remember this');
  assert.equal(restored.highlights.length, 1);
  assert.equal(restored.highlights[0]!.anchor.quote, 'Para.');
  await resumed.close();
});

test('computeDocTitle uses the filename (with extension), ignoring doc content', () => {
  // Filename with extension is returned verbatim.
  assert.equal(titleOf('# My Title\n\nBody.\n', '/tmp/anything.md'), 'anything.md');
  assert.equal(titleOf('Just paragraph.\n', '/tmp/some-slug.markdown'), 'some-slug.markdown');
  // Headings in content are ignored — title stays stable across edits.
  assert.equal(titleOf('## Subheading first\n\n# H1 later\n', '/tmp/2026-04-21-foo.md'), '2026-04-21-foo.md');
  assert.equal(titleOf('', '/a/b/c/my-doc.md'), 'my-doc.md');
});

test('resolveAgentMode maps only codex to the Codex-backed agent', () => {
  assert.equal(resolveAgentMode('codex'), 'codex');
  assert.equal(resolveAgentMode('claude'), 'claude');
  assert.equal(resolveAgentMode('gpt'), 'claude');
  assert.equal(resolveAgentMode(undefined), 'claude');
});

test('POST /api/threads/:id/interrupt interrupts the active agent turn', async () => {
  const { docPath, sessionDir, prefsPath } = scratchSession('# Title\n\nPara.\n');
  let release: (() => void) | null = null;
  let interrupted = false;
  const agentFactory: AgentFactory = () => ({
    async *send() {
      await new Promise<void>((resolve) => { release = resolve; });
      yield { type: 'interrupted' };
    },
    async *proposeConclusion() { yield { type: 'done', text: '' }; },
    snapshot: () => [],
    interrupt: async () => { interrupted = true; release?.(); },
  });
  const { port, close } = await createServer({
    docPath, sessionDir, prefsPath, agentFactory, shutdownOnFinish: false,
  });
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as { blockIds: string[] };
  const created = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'start', kind: 'thread' }),
  });
  const { threadId } = await created.json() as { threadId: string };
  const response = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/interrupt`, { method: 'POST' });
  assert.equal(response.status, 202);
  assert.equal(interrupted, true);
  await close();
});

test('GET absolute source path with a line number renders highlighted source and targets that line', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-source-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const sourcePath = join(root, 'src', 'service.ts');
  mkdirSync(join(root, 'src'));
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(sourcePath, 'export const first = 1;\nexport const target = 2;\n');
  const staticDir = join(root, 'static');
  mkdirSync(staticDir);
  writeFileSync(join(staticDir, 'index.html'), '<script src="/app.js"></script>');
  const { port, close } = await createServer({
    docPath, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'),
    staticDir, agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const page = await fetch(`http://127.0.0.1:${port}${sourcePath}:2`);
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent(`${sourcePath}:2`)}`)).json() as {
    html: string; title: string; targetLine: number; readOnly: boolean;
  };
  assert.equal(page.status, 200);
  assert.match(await page.text(), /app\.js/);
  assert.equal(boot.title, 'service.ts');
  assert.equal(boot.targetLine, 2);
  assert.equal(boot.readOnly, true);
  assert.equal((boot as { sourceView?: boolean }).sourceView, true);
  assert.match(boot.html, /data-block-id="line-2"/);
  assert.match(boot.html, /language-typescript/);
  await close();
});

test('GET project-root-relative source path renders the selected file', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-source-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const sourcePath = join(root, 'src', 'config.json');
  mkdirSync(join(root, 'src'));
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(sourcePath, '{"enabled": true}\n');
  const { port, close } = await createServer({
    docPath, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'),
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/src/config.json:1')}`)).json() as {
    html: string; title: string; targetLine: number;
  };
  assert.equal(boot.title, 'config.json');
  assert.equal(boot.targetLine, 1);
  assert.match(boot.html, /data-block-id="line-1"/);
  assert.match(boot.html, /language-json/);
  await close();
});

test('GET project-root-relative Markdown path uses the document renderer', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-markdown-file-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const markdownPath = join(root, 'docs', 'guide.md');
  mkdirSync(join(root, 'docs'));
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(markdownPath, '# 2026-02 Dining\n\n- first\n- second\n');
  const staticDir = join(root, 'static');
  mkdirSync(staticDir);
  writeFileSync(join(staticDir, 'index.html'), '<script src="/app.js"></script>');
  const { port, close } = await createServer({
    docPath, sessionDir: join(root, 'session'), prefsPath: join(root, 'prefs.json'), staticDir,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const initialBoot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: initialBoot.blockIds[0] }, message: 'Keep this note', kind: 'note' }),
  });
  const page = await fetch(`http://127.0.0.1:${port}/docs/guide.md`);
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/docs/guide.md')}`)).json() as {
    html: string; title: string; readOnly: boolean;
  };
  assert.equal(page.status, 200);
  assert.match(await page.text(), /app\.js/);
  assert.equal(boot.title, 'guide.md');
  assert.equal(boot.readOnly, false);
  assert.equal((boot as { sourceView?: boolean }).sourceView, true);
  assert.match(boot.html, /<h1[^>]*id="2026-02-dining"[^>]*>2026-02 Dining<\/h1>/);
  assert.match(boot.html, /<ul[^>]*>/);
  assert.doesNotMatch(boot.html, /# Guide/);
  const mainBoot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as {
    html: string;
    title: string;
    readOnly: boolean;
    threads: Array<{ messages: Array<{ text: string }> }>;
  };
  assert.equal(mainBoot.title, 'doc.md');
  assert.equal(mainBoot.readOnly, false);
  assert.match(mainBoot.html, /<h1[^>]*>Discussion<\/h1>/);
  assert.equal(mainBoot.threads.length, 1);
  assert.equal(mainBoot.threads[0]!.messages[0]!.text, 'Keep this note');
  await close();
});

test('GET Markdown path prefers the explicit launcher project root', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-explicit-root-'));
  const reportDir = join(root, 'report');
  const rawDir = join(root, 'raw', '2026-02');
  mkdirSync(reportDir, { recursive: true });
  mkdirSync(rawDir, { recursive: true });
  const docPath = join(reportDir, 'spending-by-month.md');
  const markdownPath = join(rawDir, 'other.md');
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(markdownPath, '<a id="2026-02-other"></a>\n\n# 2026-02 — Other\n');
  const staticDir = join(root, 'static');
  mkdirSync(staticDir);
  writeFileSync(join(staticDir, 'index.html'), '<script src="/app.js"></script>');
  const { port, close } = await createServer({
    docPath,
    projectRoot: root,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    staticDir,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const page = await fetch(`http://127.0.0.1:${port}/raw/2026-02/other.md`);
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/other.md')}`)).json() as {
    html: string;
    title: string;
    readOnly: boolean;
  };
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('content-type'), 'text/html');
  assert.match(await page.text(), /app\.js/);
  assert.equal(boot.title, 'other.md');
  assert.equal(boot.readOnly, false);
  assert.equal((boot as { sourceView?: boolean }).sourceView, true);
  assert.match(boot.html, /<h1[^>]*id="2026-02-other"[^>]*>2026-02 — Other<\/h1>/);
  await close();
});

test('Markdown subdocument annotations stay scoped to and persist in that file', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-subdoc-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'raw', '2026-02', 'dining.md');
  mkdirSync(join(root, 'raw', '2026-02'), { recursive: true });
  writeFileSync(docPath, '# Discussion\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# 2026-02 Dining\n\nRestaurant paragraph.\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const { port, close } = await createServer({
    docPath, projectRoot: root, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const sourceBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    blockIds: string[]; readOnly: boolean; documentPath: string; threads: unknown[];
  };
  assert.equal(sourceBoot.readOnly, false);
  assert.equal(sourceBoot.documentPath, subdocPath);
  assert.equal(sourceBoot.threads.length, 0);

  const created = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      documentPath: subdocPath,
      anchor: { blockId: sourceBoot.blockIds[1] },
      message: 'Add dining context', kind: 'note',
    }),
  });
  assert.equal(created.status, 200);
  const { threadId } = (await created.json()) as { threadId: string };

  const mainBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { threads: unknown[]; applyAvailable: boolean };
  assert.equal(mainBoot.threads.length, 0);
  assert.equal(mainBoot.applyAvailable, true);
  const sourceWithThread = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    threads: Array<{ documentPath?: string }>;
  };
  assert.equal(sourceWithThread.threads.length, 1);
  assert.equal(sourceWithThread.threads[0]!.documentPath, subdocPath);

  const closed = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'Keep the dining context.' }),
  });
  assert.equal(closed.status, 200);
  assert.match(readFileSync(subdocPath, 'utf8'), /<details/);
  assert.doesNotMatch(readFileSync(docPath, 'utf8'), /<details/);

  const sourceAfterClose = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    archivedThreads: unknown[];
  };
  assert.equal(sourceAfterClose.archivedThreads.length, 1);
  await close();
});

test('Apply availability is broadcast to every open document view', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-apply-availability-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'raw', '2026-02', 'dining.md');
  mkdirSync(join(root, 'raw', '2026-02'), { recursive: true });
  writeFileSync(docPath, '# Discussion\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# Dining\n\nRestaurant paragraph.\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const { port, close } = await createServer({
    docPath, projectRoot: root, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const eventsController = new AbortController();
  const events = await readEvents(`http://127.0.0.1:${port}/events`, eventsController.signal);
  const subdocBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    blockIds: string[]; documentPath: string;
  };
  const created = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ documentPath: subdocBoot.documentPath, anchor: { blockId: subdocBoot.blockIds[1] }, message: 'dining note', kind: 'note' }),
  });
  assert.equal(created.status, 200);
  const mainBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { applyAvailable: boolean };
  assert.equal(mainBoot.applyAvailable, true);
  await new Promise<void>((resolve) => setImmediate(resolve));
  eventsController.abort();
  assert.match(events.join('\n'), /event: server\.apply-availability[\s\S]*"applyAvailable":true/);
  await close();
});

test('A failed subdocument close preserves the live note instead of losing it', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-subdoc-close-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'raw', '2026-02', 'dining.md');
  mkdirSync(join(root, 'raw', '2026-02'), { recursive: true });
  writeFileSync(docPath, '# Discussion\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# Dining\n\nOriginal restaurant paragraph.\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const { port, close } = await createServer({
    docPath, projectRoot: root, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    blockIds: string[]; documentPath: string;
  };
  const created = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      documentPath: boot.documentPath,
      anchor: { blockId: boot.blockIds[1] },
      message: 'Keep this note', kind: 'note',
    }),
  });
  const { threadId } = (await created.json()) as { threadId: string };
  writeFileSync(subdocPath, '# Dining\n\nThe anchor was replaced externally.\n');

  const closed = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'Keep this note' }),
  });
  assert.equal(closed.status, 409);
  assert.doesNotMatch(readFileSync(subdocPath, 'utf8'), /<details/);
  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as {
    threads: Array<{ id: string; status: string }>;
  };
  assert.deepEqual(after.threads.map((thread) => [thread.id, thread.status]), [[threadId, 'open']]);
  await close();
});

test('A failed Apply rolls back archives across the main document and subdocument', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-apply-rollback-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'raw', '2026-02', 'dining.md');
  mkdirSync(join(root, 'raw', '2026-02'), { recursive: true });
  writeFileSync(docPath, '# Discussion\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# Dining\n\nOriginal restaurant paragraph.\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const { port, close } = await createServer({
    docPath, projectRoot: root, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });

  const mainBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[]; documentPath: string };
  const subBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/dining.md')}`)).json()) as { blockIds: string[]; documentPath: string };
  const addNote = async (documentPath: string, blockId: string, message: string): Promise<void> => {
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ documentPath, anchor: { blockId }, message, kind: 'note' }),
    });
    assert.equal(response.status, 200);
  };
  await addNote(mainBoot.documentPath, mainBoot.blockIds[1]!, 'Main note');
  await addNote(subBoot.documentPath, subBoot.blockIds[1]!, 'Subdocument note');
  writeFileSync(subdocPath, '# Dining\n\nThe anchor was replaced externally.\n');

  const applied = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applied.status, 500);
  assert.match(await applied.text(), /dining\.md/);
  assert.doesNotMatch(readFileSync(docPath, 'utf8'), /<details/);
  assert.doesNotMatch(readFileSync(subdocPath, 'utf8'), /<details/);
  const mainAfter = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: Array<{ status: string; messages: Array<{ text: string }> }>;
  };
  assert.deepEqual(mainAfter.threads.map((thread) => [thread.status, thread.messages[0]?.text]), [['open', 'Main note']]);
  await close();
});

test('Apply signal lists the main document and every changed Markdown subdocument', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-apply-subdoc-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const subdocPath = join(root, 'raw', '2026-02', 'other.md');
  mkdirSync(join(root, 'raw', '2026-02'), { recursive: true });
  writeFileSync(docPath, '# Discussion\n\nMain paragraph.\n');
  writeFileSync(subdocPath, '# Other\n\nSubdocument paragraph.\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));
  const { port, close } = await createServer({
    docPath, projectRoot: root, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }), shutdownOnFinish: false,
  });
  const mainBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const subBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/raw/2026-02/other.md')}`)).json()) as { blockIds: string[] };
  const addNote = async (documentPath: string, blockId: string, message: string): Promise<void> => {
    const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ documentPath, anchor: { blockId }, message, kind: 'note' }),
    });
    assert.equal(response.status, 200);
  };
  await addNote(docPath, mainBoot.blockIds[1]!, 'Main follow-up');
  await addNote(subdocPath, subBoot.blockIds[1]!, 'Subdocument follow-up');

  const applied = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applied.status, 200);
  const signal = JSON.parse(readFileSync(join(sessionDir, 'apply-1.json'), 'utf8')) as { documentPaths: string[] };
  assert.deepEqual(signal.documentPaths, [docPath, subdocPath]);
  assert.match(readFileSync(docPath, 'utf8'), /Main follow-up/);
  assert.match(readFileSync(subdocPath, 'utf8'), /Subdocument follow-up/);
  await close();
});

test('GET /<path>.svg serves a doc-relative asset from the doc directory', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession(
    '# Diagram\n\n![alt](./docs/diagram.svg)\n',
  );
  const docDir = join(docPath, '..');
  mkdirSync(join(docDir, 'docs'), { recursive: true });
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>';
  writeFileSync(join(docDir, 'docs', 'diagram.svg'), svg);
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/docs/diagram.svg`);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'image/svg+xml');
  assert.equal(await res.text(), svg);
  await close();
});

test('GET /<sibling>.svg works when the doc lives in a subdir of a git repo', async () => {
  // Layout:
  //   /tmp/ind-XXXX/
  //     .git/              (marks the repo root → serve root)
  //     assets/foo.png
  //     docs/note.md       (the discussion doc, in a subdir)
  // The doc references `../assets/foo.png` — must serve, because the repo
  // root contains both `docs/` and `assets/`.
  const root = mkdtempSync(join(tmpdir(), 'ind-'));
  mkdirSync(join(root, '.git'));
  mkdirSync(join(root, 'assets'));
  mkdirSync(join(root, 'docs'));
  const png = Buffer.from('89504e470d0a1a0a', 'hex'); // PNG magic prefix only
  writeFileSync(join(root, 'assets', 'foo.png'), png);
  const docPath = join(root, 'docs', 'note.md');
  writeFileSync(docPath, '# Note\n\n![](../assets/foo.png)\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));

  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  // The HTML img would render `../assets/foo.png` which the browser resolves
  // against `/docs/note.md` (the page is `/`) → request goes to /assets/foo.png.
  const res = await fetch(`http://127.0.0.1:${port}/assets/foo.png`);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'image/png');
  await close();
});

test('GET / climbs to project root via CLAUDE.md marker when no .git is present', async () => {
  // Layout: a project dir has CLAUDE.md but no .git. Doc lives at
  //   <root>/docs/note.md and references `./docs/foo.svg` — i.e. the page
  //   URL becomes /docs/foo.svg, which must resolve against the project
  //   root, not the doc's own directory (<root>/docs/, which would
  //   look for <root>/docs/docs/foo.svg).
  const root = mkdtempSync(join(tmpdir(), 'ind-'));
  writeFileSync(join(root, 'CLAUDE.md'), '# project notes');
  mkdirSync(join(root, 'docs'));
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>';
  writeFileSync(join(root, 'docs', 'foo.svg'), svg);
  const docPath = join(root, 'docs', 'note.md');
  writeFileSync(docPath, '# Note\n\n![](./docs/foo.svg)\n');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));

  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/docs/foo.svg`);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), svg);
  await close();
});

test('GET / refuses to escape the serve root via ../', async () => {
  // Put a "secret" file above the repo root; the request must NOT reach it.
  const root = mkdtempSync(join(tmpdir(), 'ind-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# X\n\nP.\n');
  const sibling = join(root, '..', 'leaked.svg');
  writeFileSync(sibling, '<svg/>');
  const sessionDir = join(root, 'session');
  const prefsPath = join(root, 'prefs.json');
  const transcriptPath = join(root, 'session.jsonl');
  writeFileSync(transcriptPath, JSON.stringify({ type: 'user', text: 'hi' }));

  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/..%2fleaked.svg`);
  assert.equal(res.status, 404);
  await close();
});

test('GET /<path>.txt is served with text/plain (no extension whitelist)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# X\n\nP.\n');
  const docDir = join(docPath, '..');
  writeFileSync(join(docDir, 'notes.txt'), 'hello');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const res = await fetch(`http://127.0.0.1:${port}/notes.txt`);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type') ?? '', /^text\/plain/);
  assert.equal(await res.text(), 'hello');
  await close();
});

test('GET /<dotfile> is refused even though it is inside the serve root', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# X\n\nP.\n');
  const docDir = join(docPath, '..');
  writeFileSync(join(docDir, '.env'), 'OPENAI_API_KEY=sk-should-never-be-served');
  mkdirSync(join(docDir, '.git'), { recursive: true });
  writeFileSync(join(docDir, '.git', 'config'), '[remote "origin"]\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  assert.equal((await fetch(`http://127.0.0.1:${port}/.env`)).status, 404);
  assert.equal((await fetch(`http://127.0.0.1:${port}/.git/config`)).status, 404);
  await close();
});

test('GET /api/bootstrap returns the doc title (filename with .md)', async () => {
  // scratchSession creates the doc at "doc.md" under a fresh tmp dir.
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# Hello Doc\n\nPara.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const json = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { title: string };
  assert.equal(json.title, 'doc.md');
  await close();
});

test('GET /events accepts SSE connection and responds with event-stream header', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const controller = new AbortController();
  const res = await fetch(`http://127.0.0.1:${port}/events`, { signal: controller.signal });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  controller.abort();
  await close();
});

test('GET /events sends the current Apply state for reconnecting clients', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const controller = new AbortController();
  const events = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((resolve) => setTimeout(resolve, 50));
  controller.abort();
  const joined = (await events).join('\n');
  assert.match(joined, /event: server\.apply-state/);
  assert.match(joined, /"applying":false/);
  assert.match(joined, /"applyStatus":null/);
  await close();
});

test('GET /api/prefs returns stored prefs; POST persists across server restarts', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const first = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  // Initial: empty prefs.
  const initial = await (await fetch(`http://127.0.0.1:${first.port}/api/prefs`)).json() as Record<string, string>;
  assert.deepEqual(initial, {});

  // Save theme=dark width=full.
  const saved = await (await fetch(`http://127.0.0.1:${first.port}/api/prefs`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ theme: 'dark', width: 'full' }),
  })).json() as Record<string, string>;
  assert.equal(saved.theme, 'dark');
  assert.equal(saved.width, 'full');
  await first.close();

  // Reopen on a fresh port — prefs should persist.
  const second = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const reloaded = await (await fetch(`http://127.0.0.1:${second.port}/api/prefs`)).json() as Record<string, string>;
  assert.equal(reloaded.theme, 'dark');
  assert.equal(reloaded.width, 'full');
  await second.close();
});

async function readEvents(url: string, signal: AbortSignal): Promise<string[]> {
  const res = await fetch(url, { signal });
  const reader = res.body!.getReader();
  const lines: string[] = [];
  const decoder = new TextDecoder();
  let buf = '';
  (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value);
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          lines.push(buf.slice(0, idx));
          buf = buf.slice(idx + 2);
        }
      }
    } catch { /* aborted */ }
  })();
  return lines;
}

test('external markdown file changes emit doc.updated and refresh bootstrap state', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nOriginal.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  writeFileSync(docPath, '# T\n\nExternal edit.\n');
  await new Promise((r) => setTimeout(r, 200));
  controller.abort();

  const events = (await eventsBuf).join('\n');
  assert.match(events, /event: doc\.updated/);
  assert.match(events, /External edit\./);

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { html: string };
  assert.match(boot.html, /External edit\./);

  await close();
});

// Highlights are pure session-only visual markers. They live in their own
// /api/highlights namespace and never enter liveThreads. Cover create/list,
// delete, convert→thread, convert→note, and the empty-message guard.
test('POST /api/highlights creates a session highlight; bootstrap exposes it; threads stay empty', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'one' } }),
  });
  assert.equal(r.status, 200);
  const { highlightId } = (await r.json()) as { highlightId: string };
  assert.match(highlightId, /^h-\d+$/);

  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: unknown[]; highlights: Array<{ id: string; anchor: { quote?: string } }>;
  };
  assert.equal(after.threads.length, 0);
  assert.equal(after.highlights.length, 1);
  assert.equal(after.highlights[0]!.id, highlightId);
  assert.equal(after.highlights[0]!.anchor.quote, 'one');
  await close();
});

test('DELETE /api/highlights/:id removes the highlight and a second delete 404s', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'one' } }),
  });
  const { highlightId } = (await r.json()) as { highlightId: string };
  const del = await fetch(`http://127.0.0.1:${port}/api/highlights/${highlightId}`, { method: 'DELETE' });
  assert.equal(del.status, 200);
  const again = await fetch(`http://127.0.0.1:${port}/api/highlights/${highlightId}`, { method: 'DELETE' });
  assert.equal(again.status, 404);
  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { highlights: unknown[] };
  assert.equal(after.highlights.length, 0);
  await close();
});

test('POST /api/highlights/:id/convert {to:"thread"} promotes to a thread and removes the highlight', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'agent reply', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'one' } }),
  });
  const { highlightId } = (await r.json()) as { highlightId: string };
  const conv = await fetch(`http://127.0.0.1:${port}/api/highlights/${highlightId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'thread', message: 'discuss this' }),
  });
  assert.equal(conv.status, 200);
  const body = (await conv.json()) as { threadId: string; kind: string };
  assert.equal(body.kind, 'thread');
  assert.match(body.threadId, /^t-\d+$/);

  // Give the agent stream a moment to settle, then check bootstrap shape.
  await new Promise((r2) => setTimeout(r2, 50));
  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    highlights: unknown[];
    threads: Array<{ id: string; kind: string; anchor: { quote?: string }; messages: Array<{ role: string; text: string }> }>;
  };
  assert.equal(after.highlights.length, 0);
  assert.equal(after.threads.length, 1);
  assert.equal(after.threads[0]!.kind, 'thread');
  assert.equal(after.threads[0]!.anchor.quote, 'one');
  // The user message we seeded survives.
  assert.equal(after.threads[0]!.messages[0]!.role, 'user');
  assert.equal(after.threads[0]!.messages[0]!.text, 'discuss this');
  await close();
});

test('POST /api/highlights/:id/convert {to:"note"} promotes to a note (no agent)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'agent reply', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'one' } }),
  });
  const { highlightId } = (await r.json()) as { highlightId: string };
  const conv = await fetch(`http://127.0.0.1:${port}/api/highlights/${highlightId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'note', message: 'my note text' }),
  });
  assert.equal(conv.status, 200);
  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    highlights: unknown[];
    threads: Array<{ id: string; kind: string; messages: Array<{ role: string; text: string }> }>;
  };
  assert.equal(after.highlights.length, 0);
  assert.equal(after.threads.length, 1);
  assert.equal(after.threads[0]!.kind, 'note');
  assert.equal(after.threads[0]!.messages[0]!.text, 'my note text');
  await close();
});

test('POST /api/highlights/:id/convert {to:"thread"} 400s on empty message', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId, quote: 'one' } }),
  });
  const { highlightId } = (await r.json()) as { highlightId: string };
  const conv = await fetch(`http://127.0.0.1:${port}/api/highlights/${highlightId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'thread', message: '   ' }),
  });
  assert.equal(conv.status, 400);
  await close();
});

test('POST /api/highlights without anchor.blockId returns 400', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP one.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const r = await fetch(`http://127.0.0.1:${port}/api/highlights`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: {} }),
  });
  assert.equal(r.status, 400);
  await close();
});

test('POST /api/threads creates a thread, streams assistant deltas via SSE', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'short answer', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const anchorBlockId = boot.blockIds[1]!; // the paragraph, not the heading

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: anchorBlockId }, message: 'why?' }),
  });
  assert.equal(r.status, 200);
  const body = (await r.json()) as { threadId: string };
  assert.match(body.threadId, /^t-\d+$/);

  await new Promise((res) => setTimeout(res, 100));
  controller.abort();
  const lines = await eventsBuf;
  const joined = lines.join('\n');
  assert.match(joined, /event: thread\.message\.delta/);
  assert.match(joined, /short answer/);
  assert.match(joined, /event: thread\.message\.done/);
  await close();
});

test('POST /api/threads forwards agent status updates via SSE', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const statusAgentFactory: AgentFactory = () => {
    const agent: ThreadAgent = {
      async *send() {
        yield { type: 'status', status: 'Using Read...' };
        yield { type: 'delta', text: 'answer' };
        yield { type: 'done', text: 'answer' };
      },
      async *proposeConclusion() {
        yield { type: 'done', text: 'c' };
      },
      snapshot: () => [],
    };
    return agent;
  };
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: statusAgentFactory,
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1]! }, message: 'why?' }),
  });
  assert.equal(r.status, 200);

  await new Promise((res) => setTimeout(res, 100));
  controller.abort();
  const joined = (await eventsBuf).join('\n');
  assert.match(joined, /event: thread\.message\.status/);
  assert.match(joined, /"status":"Using Read\.\.\."/);
  assert.match(joined, /event: thread\.message\.done/);
  await close();
});

test('POST /api/threads forwards agent activity updates via SSE', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nAnchor paragraph.\n');
  const activityAgentFactory: AgentFactory = () => {
    const agent: ThreadAgent = {
      async *send() {
        yield { type: 'activity', activity: { kind: 'tool', title: 'Tool', text: 'Ran pwd (exit 0)' } };
        yield { type: 'delta', text: 'answer' };
        yield { type: 'done', text: 'answer' };
      },
      async *proposeConclusion() {
        yield { type: 'done', text: 'c' };
      },
      snapshot: () => [],
    };
    return agent;
  };
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: activityAgentFactory,
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1]! }, message: 'why?' }),
  });
  assert.equal(r.status, 200);

  await new Promise((res) => setTimeout(res, 100));
  controller.abort();
  const joined = (await eventsBuf).join('\n');
  assert.match(joined, /event: thread\.message\.activity/);
  assert.match(joined, /"kind":"tool"/);
  assert.match(joined, /Ran pwd/);
  assert.match(joined, /event: thread\.message\.done/);
  await close();
});

test('POST /api/threads/:id/messages streams a follow-up reply', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: mockAgentFactory({ reply: 'more', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const r1 = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  });
  const { threadId } = (await r1.json()) as { threadId: string };

  const r2 = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/messages`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message: 'and?' }),
  });
  assert.equal(r2.status, 202);
  await close();
});

test('POST /api/threads/:id/propose-conclusion streams the proposal', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'proposed answer' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/propose-conclusion`, { method: 'POST' });
  assert.equal(r.status, 202);
  await new Promise((r) => setTimeout(r, 100));
  controller.abort();
  const lines = await eventsBuf;
  assert.match(lines.join('\n'), /event: thread\.conclusion\.proposed/);
  assert.match(lines.join('\n'), /proposed answer/);
  await close();
});

test('POST /api/threads/:id/close writes <details> into doc and broadcasts doc.updated', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };

  await new Promise((r) => setTimeout(r, 50));
  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'Resolved.' }),
  });
  assert.equal(r.status, 200);
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /💬 Thread on entire block — \d{4}-\d{2}-\d{2}/);
  assert.match(after, /<div class="archived-conclusion" data-raw="Resolved\.">/);
  await close();
});

test('user message is retained when the agent throws before emitting done', async () => {
  const failingAgentFactory: AgentFactory = () =>
    ({
      async *send() {
        yield { type: 'delta', text: 'partial ' };
        throw new Error('agent blew up');
      },
      async *proposeConclusion() {
        yield { type: 'done', text: 'auto conclusion after failure' };
      },
      snapshot: () => [],
    }) satisfies ThreadAgent;

  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath,
    sessionDir,
    mainJsonlPath: transcriptPath,
    prefsPath,
    agentFactory: failingAgentFactory,
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'important question' }),
  })).json()) as { threadId: string };

  await new Promise((r) => setTimeout(r, 100));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'closed after failure' }),
  });
  assert.equal(r.status, 200);
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /data-role="user" data-raw="important question"/);
  await close();
});

test('POST /api/threads with kind=note stores a user message but does NOT dispatch an agent', async () => {
  let agentCreated = 0;
  const trackingFactory: AgentFactory = () => {
    agentCreated += 1;
    return {
      async *send() { yield { type: 'done', text: 'should not run' }; },
      async *proposeConclusion() { yield { type: 'done', text: 'should not run' }; },
      snapshot: () => [],
    } satisfies ThreadAgent;
  };
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: trackingFactory,
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };

  const r = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'just a note', kind: 'note' }),
  });
  assert.equal(r.status, 200);
  const body = (await r.json()) as { threadId: string; kind: string };
  assert.equal(body.kind, 'note');

  // Allow event-loop turn for any stray agent.send to fire.
  await new Promise((res) => setTimeout(res, 50));
  assert.equal(agentCreated, 0, 'no agent should be created for a note');

  // Close the note: conclusion defaults to note text; doc gets a 📝 Note block.
  const closeR = await fetch(`http://127.0.0.1:${port}/api/threads/${body.threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'just a note' }),
  });
  assert.equal(closeR.status, 200);
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /📝 Note on entire block — \d{4}-\d{2}-\d{2}/);
  assert.match(after, /just a note/);
  await close();
});

test('POST /api/finish auto-archives open notes using the note text as conclusion', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'auto claude conclusion' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'open note body', kind: 'note' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' });
  assert.equal(r.status, 200);
  const result = (await r.json()) as { conclusions: Array<{ threadId: string; conclusion: string; closedBy: string }> };
  const entry = result.conclusions.find((c) => c.threadId === threadId)!;
  assert.equal(entry.closedBy, 'auto');
  assert.equal(entry.conclusion, 'open note body');
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /📝 Note on entire block/);
  assert.match(after, /open note body/);
  assert.doesNotMatch(after, /auto claude conclusion/);
  await close();
});

test('DELETE /api/threads/:id discards a live note without writing to the doc', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const before = readFileSync(docPath, 'utf8');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'scratch note', kind: 'note' }),
  })).json()) as { threadId: string };

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}`, { method: 'DELETE' });
  assert.equal(r.status, 200);
  assert.equal(readFileSync(docPath, 'utf8'), before, 'doc must be unchanged after delete');

  // Thread is gone from bootstrap.
  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { threads: Array<{ id: string }> };
  assert.equal(after.threads.find((t) => t.id === threadId), undefined);
  await close();
});

test('DELETE /api/threads/:id deletes a closed thread and removes its details block', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 30));
  await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'done' }),
  });
  assert.match(readFileSync(docPath, 'utf8'), /data-thread-id="t-1"/);

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}`, { method: 'DELETE' });
  assert.equal(r.status, 200);
  const afterDoc = readFileSync(docPath, 'utf8');
  assert.doesNotMatch(afterDoc, /data-thread-id="t-1"/);
  assert.doesNotMatch(afterDoc, /data-raw="done"/);

  const afterBootstrap = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: Array<{ id: string }>;
  };
  assert.equal(afterBootstrap.threads.find((t) => t.id === threadId), undefined);
  await close();
});

test('PATCH /api/threads/:id/note edits a live note in place', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'original note', kind: 'note' }),
  })).json()) as { threadId: string };

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/note`, {
    method: 'PATCH', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message: 'edited note text' }),
  });
  assert.equal(r.status, 200);
  // Closing after edit archives the edited text, not the original.
  await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' });
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /edited note text/);
  assert.doesNotMatch(after, /original note/);
  await close();
});

test('PATCH /api/threads/:id/note refuses non-notes (400)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 30));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/note`, {
    method: 'PATCH', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message: 'x' }),
  });
  assert.equal(r.status, 400);
  await close();
});

test('PUT /api/threads/:id/conclusion rewrites the archived <details> block in place', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 30));
  await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/close`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'first pass' }),
  });
  const firstDoc = readFileSync(docPath, 'utf8');
  assert.match(firstDoc, /data-thread-id="t-1"/);
  assert.match(firstDoc, /<div class="archived-conclusion" data-raw="first pass">/);

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/conclusion`, {
    method: 'PUT', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'revised wording' }),
  });
  assert.equal(r.status, 200);
  const after = readFileSync(docPath, 'utf8');
  assert.match(after, /<div class="archived-conclusion" data-raw="revised wording">/);
  assert.doesNotMatch(after, /data-raw="first pass"/);
  // Exactly one <details> block — we replaced, not appended.
  assert.equal([...after.matchAll(/<details/g)].length, 1);
  await close();
});

test('PUT /api/threads/:id/conclusion refuses to edit an open thread (409)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 30));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/conclusion`, {
    method: 'PUT', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conclusion: 'nope' }),
  });
  assert.equal(r.status, 409);
  await close();
});

test('DELETE /api/threads/:id removes an archived <details> block from the doc', async () => {
  // Seed a doc with two pre-existing archived threads. scratchSession loads it.
  const seeded = 'Anchor.\n\n' +
    '<details data-thread-id="t-1"><summary>💬 Thread on entire block — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q1"><strong>User:</strong> q1</div>\n' +
    '<div class="archived-msg" data-role="assistant" data-raw="a1"><strong>Claude:</strong> a1</div>\n' +
    '<div class="archived-conclusion" data-raw="first conclusion"><strong>Conclusion:</strong> first conclusion</div>\n' +
    '</details>\n\n' +
    '<details data-thread-id="t-2"><summary>💬 Thread on entire block — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q2"><strong>User:</strong> q2</div>\n' +
    '<div class="archived-msg" data-role="assistant" data-raw="a2"><strong>Claude:</strong> a2</div>\n' +
    '<div class="archived-conclusion" data-raw="second conclusion"><strong>Conclusion:</strong> second conclusion</div>\n' +
    '</details>\n';
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession(seeded);
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    archivedThreads: Array<{ id: string; conclusion?: string }>;
  };
  assert.equal(boot.archivedThreads.length, 2);
  // Delete the first archived thread.
  const targetId = boot.archivedThreads[0]!.id;

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${targetId}`, { method: 'DELETE' });
  assert.equal(r.status, 200);

  const after = readFileSync(docPath, 'utf8');
  assert.doesNotMatch(after, /first conclusion/);
  assert.match(after, /second conclusion/);

  // Remaining archive now has a fresh archived-1 id.
  const reboot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    archivedThreads: Array<{ id: string; conclusion?: string }>;
  };
  assert.equal(reboot.archivedThreads.length, 1);
  assert.equal(reboot.archivedThreads[0]!.id, 'archived-1');
  assert.equal(reboot.archivedThreads[0]!.conclusion, 'second conclusion');
  await close();
});

test('DELETE /api/threads/:id returns 404 for an unknown id (neither live nor archived)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const r = await fetch(`http://127.0.0.1:${port}/api/threads/does-not-exist`, { method: 'DELETE' });
  assert.equal(r.status, 404);
  await close();
});

test('DELETE /api/threads/:id emits thread.deleted before doc.updated so re-numbered ids do not collide', async () => {
  // Regression: if doc.updated (with re-numbered archivedThreads) reaches the
  // client before thread.deleted, the old deleted id can match a survivor's
  // new id — the client then drops the survivor and both archived chips
  // vanish. Keep thread.deleted strictly ahead of doc.updated so the deleted
  // id is interpreted against the pre-re-numbering archive map.
  const seeded = 'Anchor.\n\n' +
    '<details data-thread-id="t-1"><summary>💬 Thread on entire block — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q1"><strong>User:</strong> q1</div>\n' +
    '<div class="archived-conclusion" data-raw="c1"><strong>Conclusion:</strong> c1</div>\n' +
    '</details>\n\n' +
    '<details data-thread-id="t-2"><summary>💬 Thread on entire block — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q2"><strong>User:</strong> q2</div>\n' +
    '<div class="archived-conclusion" data-raw="c2"><strong>Conclusion:</strong> c2</div>\n' +
    '</details>\n';
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession(seeded);
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    archivedThreads: Array<{ id: string }>;
  };
  const targetId = boot.archivedThreads[0]!.id;
  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${targetId}`, { method: 'DELETE' });
  assert.equal(r.status, 200);

  await new Promise((r) => setTimeout(r, 100));
  controller.abort();
  const joined = (await eventsBuf).join('\n');
  const deletedIdx = joined.indexOf('event: thread.deleted');
  const updatedIdx = joined.indexOf('event: doc.updated');
  assert.ok(deletedIdx >= 0, 'thread.deleted event missing');
  assert.ok(updatedIdx >= 0, 'doc.updated event missing');
  assert.ok(deletedIdx < updatedIdx, 'thread.deleted must precede doc.updated');
  await close();
});

test('POST /api/threads/:id/convert {to: "note"} collapses a thread to a single user message', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'useful assistant reply', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'question' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'note' }),
  });
  assert.equal(r.status, 200);

  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: Array<{ id: string; kind: string; messages: Array<{ role: string; text: string }> }>;
  };
  const converted = after.threads.find((t) => t.id === threadId)!;
  assert.equal(converted.kind, 'note');
  assert.equal(converted.messages.length, 1);
  assert.equal(converted.messages[0]!.role, 'user');
  // Body defaults to the last assistant reply — the useful outcome to capture.
  assert.equal(converted.messages[0]!.text, 'useful assistant reply');
  await close();
});

test('POST /api/threads/:id/convert {to: "thread"} spawns an agent and streams a reply', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'agent reply to the note', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'note becoming a question', kind: 'note' }),
  })).json()) as { threadId: string };

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'thread' }),
  });
  assert.equal(r.status, 200);
  // Give runStreamReply a beat to push the assistant message.
  await new Promise((r) => setTimeout(r, 50));

  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: Array<{ id: string; kind: string; messages: Array<{ role: string; text: string }> }>;
  };
  const converted = after.threads.find((t) => t.id === threadId)!;
  assert.equal(converted.kind, 'thread');
  // Original note text is preserved as the first user message, followed by
  // the agent's reply — duplicated user pushes would break this.
  assert.equal(converted.messages.length, 2);
  assert.deepEqual(
    converted.messages.map((m) => [m.role, m.text]),
    [['user', 'note becoming a question'], ['assistant', 'agent reply to the note']],
  );
  await close();
});

test('POST /api/threads/:id/convert rejects same-kind conversions (409)', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Anchor.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'x', kind: 'note' }),
  })).json()) as { threadId: string };

  const r = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/convert`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ to: 'note' }),
  });
  assert.equal(r.status, 409);
  await close();
});

test('POST /api/finish auto-closes open threads and writes result.json', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'auto conclusion' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'hi' }),
  })).json()) as { threadId: string };
  await new Promise((r) => setTimeout(r, 50));

  const r = await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' });
  assert.equal(r.status, 200);
  const result = (await r.json()) as { conclusions: Array<{ threadId: string; conclusion: string; closedBy: string }>; docPath: string };
  assert.equal(result.conclusions.length, 1);
  assert.equal(result.conclusions[0]!.threadId, threadId);
  assert.equal(result.conclusions[0]!.closedBy, 'auto');
  assert.match(result.conclusions[0]!.conclusion, /auto conclusion/);

  const resultFile = JSON.parse(readFileSync(join(sessionDir, 'result.json'), 'utf8')) as { docPath: string };
  assert.equal(resultFile.docPath, docPath);
  await close();
});

test('POST /api/apply hands the signal back to the running main session', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const prompts: string[] = [];
  const mainSession: MainSessionBridge = { send: async (prompt) => { prompts.push(prompt); } };
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    mainSession,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'note', kind: 'note' }),
  });

  const response = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(response.status, 200);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0]!, /\/inline-discussion:apply/);
  assert.match(prompts[0]!, /apply-1\.json/);
  await fetch(`http://127.0.0.1:${port}/api/apply/failed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: 'test cleanup' }),
  });
  await close();
});

test('POST /api/finish hands the result back to the running main session', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('Para.\n');
  const prompts: string[] = [];
  const mainSession: MainSessionBridge = { send: async (prompt) => { prompts.push(prompt); } };
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    mainSession,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[0] }, message: 'note', kind: 'note' }),
  });

  const response = await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' });
  assert.equal(response.status, 200);
  assert.equal(prompts.length, 1);
  assert.match(prompts[0]!, /result\.json/);
  assert.match(prompts[0]!, /finished/);
  await close();
});

test('app-server handoff completes Apply without a legacy monitoring wait', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const mainSession: MainSessionBridge = { send: async () => {} };
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    mainSession,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note', kind: 'note' }),
  });
  await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  const done = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(done.status, 200);

  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean;
    applyStatus: string | null;
  };
  assert.equal(after.applying, false);
  assert.equal(after.applyStatus, null);

  const monitoring = await fetch(`http://127.0.0.1:${port}/api/apply/monitoring`, { method: 'POST' });
  assert.deepEqual(await monitoring.json(), { ok: true, ignored: true });
  await close();
});

test('GET /api/bootstrap returns applying=false and applyStatus=null on a fresh session', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const json = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean;
    applyStatus: string | null;
  };
  assert.equal(json.applying, false);
  assert.equal(json.applyStatus, null);
  await close();
});

test('POST /api/apply archives open threads, writes apply-1.json, sets applying=true', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nA paragraph.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'C' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1];
  // Add one note so there is something to apply.
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'note text', kind: 'note' }),
  });

  const res = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(res.status, 200);
  const body = (await res.json()) as { applyIndex: number };
  assert.equal(body.applyIndex, 1);

  const signal = JSON.parse(readFileSync(join(sessionDir, 'apply-1.json'), 'utf8')) as {
    mode: string; applyIndex: number; conclusions: unknown[];
  };
  assert.equal(signal.mode, 'apply');
  assert.equal(signal.applyIndex, 1);
  assert.equal(signal.conclusions.length, 1);

  // bootstrap now reports applying=true
  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { applying: boolean };
  assert.equal(boot2.applying, true);

  await close();
});

test('POST /api/apply returns 409 already-applying while an apply is in progress', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note', kind: 'note' }),
  });
  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyRes.status, 200);

  const alreadyApplying = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(alreadyApplying.status, 409);
  const body = await alreadyApplying.json() as { error: string };
  assert.equal(body.error, 'already-applying');

  await close();
});

test('POST /api/apply/progress updates bootstrap state and emits SSE while applying', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const notApplying = await fetch(`http://127.0.0.1:${port}/api/apply/progress`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status: 'early', percent: 1 }),
  });
  assert.equal(notApplying.status, 409);
  assert.deepEqual(await notApplying.json(), { error: 'not-applying' });

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note', kind: 'note' }),
  });
  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyRes.status, 200);

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const progressRes = await fetch(`http://127.0.0.1:${port}/api/apply/progress`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status: 'Editing introduction', current: 1, total: 4 }),
  });
  assert.equal(progressRes.status, 200);
  const progressBody = await progressRes.json() as {
    ok: boolean;
    progress: { status: string; percent: number | null; current?: number; total?: number };
  };
  assert.equal(progressBody.ok, true);
  assert.equal(progressBody.progress.status, 'Editing introduction');
  assert.equal(progressBody.progress.percent, 25);
  assert.equal(progressBody.progress.current, 1);
  assert.equal(progressBody.progress.total, 4);

  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applyStatus: string | null;
    applyProgress: { status: string; percent: number | null };
  };
  assert.equal(boot2.applyStatus, 'Editing introduction');
  assert.equal(boot2.applyProgress.status, 'Editing introduction');
  assert.equal(boot2.applyProgress.percent, 25);

  await new Promise((r) => setTimeout(r, 50));
  controller.abort();
  const events = (await eventsBuf).join('\n');
  assert.match(events, /event: server\.apply-progress/);
  assert.match(events, /"status":"Editing introduction"/);
  assert.match(events, /"percent":25/);

  await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  await close();
});

test('POST /api/apply/progress refreshes the inactivity timeout for active work', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  let close: (() => Promise<void>) | undefined;
  try {
    const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
    const server = await createServer({
      docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
      agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
      shutdownOnFinish: false,
    });
    close = server.close;
    const boot = (await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json()) as { blockIds: string[] };
    await fetch(`http://127.0.0.1:${server.port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note', kind: 'note' }),
    });
    const applyRes = await fetch(`http://127.0.0.1:${server.port}/api/apply`, { method: 'POST' });
    assert.equal(applyRes.status, 200);

    mock.timers.tick(15 * 60 * 1000 - 1);
    const beforeProgress = (await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json()) as { applying: boolean };
    assert.equal(beforeProgress.applying, true);

    const progressRes = await fetch(`http://127.0.0.1:${server.port}/api/apply/progress`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ status: 'Still working', percent: 50 }),
    });
    assert.equal(progressRes.status, 200);

    mock.timers.tick(15 * 60 * 1000 - 1);
    const afterProgress = (await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json()) as { applying: boolean };
    assert.equal(afterProgress.applying, true);

    mock.timers.tick(1);
    const afterIdle = (await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json()) as { applying: boolean };
    assert.equal(afterIdle.applying, false);
  } finally {
    await close?.();
    mock.timers.reset();
  }
});

test('mutating endpoints return 409 while applying=true', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1];
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'note', kind: 'note' }),
  });
  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyRes.status, 200);

  // Now applying=true; attempt to add another thread.
  const blocked = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'second', kind: 'note' }),
  });
  assert.equal(blocked.status, 409);
  const body = await blocked.json() as { error: string };
  assert.equal(body.error, 'applying');

  // /api/finish also blocked.
  const finishBlocked = await fetch(`http://127.0.0.1:${port}/api/finish`, { method: 'POST' });
  assert.equal(finishBlocked.status, 409);

  await close();
});

test('POST /api/apply/done reloads doc, waits for monitoring, then apply/monitoring clears applying', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nOne.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1];
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'note', kind: 'note' }),
  });
  await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), true);

  // Simulate main agent editing the doc.
  writeFileSync(docPath, '# T\n\nOne.\n\nMain added this paragraph.\n');

  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const done = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(done.status, 200);

  await new Promise((r) => setTimeout(r, 50));

  // The doc is refreshed, but the apply lifecycle stays active until the main
  // session starts the next wait loop and calls /api/apply/monitoring.
  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean;
    blockIds: string[];
    applyStatus: string | null;
    applyTasks: Array<{ label: string; state: string }>;
  };
  assert.equal(boot2.applying, true);
  assert.equal(boot2.applyStatus, 'Waiting for main session monitoring...');
  assert.equal(boot2.applyTasks.at(-1)?.state, 'active');
  // Doc has the new content reflected in block count.
  assert.equal(boot2.blockIds.length >= 3, true);

  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), false);

  const monitoring = await fetch(`http://127.0.0.1:${port}/api/apply/monitoring`, { method: 'POST' });
  assert.equal(monitoring.status, 200);
  const monitoringBody = (await monitoring.json()) as { ok: boolean; tasks: Array<{ state: string }> };
  assert.equal(monitoringBody.ok, true);
  assert.equal(monitoringBody.tasks.at(-1)?.state, 'done');

  await new Promise((r) => setTimeout(r, 50));
  controller.abort();
  const events = (await eventsBuf).join('\n');
  assert.match(events, /event: doc\.reloaded/);
  assert.match(events, /"html":"/);
  assert.match(events, /"blockIds":\[/);
  assert.match(events, /event: server\.apply-complete/);

  const boot3 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { applying: boolean; applyStatus: string | null };
  assert.equal(boot3.applying, false);
  assert.equal(boot3.applyStatus, null);

  await close();
});

test('POST /api/apply {removeThreads:true} strips all archived blocks on apply/done', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nKeep me.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note text', kind: 'note' }),
  });

  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ removeThreads: true }),
  });
  assert.equal(applyRes.status, 200);
  // The note was archived into the doc as a <details> block.
  assert.match(readFileSync(docPath, 'utf8'), /<details/);

  const done = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(done.status, 200);

  // Server stripped every archived block; the original prose is untouched.
  const docAfter = readFileSync(docPath, 'utf8');
  assert.doesNotMatch(docAfter, /<details/);
  assert.match(docAfter, /Keep me\./);

  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { archivedThreads: unknown[] };
  assert.equal(boot2.archivedThreads.length, 0);

  await close();
});

test('POST /api/apply without removeThreads keeps archived blocks on apply/done', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nKeep me.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'note text', kind: 'note' }),
  });

  // No body at all — legacy callers must keep the default "keep" behaviour.
  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyRes.status, 200);

  const done = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(done.status, 200);

  assert.match(readFileSync(docPath, 'utf8'), /<details/);
  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { archivedThreads: unknown[] };
  assert.equal(boot2.archivedThreads.length, 1);

  await close();
});

test('POST /api/apply/failed clears applying and unlinks apply-N.json on disk', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1];
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'n', kind: 'note' }),
  });
  await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), true);

  const fail = await fetch(`http://127.0.0.1:${port}/api/apply/failed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: 'oops' }),
  });
  assert.equal(fail.status, 200);

  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { applying: boolean };
  assert.equal(boot2.applying, false);
  // F4: failed must unlink the signal file so launch.sh's apply-*.json glob
  // can't re-pick it on the next poll, looping the agent indefinitely.
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), false);

  await close();
});

test('apply counter increments across multiple applies in one session', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  async function noteAndApply(i: number): Promise<number> {
    const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
    await fetch(`http://127.0.0.1:${port}/api/threads`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: `n${i}`, kind: 'note' }),
    });
    const r = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
    const body = (await r.json()) as { applyIndex: number };
    await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
    await fetch(`http://127.0.0.1:${port}/api/apply/monitoring`, { method: 'POST' });
    return body.applyIndex;
  }

  assert.equal(await noteAndApply(1), 1);
  assert.equal(await noteAndApply(2), 2);
  assert.equal(await noteAndApply(3), 3);

  await close();
});

// Regression for F3: if archiveAllOpenThreads (or anything else in the apply
// prelude) throws, /api/apply must return 500, flip state.applying back to
// false, and emit server.apply-failed. Without the recovery the session would
// be stuck in applying mode for 5 minutes (no timeout was armed) until the
// operator manually POSTed /api/apply/failed.
test('POST /api/apply with throwing prelude returns 500, clears applying, emits server.apply-failed', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nA paragraph.\n');
  // Custom agent factory whose proposeConclusion throws — this is the path
  // archiveAllOpenThreads takes for non-note threads.
  const throwingFactory: AgentFactory = (_opts) => {
    const agent: ThreadAgent = {
      async *send(_userText: string) {
        yield { type: 'done', text: 'r' };
      },
      proposeConclusion(): AsyncIterable<{ type: 'delta' | 'done'; text: string }> {
        return {
          [Symbol.asyncIterator]() {
            return {
              next(): Promise<IteratorResult<{ type: 'delta' | 'done'; text: string }>> {
                return Promise.reject(new Error('boom'));
              },
            };
          },
        };
      },
      snapshot: () => [],
    };
    return agent;
  };
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: throwingFactory,
    shutdownOnFinish: false,
  });

  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;
  // Create a 'thread' (NOT a note) so archiveAllOpenThreads must call
  // proposeConclusion and the throwing factory is exercised.
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'q?', kind: 'thread' }),
  });

  // Subscribe to SSE before triggering apply so we can capture the
  // server.apply-failed frame.
  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  const res = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(res.status, 500);

  // applying must be back to false.
  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean; applyStatus: string | null;
  };
  assert.equal(boot2.applying, false);
  assert.equal(boot2.applyStatus, null);

  await new Promise((r) => setTimeout(r, 50));
  controller.abort();
  const lines = await eventsBuf;
  const joined = lines.join('\n');
  assert.match(joined, /event: server\.apply-failed/);
  assert.match(joined, /"error":"boom"/);

  await close();
});

// Regression for F4: Apply commits in-session threads into the doc, so the
// live view must reset on /api/apply/done. Without clearing liveThreads /
// agents, /api/bootstrap would surface stale threads from the round we just
// archived and a page reload would re-render their cards.
test('POST /api/apply/done clears liveThreads and agents after doc reload', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nPara.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;

  // One note (populates liveThreads only) and one thread (populates both
  // liveThreads and agents) so the assertion covers both maps.
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'note', kind: 'note' }),
  });
  const threadRes = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'q?', kind: 'thread' }),
  });
  const { threadId } = (await threadRes.json()) as { threadId: string };

  await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  // Simulate the main agent editing the doc before signalling done.
  writeFileSync(docPath, '# T\n\nPara.\n\nMain added this paragraph.\n');
  const done = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(done.status, 200);

  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    threads: unknown[];
  };
  assert.equal(boot2.threads.length, 0);

  const monitoring = await fetch(`http://127.0.0.1:${port}/api/apply/monitoring`, { method: 'POST' });
  assert.equal(monitoring.status, 200);

  // Agents map is private state — verify indirectly: messaging the now-cleared
  // thread must 404, proving the agent entry was removed alongside the thread.
  const msgRes = await fetch(`http://127.0.0.1:${port}/api/threads/${threadId}/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message: 'after apply' }),
  });
  assert.equal(msgRes.status, 404);

  await close();
});

test('POST /api/apply with no live threads returns 400 nothing-to-apply', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const r = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(r.status, 400);
  const body = (await r.json()) as { error: string };
  assert.equal(body.error, 'nothing-to-apply');
  await close();
});

// Plan Task 5 — guardApplying middleware: every mutating endpoint must return
// 409 apply-in-progress while state.applying === true so a long-running apply
// can't be raced by a thread mutation that drifts the doc state out from under
// it. Server returns body { error: 'applying' } (not 'apply-in-progress' — the
// short token in the description above is the human-readable status, the wire
// format stays 'applying' to match the existing client). Eight endpoints are
// guarded: /api/threads (POST), /api/threads/:id/messages, .../close, DELETE
// /api/threads/:id, PATCH .../note, PUT .../conclusion, POST .../convert, and
// POST /api/finish.
test('guardApplying blocks all 8 mutating endpoints with 409 apply-in-progress while applying=true', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  const blockId = boot.blockIds[1]!;

  // Need a live thread so /api/apply does not 400 nothing-to-apply.
  const { threadId } = (await (await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId }, message: 'note', kind: 'note' }),
  })).json()) as { threadId: string };

  const applyRes = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyRes.status, 200);

  type Endpoint = { method: string; path: string; body?: unknown };
  const endpoints: Endpoint[] = [
    { method: 'POST', path: '/api/threads', body: { anchor: { blockId }, message: 'x', kind: 'note' } },
    { method: 'POST', path: `/api/threads/${threadId}/messages`, body: { message: 'x' } },
    { method: 'POST', path: `/api/threads/${threadId}/close`, body: { conclusion: 'x' } },
    { method: 'DELETE', path: `/api/threads/${threadId}` },
    { method: 'PATCH', path: `/api/threads/${threadId}/note`, body: { message: 'x' } },
    { method: 'PUT', path: `/api/threads/${threadId}/conclusion`, body: { conclusion: 'x' } },
    { method: 'POST', path: `/api/threads/${threadId}/convert`, body: { to: 'thread' } },
    { method: 'POST', path: '/api/finish' },
  ];

  for (const ep of endpoints) {
    const init: RequestInit = ep.body !== undefined
      ? { method: ep.method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(ep.body) }
      : { method: ep.method };
    const r = await fetch(`http://127.0.0.1:${port}${ep.path}`, init);
    assert.equal(r.status, 409, `${ep.method} ${ep.path} should return 409 while applying=true`);
    const body = (await r.json()) as { error: string };
    assert.equal(body.error, 'applying', `${ep.method} ${ep.path} body.error should be "applying"`);
  }

  await close();
});

// Plan Task 7 — POST /api/apply/failed must:
//   - return 409 { error: 'not-applying' } when state.applying === false,
//   - while applying=true: return 200 { ok: true }, reset state.applying and
//     state.applyStatus, clear the pending 5-min timeout, and emit an SSE
//     server.apply-failed frame carrying the supplied error payload.
test('POST /api/apply/failed: 409 when not applying; 200 + state reset + SSE event when applying', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  // Path A: applying=false → 409 not-applying.
  const noopRes = await fetch(`http://127.0.0.1:${port}/api/apply/failed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: 'irrelevant' }),
  });
  assert.equal(noopRes.status, 409);
  assert.deepEqual(await noopRes.json(), { error: 'not-applying' });

  // Prime + apply so state.applying flips to true.
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1]! }, message: 'n', kind: 'note' }),
  });
  await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  const midBoot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean; applyStatus: string | null;
  };
  assert.equal(midBoot.applying, true);
  assert.notEqual(midBoot.applyStatus, null);

  // Subscribe to SSE before issuing failed so we capture the frame.
  const controller = new AbortController();
  const eventsBuf = readEvents(`http://127.0.0.1:${port}/events`, controller.signal);
  await new Promise((r) => setTimeout(r, 50));

  // Path B: applying=true → 200 + state cleared.
  const failed = await fetch(`http://127.0.0.1:${port}/api/apply/failed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: 'main agent crashed' }),
  });
  assert.equal(failed.status, 200);
  assert.deepEqual(await failed.json(), { ok: true });

  const after = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean; applyStatus: string | null;
  };
  assert.equal(after.applying, false);
  assert.equal(after.applyStatus, null);

  await new Promise((r) => setTimeout(r, 50));
  controller.abort();
  const lines = await eventsBuf;
  const joined = lines.join('\n');
  assert.match(joined, /event: server\.apply-failed/);
  assert.match(joined, /"error":"main agent crashed"/);

  // A second /api/apply/failed must now return 409 not-applying — proves the
  // timeout was cleared (state.applying stayed false; if the timer had still
  // been live and re-armed somehow, this wouldn't be a meaningful check, but
  // here the only way to land on 409 is if the previous call cleared cleanly).
  const reFailed = await fetch(`http://127.0.0.1:${port}/api/apply/failed`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ error: 'still-no' }),
  });
  assert.equal(reFailed.status, 409);
  assert.deepEqual(await reFailed.json(), { error: 'not-applying' });

  await close();
});

// Plan Task 8 — multi-apply in one session must:
//   - increment applyCounter monotonically (response.applyIndex 1 → 2),
//   - write apply-1.json then apply-2.json,
//   - delete each apply-N.json on /api/apply/done,
//   - clear state.liveThreads (and state.agents) between rounds — F3 regression
//     for the leftover-liveThreads bug. (state.agents is private; we cover it
//     in a sibling test by asserting the cleared-thread id 404s on /messages.)
test('multi-apply: applyCounter increments to 2, apply-2.json written, no stale apply-1.json, liveThreads cleared between rounds', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  // Round 1: prime → apply → done.
  const boot1 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot1.blockIds[1]! }, message: 'note one', kind: 'note' }),
  });
  const applyA = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyA.status, 200);
  // applyCounter == 1 surfaced as response.applyIndex.
  assert.equal(((await applyA.json()) as { applyIndex: number }).applyIndex, 1);
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), true);

  writeFileSync(docPath, '# T\n\nP.\n\nMain edit 1.\n');
  const doneA = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(doneA.status, 200);
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), false);
  const monitoringA = await fetch(`http://127.0.0.1:${port}/api/apply/monitoring`, { method: 'POST' });
  assert.equal(monitoringA.status, 200);

  // liveThreads cleared after done (regression for F3 / F4).
  const afterA = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { threads: unknown[] };
  assert.equal(afterA.threads.length, 0);

  // Round 2: prime fresh → apply → done.
  const boot2 = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as { blockIds: string[] };
  await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot2.blockIds[1]! }, message: 'note two', kind: 'note' }),
  });
  const applyB = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(applyB.status, 200);
  // applyCounter == 2.
  assert.equal(((await applyB.json()) as { applyIndex: number }).applyIndex, 2);
  assert.equal(existsSync(join(sessionDir, 'apply-2.json')), true);
  // No stale apply-1.json resurrected.
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), false);

  writeFileSync(docPath, '# T\n\nP.\n\nMain edit 1.\n\nMain edit 2.\n');
  const doneB = await fetch(`http://127.0.0.1:${port}/api/apply/done`, { method: 'POST' });
  assert.equal(doneB.status, 200);
  assert.equal(existsSync(join(sessionDir, 'apply-2.json')), false);
  assert.equal(existsSync(join(sessionDir, 'apply-1.json')), false);

  await close();
});

// Plan Task 9 — when state.liveThreads is empty, /api/apply must short-circuit
// before flipping any state. No signal file written, applying stays false, so
// a subsequent apply (with threads) behaves normally.
test('POST /api/apply with empty liveThreads: 400 nothing-to-apply, no apply-N.json written, applying stays false', async () => {
  const { docPath, sessionDir, transcriptPath, prefsPath } = scratchSession('# T\n\nP.\n');
  const { port, close } = await createServer({
    docPath, sessionDir, mainJsonlPath: transcriptPath, prefsPath,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const r = await fetch(`http://127.0.0.1:${port}/api/apply`, { method: 'POST' });
  assert.equal(r.status, 400);
  assert.deepEqual(await r.json(), { error: 'nothing-to-apply' });

  // No apply-N.json on disk for any plausible N.
  for (let i = 1; i <= 5; i += 1) {
    assert.equal(
      existsSync(join(sessionDir, `apply-${i}.json`)),
      false,
      `apply-${i}.json should not exist after a nothing-to-apply 400`,
    );
  }

  // applying remains false.
  const boot = (await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json()) as {
    applying: boolean; applyStatus: string | null;
  };
  assert.equal(boot.applying, false);
  assert.equal(boot.applyStatus, null);

  await close();
});
