import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { replaceThreadDetails } from '../src/doc-writer.ts';

test('replaceThreadDetails updates a quoted-anchor archive with occurrence metadata', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'inline-discussion-conclusion-'));
  const docPath = join(directory, 'doc.md');
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  writeFileSync(docPath, [
    'Anchor paragraph.',
    '',
    '<details data-thread-id="t-1" data-occurrence="1"><summary>Thread</summary>',
    '<div class="archived-conclusion" data-raw="first pass">first pass</div>',
    '</details>',
    '',
  ].join('\n'));

  replaceThreadDetails(docPath, {
    blockId: 'anchor-block',
    quote: 'Anchor paragraph.',
    occurrence: 1,
    transcript: [{ role: 'user', text: 'question', ts: '2026-08-10T00:00:00.000Z' }],
    conclusion: 'revised wording',
    date: '2026-08-10',
    threadId: 't-1',
  });

  const updated = readFileSync(docPath, 'utf8');
  assert.match(updated, /<details data-thread-id="t-1" data-occurrence="1">/);
  assert.match(updated, /data-raw="revised wording"/);
  assert.doesNotMatch(updated, /data-raw="first pass"/);
});
