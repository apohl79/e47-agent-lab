// test/archive.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseArchivedThreads } from '../src/archive.ts';

test('parseArchivedThreads picks up a single details block', () => {
  const md = [
    '# Doc',
    '',
    'Anchor paragraph with Option A in it.',
    '',
    '<details><summary>💬 Thread on "Option A" — 2026-04-10</summary>',
    '',
    '**User:** why?',
    '',
    '**Claude:** because',
    '',
    '**Conclusion:** resolved ok',
    '',
    '</details>',
    '',
    'Another paragraph.',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 1);
  const t = threads[0]!;
  assert.equal(t.status, 'archived');
  assert.equal(t.anchor.quote, 'Option A');
  assert.equal(t.conclusion, 'resolved ok');
  assert.equal(t.messages.length, 2);
  assert.equal(t.messages[0]!.role, 'user');
  assert.equal(t.messages[1]!.role, 'assistant');
});

test('parseArchivedThreads handles entire-block anchor', () => {
  const md = [
    'Anchor.',
    '',
    '<details><summary>💬 Thread on entire block — 2026-04-10</summary>',
    '',
    '**User:** x',
    '',
    '**Claude:** y',
    '',
    '**Conclusion:** ok',
    '',
    '</details>',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 1);
  assert.equal(threads[0]!.anchor.quote, undefined);
});

test('parseArchivedThreads skips malformed blocks', () => {
  const md = [
    'Anchor.',
    '',
    '<details><summary>💬 Thread on "X" — 2026-04-10</summary>',
    '(missing user/claude/conclusion headers)',
    '</details>',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 0);
});

test('parseArchivedThreads preserves all messages in a multi-turn thread', () => {
  // Matches what doc-writer.formatDetails produces for a 4-message transcript.
  const md = [
    'Anchor paragraph.',
    '',
    '<details><summary>💬 Thread on "Anchor" — 2026-04-10</summary>',
    '',
    '**User:** first question',
    '',
    '**Claude:** first answer',
    '',
    '**User:** follow-up',
    '',
    '**Claude:** second answer',
    '',
    '**Conclusion:** wrapped up',
    '',
    '</details>',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 1);
  const t = threads[0]!;
  assert.equal(t.messages.length, 4);
  assert.deepEqual(
    t.messages.map((m) => [m.role, m.text]),
    [
      ['user', 'first question'],
      ['assistant', 'first answer'],
      ['user', 'follow-up'],
      ['assistant', 'second answer'],
    ],
  );
  assert.equal(t.conclusion, 'wrapped up');
});

test('parseArchivedThreads picks up 📝 Note blocks (kind=note, body as conclusion)', () => {
  const md = [
    'Anchor paragraph.',
    '',
    '<details><summary>📝 Note on "Anchor" — 2026-04-20</summary>',
    '',
    'standalone remark',
    '',
    '</details>',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 1);
  const t = threads[0]!;
  assert.equal(t.kind, 'note');
  assert.equal(t.anchor.quote, 'Anchor');
  assert.equal(t.conclusion, 'standalone remark');
  assert.equal(t.messages.length, 1);
  assert.equal(t.messages[0]!.role, 'user');
  assert.equal(t.messages[0]!.text, 'standalone remark');
});

test('parseArchivedThreads preserves a duplicate-quote occurrence from details metadata', () => {
  const markdown = [
    '## Items',
    '',
    '<details data-occurrence="3"><summary>💬 Thread on "DE655001" — 2026-04-19</summary>',
    '<div class="archived-msg" data-role="user" data-raw="Question">Question</div>',
    '<div class="archived-conclusion" data-raw="Done"><strong>Conclusion:</strong> Done</div>',
    '</details>',
  ].join('\n');

  const [thread] = parseArchivedThreads(markdown);
  assert.equal(thread?.anchor.occurrence, 3);
});

test('parseArchivedThreads round-trips multi-paragraph messages via <br>', () => {
  const md = [
    'Anchor paragraph with Option A in it.',
    '',
    '<details><summary>💬 Thread on "Option A" — 2026-04-10</summary>',
    '',
    '**User:** why?',
    '',
    '**Claude:** line one<br><br>line two<br>line three',
    '',
    '**Conclusion:** first line<br>second line',
    '',
    '</details>',
  ].join('\n');
  const threads = parseArchivedThreads(md);
  assert.equal(threads.length, 1);
  const t = threads[0]!;
  assert.equal(t.messages[1]!.text, 'line one\n\nline two\nline three');
  assert.equal(t.conclusion, 'first line\nsecond line');
});
