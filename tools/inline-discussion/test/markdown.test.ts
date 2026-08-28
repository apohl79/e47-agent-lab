// test/markdown.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseDoc, renderDoc, updateTaskCheckboxState } from '../src/markdown.ts';

test('parseDoc assigns stable block IDs per top-level block', () => {
  const md = `# Title\n\nPara one.\n\n\`\`\`js\nconsole.log(1);\n\`\`\`\n`;
  const result = parseDoc(md);
  assert.equal(result.blocks.length, 3);
  assert.equal(result.blocks[0]!.kind, 'heading');
  assert.equal(result.blocks[1]!.kind, 'paragraph');
  assert.equal(result.blocks[2]!.kind, 'code');
  assert.equal(result.blocks.every((block) => /^[0-9a-f]{10}$/.test(block.id)), true);
});

test('parseDoc block IDs are stable for unchanged content', () => {
  const md = '# Same\n\nSame paragraph.\n';
  const a = parseDoc(md);
  const b = parseDoc(md);
  assert.deepEqual(
    a.blocks.map((x) => x.id),
    b.blocks.map((x) => x.id),
  );
});

test('parseDoc suffixes colliding IDs deterministically', () => {
  const md = 'Same line.\n\nSame line.\n';
  const r = parseDoc(md);
  assert.equal(r.blocks.length, 2);
  assert.notEqual(r.blocks[0]!.id, r.blocks[1]!.id);
  assert.ok(r.blocks[1]!.id.endsWith('-2'));
});

test('parseDoc keeps <details> blocks as html kind (round-trip support)', () => {
  const md = 'Anchor.\n\n<details><summary>💬 Thread on "x" — 2026-04-10</summary>\n\n**User:** q\n\n**Claude:** a\n\n**Conclusion:** ok\n\n</details>\n\nAfter.\n';
  const r = parseDoc(md);
  const kinds = r.blocks.map((b) => b.kind);
  assert.ok(kinds.includes('html'));
  const detailsBlock = r.blocks.find((b) => b.kind === 'html')!;
  assert.match(detailsBlock.markdown, /<details>/);
});

test('renderDoc preserves soft-wrapped lines and blank-line block breaks', () => {
  const md = 'First source-wrapped line\nsecond source-wrapped line\n\nNew paragraph.\n';
  const { html } = renderDoc(md);
  assert.doesNotMatch(html, /<br\s*\/?>/);
  assert.match(html, /First source-wrapped line\s+second source-wrapped line/);
  assert.match(html, /<\/p>\s*<p[^>]*>New paragraph\.<\/p>/);
});

test('renderDoc rewrites relative image URLs against the document path', () => {
  const md = '![Local diagram](./diagrams/chart.svg)\n\n![Remote diagram](https://example.com/chart.svg)\n';
  const { html } = renderDoc(md, '/tmp/docs/decision.md');
  assert.match(html, /src="\/api\/assets\?documentPath=%2Ftmp%2Fdocs%2Fdecision\.md&amp;asset=\.%2Fdiagrams%2Fchart\.svg"/);
  assert.match(html, /src="https:\/\/example\.com\/chart\.svg"/);
});

test('renderDoc rewrites relative file links against the document path', () => {
  const md = '[Sibling](./evidence/other.md:40-45#decision)\n\n'
    + '[Parent](../src/service.ts:10:3-10:9)\n\n'
    + '[Root](/docs/root.md:2)\n\n'
    + '[Remote](https://example.com/doc.md:2)\n';
  const { html } = renderDoc(md, '/tmp/project/docs/review.md');
  assert.match(html, /href="\/tmp\/project\/docs\/evidence\/other\.md:40-45#decision"/);
  assert.match(html, /href="\/tmp\/project\/src\/service\.ts:10:3-10:9"/);
  assert.match(html, /href="\/docs\/root\.md:2"/);
  assert.match(html, /href="https:\/\/example\.com\/doc\.md:2"/);
});

test('renderDoc normalizes Slack message links to supported channel URIs while stripping unsafe protocols', () => {
  const { html } = renderDoc(
    '[Slack](slack://channel?team=T012TSW2ML3&id=C0AJ32EB8G3&message=1787836685.584729) [Unsafe](javascript:alert(1))',
  );

  assert.match(html, /href="slack:\/\/channel\?team=T012TSW2ML3&amp;id=C0AJ32EB8G3"/);
  assert.doesNotMatch(html, /message=1787836685\.584729/);
  assert.doesNotMatch(html, /href="javascript:/i);
});

test('renderDoc emits data-block-id on every top-level block', () => {
  const md = '# Title\n\nPara.\n';
  const { html, blocks } = renderDoc(md);
  assert.match(html, new RegExp(`data-block-id="${blocks[0]!.id}"`));
  assert.match(html, new RegExp(`data-block-id="${blocks[1]!.id}"`));
});

test('renderDoc emits source line spans on rendered Markdown blocks', () => {
  const { html, blocks } = renderDoc('# Title\n\nParagraph\nwrapped.\n\n- first\n- second\n');
  assert.deepEqual(
    blocks.map(({ sourceStartLine, sourceEndLine }) => ({ sourceStartLine, sourceEndLine })),
    [
      { sourceStartLine: 1, sourceEndLine: 1 },
      { sourceStartLine: 3, sourceEndLine: 4 },
      { sourceStartLine: 6, sourceEndLine: 7 },
    ],
  );
  assert.match(html, /data-source-start-line="3"[^>]*data-source-end-line="4"/);
});

test('parseDoc source spans retain on-disk lines when details spacing is normalized', () => {
  const blocks = parseDoc('Anchor.\n\n<details>\nBody\n</details>\nAfter.\n').blocks;
  assert.equal(blocks.at(-1)?.markdown.trim(), 'After.');
  assert.equal(blocks.at(-1)?.sourceStartLine, 6);
  assert.equal(blocks.at(-1)?.sourceEndLine, 6);
});

test('renderDoc emits stable section-link ids for headings', () => {
  const { html } = renderDoc('# 2026-02 Dining\n\n# 2026-02 Dining\n');
  assert.match(html, /<h1[^>]*id="2026-02-dining"/);
  assert.match(html, /<h1[^>]*id="2026-02-dining-1"/);
});

test('renderDoc sanitises scripts but preserves data-block-id', () => {
  const md = 'Hello <script>alert(1)</script> world';
  const { html } = renderDoc(md);
  assert.doesNotMatch(html, /<script>/i);
  assert.match(html, /data-block-id="[0-9a-f]{10}"/);
});

test('renderDoc applies syntax highlighting class to code blocks', () => {
  const md = '```js\nconsole.log(1);\n```\n';
  const { html } = renderDoc(md);
  assert.match(html, /class="[^"]*hljs[^"]*language-js/);
});

test('renderDoc marks Mermaid fences for browser-side rendering', () => {
  const { html } = renderDoc('```mermaid\nflowchart LR\n  A --> B\n```\n');
  assert.match(html, /<pre class="mermaid"[^>]*>flowchart LR/);
  assert.doesNotMatch(html, /language-mermaid/);
});

test('renderDoc preserves <details> and <summary> through sanitisation', () => {
  const md = 'Anchor.\n\n<details><summary>Summary text</summary>\n\nBody.\n\n</details>\n';
  const { html } = renderDoc(md);
  assert.match(html, /<details[^>]*>/);
  assert.match(html, /<summary[^>]*>Summary text<\/summary>/);
});

test('renderDoc adds data-block-id to elements even when first tag has attributes', () => {
  const md = '| a | b |\n| - | - |\n| 1 | 2 |\n';
  const { html } = renderDoc(md);
  assert.match(html, /<table[^>]*data-block-id="[0-9a-f]{10}"/);
});

test('renderDoc preserves checked task-list state and strikethrough markdown', () => {
  const md = '- [ ] Open item\n- [x] ~~Done item~~\n';
  const { html } = renderDoc(md);
  assert.match(html, /<input[^>]*type="checkbox"[^>]*>/);
  assert.match(html, /<input[^>]*checked[^>]*>/);
  assert.match(html, /data-task-checkbox-index="0"/);
  assert.match(html, /data-task-checkbox-index="1"/);
  assert.match(html, /<del>Done item<\/del>/);
});

test('updateTaskCheckboxState changes only the selected Markdown task marker', () => {
  const markdown = '- [ ] First\n  - [x] Nested\n- [ ] Last\n';
  const blockId = parseDoc(markdown).blocks[0]!.id;

  assert.equal(
    updateTaskCheckboxState(markdown, blockId, 1, false),
    '- [ ] First\n  - [ ] Nested\n- [ ] Last\n',
  );
  assert.equal(updateTaskCheckboxState(markdown, blockId, 3, true), null);
});

test('renderDoc converts inline line-through styles to semantic del tags', () => {
  const md = '<span style="text-decoration: line-through;">Done item</span>\n';
  const { html } = renderDoc(md);
  assert.match(html, /<del>Done item<\/del>/);
  assert.doesNotMatch(html, /style=/);
});
