import type { Thread } from '../types.ts';

function previousBlockId(details: HTMLDetailsElement): string | undefined {
  let sibling = details.previousElementSibling;
  while (sibling) {
    const blockId = sibling.getAttribute('data-block-id');
    if (blockId) return blockId;
    sibling = sibling.previousElementSibling;
  }
  return undefined;
}

export function findThreadDetails(
  details: Iterable<HTMLDetailsElement>,
  thread: Thread,
): HTMLDetailsElement | undefined {
  const kindLabel = thread.kind === 'note' ? '📝 Note on' : '💬 Thread on';
  const anchorLabel = thread.anchor.quote ?? 'entire block';
  return [...details].find((candidate) => {
    const summary = candidate.querySelector(':scope > summary')?.textContent ?? '';
    return candidate.dataset.threadId === thread.id || (
      previousBlockId(candidate) === thread.anchor.blockId &&
      summary.includes(kindLabel) &&
      summary.includes(anchorLabel)
    );
  });
}
