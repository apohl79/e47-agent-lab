import type { SourceRange } from '../types.ts';

const TARGET_MARK_CLASS = 'source-range-target';

function unwrapTargetMarks(root: ParentNode): void {
  for (const mark of root.querySelectorAll<HTMLElement>(`.${TARGET_MARK_CLASS}`)) {
    const parent = mark.parentNode;
    if (parent === null) continue;
    while (mark.firstChild !== null) parent.insertBefore(mark.firstChild, mark);
    mark.remove();
    parent.normalize();
  }
}

function resetSourceTargets(root: ParentNode): void {
  for (const element of root.querySelectorAll<HTMLElement>('.source-line-target, .source-block-target')) {
    element.classList.remove('source-line-target', 'source-block-target');
  }
  unwrapTargetMarks(root);
}

function wrapTextOffsets(root: HTMLElement, startOffset: number, endOffset: number): HTMLElement | null {
  const document = root.ownerDocument;
  const walker = document.createTreeWalker(root, document.defaultView!.NodeFilter.SHOW_TEXT);
  const nodes: Array<{ node: Text; start: number; end: number }> = [];
  let textOffset = 0;
  let current = walker.nextNode();
  while (current !== null) {
    const text = current as Text;
    const nextOffset = textOffset + text.data.length;
    nodes.push({ node: text, start: textOffset, end: nextOffset });
    textOffset = nextOffset;
    current = walker.nextNode();
  }

  let firstMark: HTMLElement | null = null;
  for (const entry of nodes.reverse()) {
    const selectionStart = Math.max(startOffset, entry.start);
    const selectionEnd = Math.min(endOffset, entry.end);
    if (selectionStart >= selectionEnd) continue;
    const range = document.createRange();
    range.setStart(entry.node, selectionStart - entry.start);
    range.setEnd(entry.node, selectionEnd - entry.start);
    const mark = document.createElement('mark');
    mark.className = TARGET_MARK_CLASS;
    range.surroundContents(mark);
    firstMark = mark;
  }
  return firstMark;
}

function sourceLineElements(root: ParentNode, range: SourceRange): HTMLElement[] {
  const lines: HTMLElement[] = [];
  for (let line = range.startLine; line <= range.endLine; line += 1) {
    const element = root.querySelector<HTMLElement>(`[data-block-id="line-${line}"]`);
    if (element !== null) lines.push(element);
  }
  return lines;
}

function highlightSourceCharacters(lines: HTMLElement[], range: SourceRange): HTMLElement | null {
  if (range.startColumn === undefined || range.endColumn === undefined) return null;
  let firstMark: HTMLElement | null = null;
  for (const [index, line] of lines.entries()) {
    const code = line.querySelector<HTMLElement>('.source-line-code');
    if (code === null) continue;
    const start = index === 0 ? range.startColumn - 1 : 0;
    const end = index === lines.length - 1 ? range.endColumn : code.textContent?.length ?? 0;
    const mark = wrapTextOffsets(code, start, end);
    firstMark ??= mark;
  }
  return firstMark;
}

function markdownBlocks(root: ParentNode, range: SourceRange): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>('[data-source-start-line][data-source-end-line]'))
    .filter((element) => {
      const start = Number.parseInt(element.dataset.sourceStartLine ?? '', 10);
      const end = Number.parseInt(element.dataset.sourceEndLine ?? '', 10);
      return Number.isFinite(start) && Number.isFinite(end) && start <= range.endLine && end >= range.startLine;
    });
}

function highlightRenderedText(blocks: HTMLElement[], targetText: string | null): HTMLElement | null {
  const selectedText = targetText?.trim() ?? '';
  if (selectedText === '') return null;
  for (const block of blocks) {
    const offset = (block.textContent ?? '').indexOf(selectedText);
    if (offset < 0) continue;
    return wrapTextOffsets(block, offset, offset + selectedText.length);
  }
  return null;
}

export function focusSourceRange(
  range: SourceRange | null | undefined,
  targetText: string | null | undefined,
  root: Document = document,
): boolean {
  resetSourceTargets(root);
  if (range === null || range === undefined) return false;

  const lines = sourceLineElements(root, range);
  if (lines.length > 0) {
    for (const line of lines) line.classList.add('source-line-target');
    const exactTarget = highlightSourceCharacters(lines, range);
    (exactTarget ?? lines[0])?.scrollIntoView?.({ block: 'center' });
    return true;
  }

  const blocks = markdownBlocks(root, range);
  if (blocks.length === 0) return false;
  for (const block of blocks) block.classList.add('source-block-target');
  const exactTarget = highlightRenderedText(blocks, targetText ?? null);
  (exactTarget ?? blocks[0])?.scrollIntoView?.({ block: 'center' });
  return true;
}
