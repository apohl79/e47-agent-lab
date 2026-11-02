import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import { mockAgentFactory } from '../src/agent.ts';

test('GET Markdown path exposes line and character range navigation metadata', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-markdown-range-'));
  mkdirSync(join(root, '.git'));
  const docPath = join(root, 'doc.md');
  const markdownPath = join(root, 'docs', 'guide.md');
  mkdirSync(join(root, 'docs'));
  writeFileSync(docPath, '# Discussion\n');
  writeFileSync(markdownPath, '# Guide\n\nAlpha beta gamma.\nSecond line.\n');
  const staticDir = join(root, 'static');
  mkdirSync(staticDir);
  writeFileSync(join(staticDir, 'index.html'), '<script src="/app.js"></script>');
  const { port, close } = await createServer({
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    staticDir,
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const page = await fetch(`http://127.0.0.1:${port}/docs/guide.md:3:7-3:10`);
  const boot = await (await fetch(
    `http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent('/docs/guide.md:3:7-3:10')}`,
  )).json() as {
    html: string;
    targetLine: number;
    targetRange: { startLine: number; startColumn: number; endLine: number; endColumn: number };
    targetText: string;
  };
  assert.equal(page.status, 200);
  assert.equal(boot.targetLine, 3);
  assert.deepEqual(boot.targetRange, { startLine: 3, startColumn: 7, endLine: 3, endColumn: 10 });
  assert.equal(boot.targetText, 'beta');
  assert.match(boot.html, /data-source-start-line="3"[^>]*data-source-end-line="4"/);
  await close();
});

test('document-relative links resolve beside a nested discussion document', async () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-relative-link-'));
  mkdirSync(join(root, '.git'));
  const reportDir = join(root, 'reports');
  mkdirSync(reportDir);
  const docPath = join(reportDir, 'review.md');
  const evidencePath = join(reportDir, 'evidence.md');
  const attachmentPath = join(reportDir, 'evidence.txt');
  writeFileSync(docPath, '# Review\n\n[Evidence](./evidence.md:3)\n\n[Attachment](./evidence.txt)\n');
  writeFileSync(evidencePath, '# Evidence\n\nLocal fact.\n');
  writeFileSync(join(root, 'evidence.md'), '# Root shadow\n\nWrong fact.\n');
  writeFileSync(attachmentPath, 'portable attachment\n');
  const { port, close } = await createServer({
    docPath,
    projectRoot: root,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
    agentFactory: mockAgentFactory({ reply: 'r', conclusion: 'c' }),
    shutdownOnFinish: false,
  });

  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as { html: string };
  assert.match(boot.html, new RegExp(`href="${evidencePath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:3"`));
  assert.match(boot.html, new RegExp(`href="${attachmentPath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`));
  const linkedBoot = await (await fetch(
    `http://127.0.0.1:${port}/api/bootstrap?path=${encodeURIComponent(`${evidencePath}:3`)}`,
  )).json() as { title: string; targetLine: number; targetText: string };
  const attachment = await fetch(`http://127.0.0.1:${port}${attachmentPath}`);
  assert.deepEqual(
    { title: linkedBoot.title, targetLine: linkedBoot.targetLine, targetText: linkedBoot.targetText },
    { title: 'evidence.md', targetLine: 3, targetText: 'Local fact.' },
  );
  assert.equal(await attachment.text(), 'portable attachment\n');
  await close();
});
