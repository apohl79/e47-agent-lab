// src/markdown.ts
import { marked, type Tokens, type TokensList } from 'marked';
import { createHash } from 'node:crypto';
import { dirname, extname, resolve } from 'node:path';
import type { Block, BlockKind } from './types.ts';
import hljs from 'highlight.js';
import DOMPurify from 'isomorphic-dompurify';
import { JSDOM } from 'jsdom';
import { escapeHtml } from './html.ts';
import { formatSourceRange, parseSourceReference } from './source-reference.ts';
import { ALLOWED_URI_REGEXP } from './uri-policy.ts';

const KIND_MAP: Record<string, BlockKind | undefined> = {
  heading: 'heading',
  paragraph: 'paragraph',
  code: 'code',
  blockquote: 'blockquote',
  list: 'list',
  table: 'table',
  hr: 'hr',
  html: 'html', // F3: round-trip <details> and other raw HTML blocks
};

function normaliseText(token: Tokens.Generic): string {
  const raw = (token.raw ?? '').trim();
  const normalizedCheckboxes = raw.replace(/\[[xX]\]/g, '[ ]');
  return normalizedCheckboxes.replace(/\s+/g, ' ');
}

function hashKey(kind: BlockKind, normalised: string, lang?: string): string {
  const input = lang ? `${kind}|${lang}|${normalised}` : `${kind}|${normalised}`;
  return createHash('sha1').update(input).digest('hex').slice(0, 10);
}

export interface RawParsedDoc {
  blocks: Block[];
  blockIds: string[];
  links: Record<string, { href: string; title?: string | null | undefined }>;
}

/**
 * Ensure every `</details>` is followed by a blank line. Idempotent — if the
 * doc already has `</details>\n\n…`, this is a no-op. The regex matches
 * `</details>\n` followed by any non-newline character, i.e. the buggy case
 * where the next line starts immediately after a single newline.
 *
 * CommonMark HTML block type 6 ends at a blank line, so a paragraph that sits
 * directly after `</details>` with only a single `\n` gets swallowed into the
 * HTML block and its inline markdown is rendered literally. Callers apply this
 * before lexing (parseDoc) or before persisting (rewriteDoc) so both the
 * render path and the on-disk doc converge on the same invariant.
 */
export function normalizeDetailsSpacing(md: string): string {
  return md.replace(/<\/details>\n(?=[^\n])/g, '</details>\n\n');
}

export function parseDoc(markdown: string): RawParsedDoc {
  const normalizedMarkdown = normalizeDetailsSpacing(markdown);
  const insertedNewlineOffsets = detailsSpacingInsertions(markdown);
  const tokens = marked.lexer(normalizedMarkdown) as TokensList;
  const links = tokens.links ?? {};
  const usedIds = new Map<string, number>();
  const blocks: Block[] = [];
  let sourceOffset = 0;

  for (const token of tokens) {
    const raw = token.raw ?? '';
    const locatedOffset = raw ? normalizedMarkdown.indexOf(raw, sourceOffset) : sourceOffset;
    const tokenOffset = locatedOffset >= 0 ? locatedOffset : sourceOffset;
    const sourceStartLine = originalSourceLine(normalizedMarkdown, insertedNewlineOffsets, tokenOffset);
    const contentLength = raw.replace(/(?:\r?\n)+$/, '').length;
    const sourceEndLine = originalSourceLine(
      normalizedMarkdown,
      insertedNewlineOffsets,
      tokenOffset + contentLength,
    );
    sourceOffset = tokenOffset + raw.length;
    const kind = KIND_MAP[token.type];
    if (!kind) continue;
    const lang = kind === 'code' ? (token as Tokens.Code).lang ?? '' : undefined;
    const base = hashKey(kind, normaliseText(token), lang);
    const count = (usedIds.get(base) ?? 0) + 1;
    usedIds.set(base, count);
    const id = count === 1 ? base : `${base}-${count}`;

    // Build a proper TokensList (single-element) so marked.parser has .links (F1).
    const singleton = [token] as unknown as TokensList;
    (singleton as unknown as { links: typeof links }).links = links;

    blocks.push({
      id,
      kind,
      markdown: raw,
      html: marked.parser(singleton),
      sourceStartLine,
      sourceEndLine,
    });
  }

  return { blocks, blockIds: blocks.map((b) => b.id), links };
}

const TASK_CHECKBOX_PATTERN = /^((?:\s*>\s*)*\s*(?:[-+*]|\d+[.)])\s+\[)([ xX])\]/gm;

export function updateTaskCheckboxState(
  markdown: string,
  blockId: string,
  checkboxIndex: number,
  checked: boolean,
): string | null {
  const block = parseDoc(markdown).blocks.find((candidate) => candidate.id === blockId);
  if (
    !block
    || (block.kind !== 'list' && block.kind !== 'blockquote')
    || block.sourceStartLine === undefined
    || block.sourceEndLine === undefined
  ) return null;
  const start = sourceLineOffset(markdown, block.sourceStartLine);
  const end = sourceLineOffset(markdown, block.sourceEndLine + 1);
  const source = markdown.slice(start, end);
  const match = [...source.matchAll(TASK_CHECKBOX_PATTERN)][checkboxIndex];
  if (!match || match.index === undefined) return null;
  const stateOffset = start + match.index + match[1]!.length;
  return markdown.slice(0, stateOffset) + (checked ? 'x' : ' ') + markdown.slice(stateOffset + 1);
}

function sourceLineOffset(markdown: string, line: number): number {
  return markdown.split(/(?<=\n)/).slice(0, Math.max(0, line - 1)).join('').length;
}

function detailsSpacingInsertions(markdown: string): number[] {
  const offsets: number[] = [];
  const pattern = /<\/details>\n(?=[^\n])/g;
  let normalizedOffsetShift = 0;
  for (const match of markdown.matchAll(pattern)) {
    offsets.push((match.index ?? 0) + match[0].length + normalizedOffsetShift);
    normalizedOffsetShift += 1;
  }
  return offsets;
}

function originalSourceLine(normalizedMarkdown: string, insertedOffsets: number[], offset: number): number {
  const insertedBeforeOffset = insertedOffsets.filter((insertedOffset) => insertedOffset < offset).length;
  return 1 + newlineCount(normalizedMarkdown.slice(0, offset)) - insertedBeforeOffset;
}

function newlineCount(text: string): number {
  return text.match(/\n/g)?.length ?? 0;
}

export function renderedMarkdownText(markdown: string): string {
  const dom = new JSDOM(`<body>${marked.parse(markdown) as string}</body>`);
  return (dom.window.document.body.textContent ?? '').trim();
}

const renderer = new marked.Renderer();
renderer.code = function (code: string, infostring: string | undefined, _escaped: boolean): string {
  const lang = (infostring ?? '').trim().split(/\s+/)[0] || undefined;
  if (lang?.toLowerCase() === 'mermaid') {
    return `<pre class="mermaid">${escapeHtml(code)}</pre>\n`;
  }
  const language = lang && hljs.getLanguage(lang) ? lang : undefined;
  const highlighted = language
    ? hljs.highlight(code, { language }).value
    : hljs.highlightAuto(code).value;
  const cls = language ? `hljs language-${language}` : 'hljs';
  return `<pre><code class="${cls}">${highlighted}</code></pre>\n`;
};
// Keep CommonMark soft-break semantics: single newlines inside a paragraph
// remain whitespace, while blank lines continue to separate blocks.
marked.use({ renderer, gfm: true, breaks: false });

const SANITIZE_OPTS = {
  ADD_TAGS: ['details', 'summary', 'del', 's', 'strike'],
  ADD_ATTR: ['data-block-id', 'data-thread-id', 'data-source-start-line', 'data-source-end-line', 'data-task-checkbox-index', 'checked', 'disabled', 'type'],
  ALLOWED_URI_REGEXP,
};

export interface RenderedDoc extends RawParsedDoc {
  html: string;
}

const SOURCE_LANGUAGE_BY_EXTENSION: Record<string, string> = {
  '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
  '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
  '.json': 'json', '.jsonc': 'json', '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'ini',
  '.html': 'xml', '.htm': 'xml', '.xml': 'xml', '.svg': 'xml',
  '.css': 'css', '.scss': 'scss', '.less': 'less',
  '.py': 'python', '.go': 'go', '.rs': 'rust', '.java': 'java', '.kt': 'kotlin',
  '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
  '.cs': 'csharp', '.php': 'php', '.rb': 'ruby', '.swift': 'swift',
  '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.fish': 'bash', '.ps1': 'powershell',
  '.sql': 'sql', '.graphql': 'graphql', '.gql': 'graphql', '.proto': 'protobuf',
};

const MARKDOWN_EXTENSIONS = new Set(['.md', '.markdown']);

export function isMarkdownFile(path: string): boolean {
  return MARKDOWN_EXTENSIONS.has(extname(path).toLowerCase());
}

export function isSourceFile(path: string): boolean {
  return SOURCE_LANGUAGE_BY_EXTENSION[extname(path).toLowerCase()] !== undefined;
}

export function renderSourceFile(source: string, path: string): RenderedDoc {
  const language = SOURCE_LANGUAGE_BY_EXTENSION[extname(path).toLowerCase()];
  const lines = source.split(/\r?\n/);
  const blocks = lines.map((line, index) => {
    const lineNumber = index + 1;
    const highlighted = language && hljs.getLanguage(language)
      ? hljs.highlight(line, { language }).value
      : hljs.highlightAuto(line).value;
    const id = `line-${lineNumber}`;
    return {
      id,
      kind: 'code' as const,
      markdown: line,
      sourceStartLine: lineNumber,
      sourceEndLine: lineNumber,
      html: `<pre class="source-line" data-block-id="${id}"><code class="hljs language-${language ?? 'plaintext'}"><span class="source-line-number">${lineNumber}</span><span class="source-line-code">${highlighted || ' '}</span></code></pre>`,
    };
  });
  return { blocks, blockIds: blocks.map((block) => block.id), links: {}, html: blocks.map((block) => block.html).join('\n') };
}

export function renderDoc(markdown: string, documentPath?: string): RenderedDoc {
  const parsed = parseDoc(markdown);
  const headingIds = new Map<string, number>();
  const pieces = parsed.blocks.map((block) => {
    const clean = DOMPurify.sanitize(preserveInlineSemantics(block.html), SANITIZE_OPTS);
    const withDocumentAssets = rewriteDocumentUrls(clean, documentPath);
    return injectBlockIdViaDom(withDocumentAssets, block, headingIds);
  });
  return { ...parsed, html: pieces.join('\n') };
}

function rewriteDocumentUrls(fragmentHtml: string, documentPath?: string): string {
  const dom = new JSDOM(`<div id="root">${fragmentHtml}</div>`);
  const root = dom.window.document.getElementById('root')!;
  for (const image of Array.from(root.querySelectorAll<HTMLImageElement>('img[src]'))) {
    const source = image.getAttribute('src')?.trim();
    if (!source || !documentPath || !isRelativeUrl(source)) continue;
    const hash = source.indexOf('#');
    const withoutHash = hash >= 0 ? source.slice(0, hash) : source;
    const query = withoutHash.indexOf('?');
    const assetPath = query >= 0 ? withoutHash.slice(0, query) : withoutHash;
    const fragment = hash >= 0 ? source.slice(hash) : '';
    image.setAttribute(
      'src',
      `/api/assets?documentPath=${encodeURIComponent(documentPath)}&asset=${encodeURIComponent(assetPath)}${fragment}`,
    );
  }
  for (const anchor of Array.from(root.querySelectorAll<HTMLAnchorElement>('a[href]'))) {
    const target = anchor.getAttribute('href')?.trim();
    if (!target) continue;
    if (!documentPath || !isRelativeUrl(target)) continue;
    anchor.setAttribute('href', resolveRelativeLinkTarget(target, documentPath));
  }
  return root.innerHTML;
}

function resolveRelativeLinkTarget(target: string, documentPath: string): string {
  const hashIndex = target.indexOf('#');
  const beforeHash = hashIndex >= 0 ? target.slice(0, hashIndex) : target;
  const fragment = hashIndex >= 0 ? target.slice(hashIndex) : '';
  const queryIndex = beforeHash.indexOf('?');
  const pathWithRange = queryIndex >= 0 ? beforeHash.slice(0, queryIndex) : beforeHash;
  const query = queryIndex >= 0 ? beforeHash.slice(queryIndex) : '';
  const { pathPart, range } = parseSourceReference(pathWithRange);
  return `${resolve(dirname(documentPath), decodeLinkPath(pathPart))}${formatSourceRange(range)}${query}${fragment}`;
}

function isRelativeUrl(source: string): boolean {
  return !/^(?:[a-z][a-z\d+.-]*:|\/\/|\/|#|\?)/i.test(source);
}

function decodeLinkPath(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

function preserveInlineSemantics(fragmentHtml: string): string {
  const dom = new JSDOM(`<div id="root">${fragmentHtml}</div>`);
  const root = dom.window.document.getElementById('root')!;
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('[style]'))) {
    const style = el.getAttribute('style') ?? '';
    if (!hasLineThrough(style)) continue;

    const del = dom.window.document.createElement('del');
    while (el.firstChild) del.appendChild(el.firstChild);
    el.replaceWith(del);
  }
  return root.innerHTML;
}

function hasLineThrough(style: string): boolean {
  return /(?:^|;)\s*text-decoration(?:-line)?\s*:[^;]*\bline-through\b/i.test(style);
}

function injectBlockIdViaDom(fragmentHtml: string, block: Block, headingIds: Map<string, number>): string {
  const dom = new JSDOM(`<div id="root">${fragmentHtml}</div>`);
  const root = dom.window.document.getElementById('root')!;
  if (block.kind === 'list' || block.kind === 'blockquote') {
    for (const [index, input] of Array.from(root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')).entries()) {
      input.setAttribute('data-task-checkbox-index', String(index));
    }
  }
  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType === 1 /* ELEMENT_NODE */) {
      const element = node as Element;
      element.setAttribute('data-block-id', block.id);
      if (block.sourceStartLine !== undefined) element.setAttribute('data-source-start-line', String(block.sourceStartLine));
      if (block.sourceEndLine !== undefined) element.setAttribute('data-source-end-line', String(block.sourceEndLine));
      if (/^H[1-6]$/.test(element.tagName)) {
        const baseId = slugifyHeading(element.textContent ?? '');
        const count = headingIds.get(baseId) ?? 0;
        headingIds.set(baseId, count + 1);
        element.setAttribute('id', count === 0 ? baseId : `${baseId}-${count}`);
      }
      break;
    }
  }
  return root.innerHTML;
}

function slugifyHeading(text: string): string {
  const slug = text
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}\s_-]/gu, '')
    .trim()
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
  return slug || 'section';
}
