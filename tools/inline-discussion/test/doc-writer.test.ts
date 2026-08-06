// test/doc-writer.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { appendThreadDetails, removeAllArchivedBlocks, removeArchivedBlock, removeThreadDetails } from '../src/doc-writer.ts';
import { parseArchivedThreads } from '../src/archive.ts';
import { parseDoc } from '../src/markdown.ts';

function makeDoc(body: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'ind-'));
  const path = join(dir, 'doc.md');
  writeFileSync(path, body);
  return path;
}

test('appendThreadDetails inserts a <details> block after the anchored block', () => {
  const md = '# Title\n\nFirst para.\n\nSecond para.\n';
  const path = makeDoc(md);
  const parsed = parseDoc(md);
  const firstParaId = parsed.blocks.find((b) => b.kind === 'paragraph')!.id;

  appendThreadDetails(path, {
    blockId: firstParaId,
    quote: 'First',
    transcript: [
      { role: 'user', text: 'why?', ts: '2026-04-19T10:00:00Z' },
      { role: 'assistant', text: 'because', ts: '2026-04-19T10:00:05Z' },
    ],
    conclusion: 'Resolved.',
    date: '2026-04-19',
  });

  const after = readFileSync(path, 'utf8');
  assert.match(after, /First para\./);
  assert.match(after, /<details><summary>💬 Thread on "First" — 2026-04-19<\/summary>/);
  // Messages are pre-rendered HTML with data-raw preserving the original markdown.
  assert.match(after, /<div class="archived-msg" data-role="user" data-raw="why\?">/);
  assert.match(after, /<div class="archived-msg" data-role="assistant" data-raw="because">/);
  assert.match(after, /<div class="archived-conclusion" data-raw="Resolved\.">/);
  // Rendered visible text is still present (inline markdown still parses).
  assert.match(after, /<strong>User:<\/strong> why\?/);
  assert.match(after, /<strong>Assistant:<\/strong> because/);
  assert.match(after, /<strong>Conclusion:<\/strong> Resolved\./);
  // Second para still follows (after the <details>).
  const detailsIdx = after.indexOf('</details>');
  assert.ok(after.indexOf('Second para.') > detailsIdx);
});

test('appendThreadDetails emits a single CommonMark HTML block (no blank lines inside <details>)', () => {
  // Regression guard: previously `<details><summary>…</summary>` alone became
  // its own html block and got DOMPurify-auto-closed, orphaning the transcript
  // as sibling DOM nodes outside the details.
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'assistant', text: '**YES**\n\nMultiple paragraphs.', ts: 'now' }],
    conclusion: '',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  const openIdx = after.indexOf('<details>');
  const closeIdx = after.indexOf('</details>');
  assert.ok(openIdx >= 0 && closeIdx > openIdx);
  const inside = after.slice(openIdx, closeIdx);
  assert.doesNotMatch(inside, /\n\s*\n/, 'no blank lines between <details> and </details>');
});

test('appendThreadDetails renders inline markdown inside message bodies', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'assistant', text: 'The answer is **YES**', ts: 'now' }],
    conclusion: 'Looks `good`',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /The answer is <strong>YES<\/strong>/);
  assert.match(after, /Looks <code>good<\/code>/);
});

test('appendThreadDetails uses "entire block" label when quote absent', () => {
  const md = 'Paragraph.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'x', ts: 'now' }],
    conclusion: 'done',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /💬 Thread on entire block — 2026-04-19/);
});

test('appendThreadDetails is atomic (no .tmp left over)', async () => {
  const md = 'Paragraph.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [],
    conclusion: 'x',
    date: '2026-04-19',
  });
  const { readdirSync } = await import('node:fs'); // F17: ESM, no require()
  const leftovers = readdirSync(join(path, '..')).filter((f) => f.endsWith('.tmp'));
  assert.equal(leftovers.length, 0);
});

test('appendThreadDetails preserves unsupported token kinds (raw HTML, link defs, comments)', () => {
  const md = [
    'Anchor paragraph.',
    '',
    '<!-- keep me -->',
    '',
    '[ref]: https://example.com',
    '',
    '<iframe src="x"></iframe>',
    '',
    'Trailing.',
  ].join('\n');
  const path = makeDoc(md);
  const id = parseDoc(md).blocks.find((b) => b.kind === 'paragraph' && b.markdown.includes('Anchor'))!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'x', ts: '2026-04-19T00:00:00Z' }],
    conclusion: 'ok',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /<!-- keep me -->/);
  assert.match(after, /\[ref\]: https:\/\/example\.com/);
  assert.match(after, /<iframe/);
  assert.match(after, /Trailing\./);
});

test('appendThreadDetails renders block markdown (headings, lists) in archived messages', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [
      {
        role: 'assistant',
        text: "Here's the plan:\n\n## Revised agenda\n\n- Point one\n- Point two",
        ts: 'now',
      },
    ],
    conclusion: 'Done.',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  // Block markdown must render — not leak as raw "##" / "-" characters.
  assert.match(after, /<h2>Revised agenda<\/h2>/);
  assert.match(after, /<ul>[\s\S]*<li>Point one<\/li>[\s\S]*<li>Point two<\/li>[\s\S]*<\/ul>/);
  // data-raw still carries the literal markdown for round-tripping on resume.
  assert.match(after, /data-raw="Here&#39;s the plan:&#10;&#10;## Revised agenda/);
});

test('appendThreadDetails leaves a blank line after </details> so the next paragraph parses as markdown', () => {
  // Previously the trailing-newline logic added at most a single newline, so
  // `</details>\nFrame…` got swallowed into the HTML block and its inline
  // markdown rendered as literal asterisks.
  const md = 'Anchor.\n\nFrame **bold** follow-up.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks.find((b) => b.markdown.startsWith('Anchor'))!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'q', ts: 'now' }],
    conclusion: 'ok',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /<\/details>\n\nFrame \*\*bold\*\* follow-up\./);
});

test('rewriteDoc heals a pre-existing </details>\\nText with no blank line', () => {
  const buggy = 'Anchor.\n\n<details><summary>💬 Thread — 2026-04-19</summary>\n<div class="archived-msg" data-role="user" data-raw="q"><strong>User:</strong> q</div>\n</details>\nFrame **bold** here.\n';
  const path = makeDoc(buggy);
  // parseDoc normalises the buggy gap, so the paragraph shows up as its own block.
  const id = parseDoc(buggy).blocks.find((b) => b.markdown.startsWith('Frame'))!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'x', ts: 'now' }],
    conclusion: 'done',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  // Rewriting repairs the existing buggy gap as a side effect.
  assert.match(after, /<\/details>\n\nFrame \*\*bold\*\* here\./);
});

test('appendThreadDetails falls back to a unique quote when a block id is stale', () => {
  const path = makeDoc('Anchor.\n\nOriginal paragraph.\n');
  const staleId = parseDoc(readFileSync(path, 'utf8')).blocks[1]!.id;
  writeFileSync(path, 'Anchor.\n\nUpdated paragraph keeps the Original phrase.\n');
  appendThreadDetails(path, {
    blockId: staleId,
    quote: 'Original phrase',
    kind: 'note',
    transcript: [{ role: 'user', text: 'note', ts: 'now' }],
    conclusion: 'note',
    date: '2026-04-19',
  });
  assert.match(readFileSync(path, 'utf8'), /<summary>📝 Note on "Original phrase"/);
});

test('appendThreadDetails round-trips newlines through data-raw', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [
      { role: 'assistant', text: 'para one\n\npara two\nline two', ts: 'now' },
    ],
    conclusion: 'first\nsecond\n\nthird',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  // data-raw encodes newlines as &#10;; the parser (getAttribute) decodes them back.
  assert.match(after, /data-raw="para one&#10;&#10;para two&#10;line two"/);
  assert.match(after, /data-raw="first&#10;second&#10;&#10;third"/);
});

test('appendThreadDetails writes a 📝 Note block for kind: note (no assistant/Conclusion lines)', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    kind: 'note',
    blockId: id,
    quote: 'Anchor',
    transcript: [{ role: 'user', text: 'standalone comment', ts: '2026-04-19T00:00:00Z' }],
    conclusion: 'standalone comment',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /<details><summary>📝 Note on "Anchor" — 2026-04-19<\/summary>/);
  assert.match(after, /<div class="archived-note" data-kind="note" data-raw="standalone comment">/);
  assert.doesNotMatch(after, /archived-msg/);
  assert.doesNotMatch(after, /archived-conclusion/);
});

test('appendThreadDetails persists the selected duplicate-quote occurrence', () => {
  const path = makeDoc('First DE655001\n\nSecond DE655001\n');
  appendThreadDetails(path, {
    blockId: parseDoc(readFileSync(path, 'utf8')).blocks[1]!.id,
    quote: 'DE655001',
    occurrence: 2,
    transcript: [{ role: 'user', text: 'Question', ts: 'now' }],
    conclusion: 'Done',
    date: '2026-04-19',
  });
  assert.match(readFileSync(path, 'utf8'), /<details data-occurrence="2">/);
});

test('removeThreadDetails removes the matching data-thread-id details block', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'delete me', ts: 'now' }],
    conclusion: 'done',
    date: '2026-04-19',
    threadId: 't-1',
  });
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'keep me', ts: 'now' }],
    conclusion: 'kept',
    date: '2026-04-19',
    threadId: 't-2',
  });

  const after = removeThreadDetails(readFileSync(path, 'utf8'), 't-1');
  assert.doesNotMatch(after, /data-thread-id="t-1"/);
  assert.doesNotMatch(after, /delete me/);
  assert.match(after, /data-thread-id="t-2"/);
  assert.match(after, /keep me/);
});

test('appendThreadDetails keeps <p> wrappers around paragraphs surrounding list/other blocks', () => {
  // Regression: a lazy-anchored `^<p>[\s\S]*?<\/p>$` strip used to mis-match the
  // entire string for inputs that both start and end with `<p>…</p>` but have
  // other blocks between — producing broken HTML with orphaned `<p>` tags.
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [
      {
        role: 'assistant',
        text: 'intro\n\n- bullet\n\nconclusion',
        ts: 'now',
      },
    ],
    conclusion: 'Done.',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  assert.match(after, /<p>intro<\/p>/);
  assert.match(after, /<p>conclusion<\/p>/);
  assert.match(after, /<ul>[\s\S]*<li>bullet<\/li>[\s\S]*<\/ul>/);
});

test('appendThreadDetails preserves blank lines inside fenced code blocks', () => {
  // Regression: a post-render `/\n\s*\n/g → \n` collapse used to silently eat
  // blank lines inside `<pre><code>` output, stripping meaningful spacing in
  // archived code snippets.
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [
      {
        role: 'assistant',
        text: '```\nline1\n\nline3\n```',
        ts: 'now',
      },
    ],
    conclusion: 'Done.',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  const preMatch = after.match(/<pre>[\s\S]*?<\/pre>/);
  assert.ok(preMatch, 'expected a <pre> block in output');
  // Two consecutive newline entities preserve the blank line between line1 and
  // line3 when the browser renders the entity-decoded content. (Syntax
  // highlighting may wrap portions of "line1"/"line3" in <span> elements, so
  // match the `&#10;&#10;` pair directly.)
  assert.match(preMatch![0], /&#10;&#10;/);
  // And the original newlines inside <pre> are replaced by entities, so no
  // literal newline remains to break the CommonMark HTML block.
  assert.doesNotMatch(preMatch![0], /\n/);
  // No source-level blank line remains inside the <details> block (would
  // otherwise terminate the surrounding CommonMark HTML block).
  const openIdx = after.indexOf('<details>');
  const closeIdx = after.indexOf('</details>');
  const inside = after.slice(openIdx, closeIdx);
  assert.doesNotMatch(inside, /\n\s*\n/);
});

test('appendThreadDetails escapes user/assistant text so </details> cannot break out', () => {
  const md = 'Anchor.\n';
  const path = makeDoc(md);
  const id = parseDoc(md).blocks[0]!.id;
  appendThreadDetails(path, {
    blockId: id,
    transcript: [{ role: 'user', text: 'evil </details><script>alert(1)</script>', ts: 'now' }],
    conclusion: 'malicious </details> break',
    date: '2026-04-19',
  });
  const after = readFileSync(path, 'utf8');
  // The payload's literal </details> must NOT appear inside the archived block —
  // it has been HTML-encoded so marked / DOMPurify see plain text.
  const detailsStart = after.indexOf('<details>');
  const detailsEnd = after.indexOf('</details>');
  const body = after.slice(detailsStart, detailsEnd + '</details>'.length);
  const innerStart = body.indexOf('<summary>');
  const innerEnd = body.lastIndexOf('</details>');
  const inner = body.slice(innerStart, innerEnd);
  assert.doesNotMatch(inner, /<\/details>/);
  assert.doesNotMatch(inner, /<script/);
  assert.match(inner, /&lt;\/details&gt;/);
});

test('removeArchivedBlock handles </details>\\nText with no blank-line gap (normalization-triggering source)', () => {
  // Regression guard: parseDoc / findArchivedBlocks normalize `</details>\n` → `\n\n`
  // before lexing, so the block.markdown tokens they return include the inserted
  // blank line. Searching those tokens in the un-normalized source made indexOf
  // return -1, which surfaced as a 500 "archived thread #N not found" on delete.
  const details =
    '<details data-thread-id="t1"><summary>💬 Thread on "anchor" — 2026-04-23</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q"><strong>User:</strong> q</div>\n' +
    '<div class="archived-conclusion" data-raw="c"><strong>Conclusion:</strong> c</div>\n' +
    '</details>';
  // NOTE: single newline after </details> immediately followed by "Tail" — the
  // exact case normalizeDetailsSpacing repairs to `\n\n`.
  const source = `Anchor paragraph.\n\n${details}\nTail paragraph.\n`;

  const after = removeArchivedBlock(source, 1);
  assert.doesNotMatch(after, /data-thread-id="t1"/);
  assert.match(after, /Anchor paragraph\./);
  assert.match(after, /Tail paragraph\./);
});

test('removeArchivedBlock targets the same N-th block that parseArchivedThreads labels archived-N', () => {
  // Regression guard for a counter drift between `findArchivedBlockRange`
  // (used by deletion) and `parseArchivedThreads` (drives the client's
  // `archived-N` ids). The parser skips `<details>` blocks that have no
  // preceding content block (no anchor available) — the finder used to count
  // them anyway, so the N-th archive per the client referred to a different
  // `<details>` in the source.
  const orphanDetails =
    '<details data-thread-id="orphan"><summary>💬 Thread on "nope" — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="q"><strong>User:</strong> q</div>\n' +
    '<div class="archived-conclusion" data-raw="c"><strong>Conclusion:</strong> c</div>\n' +
    '</details>';
  const anchoredDetails =
    '<details data-thread-id="anchored"><summary>💬 Thread on "real" — 2026-04-20</summary>\n' +
    '<div class="archived-msg" data-role="user" data-raw="qq"><strong>User:</strong> qq</div>\n' +
    '<div class="archived-conclusion" data-raw="cc"><strong>Conclusion:</strong> cc</div>\n' +
    '</details>';
  const source = `${orphanDetails}\n\nAnchor paragraph.\n\n${anchoredDetails}\n`;

  // Sanity: only the anchored block is parsed as an archive, so it is archived-1.
  const parsed = parseArchivedThreads(source);
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0]!.id, 'archived-1');
  assert.equal(parsed[0]!.conclusion, 'cc');

  // Deleting archive index 1 must remove the anchored block, not the orphan.
  const after = removeArchivedBlock(source, 1);
  assert.match(after, /data-thread-id="orphan"/);
  assert.doesNotMatch(after, /data-thread-id="anchored"/);
});

test('removeAllArchivedBlocks strips every thread/note block but keeps prose', () => {
  const md = '# Title\n\nFirst para.\n\nSecond para.\n';
  const path = makeDoc(md);
  const parsed = parseDoc(md);
  const paras = parsed.blocks.filter((b) => b.kind === 'paragraph');

  appendThreadDetails(path, {
    blockId: paras[0]!.id, quote: 'First',
    transcript: [{ role: 'user', text: 'q', ts: '2026-04-19T10:00:00Z' }],
    conclusion: 'Resolved.', date: '2026-04-19', threadId: 't-1',
  });
  appendThreadDetails(path, {
    kind: 'note', blockId: paras[1]!.id, quote: 'Second',
    transcript: [], conclusion: 'A note.', date: '2026-04-19', threadId: 't-2',
  });

  // Sanity: two archives present before the wipe.
  assert.equal(parseArchivedThreads(readFileSync(path, 'utf8')).length, 2);

  removeAllArchivedBlocks(path);

  const after = readFileSync(path, 'utf8');
  assert.doesNotMatch(after, /<details/);
  assert.equal(parseArchivedThreads(after).length, 0);
  // Prose survives.
  assert.match(after, /First para\./);
  assert.match(after, /Second para\./);
});

test('removeAllArchivedBlocks rewrites a doc with no archives unchanged in content', () => {
  const md = '# Title\n\nJust prose, no threads.\n';
  const path = makeDoc(md);
  removeAllArchivedBlocks(path);
  const after = readFileSync(path, 'utf8');
  assert.match(after, /Just prose, no threads\./);
  assert.equal(parseArchivedThreads(after).length, 0);
});
