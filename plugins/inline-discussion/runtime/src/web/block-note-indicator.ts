import { canAnnotateBlock } from './mermaid-diagrams.ts';

export function updateBlockNoteIndicator(
  anchor: HTMLElement,
  noteCount: number,
  reveal: () => void,
): void {
  const indicator = [...anchor.children].find((child) => child.classList.contains('block-note-indicator')) as HTMLButtonElement | undefined;
  if (noteCount <= 0) {
    indicator?.remove();
    return;
  }

  const button = indicator ?? document.createElement('button');
  button.type = 'button';
  button.className = 'block-note-indicator';
  button.dataset.noteCount = String(noteCount);
  button.setAttribute('aria-label', `Show ${noteCount} ${noteCount === 1 ? 'note' : 'notes'} for this block`);
  button.title = `${noteCount} ${noteCount === 1 ? 'note' : 'notes'} on this block`;
  button.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 3.5h12v10H9l-5 3v-13Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M7 7h6M7 10h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  button.onclick = (event) => {
    event.stopPropagation();
    reveal();
  };
  button.onfocus = reveal;
  if (!indicator) anchor.appendChild(button);
}

export function updateWholeBlockNoteIndicators(
  root: ParentNode,
  blockNoteCounts: ReadonlyMap<string, number>,
  reveal: (blockId: string) => void,
): void {
  for (const block of root.querySelectorAll<HTMLElement>('[data-block-id]')) {
    if (!canAnnotateBlock(block)) continue;
    const blockId = block.dataset.blockId;
    if (!blockId || blockNoteCounts.has(blockId)) continue;
    updateBlockNoteIndicator(block, 0, () => undefined);
  }
  for (const [blockId, count] of blockNoteCounts) {
    const block = root.querySelector<HTMLElement>(`[data-block-id="${blockId}"]`);
    if (block && canAnnotateBlock(block)) {
      updateBlockNoteIndicator(block, count, () => reveal(blockId));
    }
  }
}
