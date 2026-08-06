// src/markdown.ts
import { marked, type Tokens, type TokensList } from 'marked';
import { createHash } from 'node:crypto';
import { extname } from 'node:path';
import type { Block, BlockKind } from './types.ts';
import hljs from 'highlight.js';
import DOMPurify from 'isomorphic-dompurify';
import { JSDOM } from 'jsdom';

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
  return raw.replace(/\s+/g, ' ');
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
  const tokens = marked.lexer(normalizeDetailsSpacing(markdown)) as TokensList;
  const links = tokens.links ?? {};
  const usedIds = new Map<string, number>();
  const blocks: Block[] = [];

  for (const token of tokens) {
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
      markdown: token.raw ?? '',
      html: marked.parser(singleton),
    });
  }

  return { blocks, blockIds: blocks.map((b) => b.id), links };
}

const renderer = new marked.Renderer();
renderer.code = function (code: string, infostring: string | undefined, _escaped: boolean): string {
  const lang = (infostring ?? '').trim().split(/\s+/)[0] || undefined;
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
  ADD_ATTR: ['data-block-id', 'data-thread-id', 'checked', 'disabled', 'type'],
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
    const withDocumentAssets = documentPath
      ? rewriteRelativeImageUrls(clean, documentPath)
      : clean;
    return injectBlockIdViaDom(withDocumentAssets, block.id, headingIds);
  });
  return { ...parsed, html: pieces.join('\n') };
}

function rewriteRelativeImageUrls(fragmentHtml: string, documentPath: string): string {
  const dom = new JSDOM(`<div id="root">${fragmentHtml}</div>`);
  const root = dom.window.document.getElementById('root')!;
  for (const image of Array.from(root.querySelectorAll<HTMLImageElement>('img[src]'))) {
    const source = image.getAttribute('src')?.trim();
    if (!source || !isRelativeImageUrl(source)) continue;
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
  return root.innerHTML;
}

function isRelativeImageUrl(source: string): boolean {
  return !/^(?:[a-z][a-z\d+.-]*:|\/\/|\/|#)/i.test(source);
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

function injectBlockIdViaDom(fragmentHtml: string, blockId: string, headingIds: Map<string, number>): string {
  const dom = new JSDOM(`<div id="root">${fragmentHtml}</div>`);
  const root = dom.window.document.getElementById('root')!;
  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType === 1 /* ELEMENT_NODE */) {
      const element = node as Element;
      element.setAttribute('data-block-id', blockId);
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
