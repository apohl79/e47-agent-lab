type ShiftArrowKey = 'ArrowLeft' | 'ArrowRight' | 'ArrowUp' | 'ArrowDown';
type TextareaSelectionDirection = 'forward' | 'backward' | 'none';

type TextareaSelection = Readonly<{
  start: number;
  end: number;
  direction: TextareaSelectionDirection;
}>;

const isShiftArrowKey = (key: string): key is ShiftArrowKey =>
  key === 'ArrowLeft' || key === 'ArrowRight' || key === 'ArrowUp' || key === 'ArrowDown';

const lineStartOf = (value: string, position: number): number =>
  value.lastIndexOf('\n', Math.max(0, position - 1)) + 1;

const lineEndOf = (value: string, position: number): number => {
  const nextBreak = value.indexOf('\n', position);
  return nextBreak === -1 ? value.length : nextBreak;
};

const moveVertical = (value: string, position: number, direction: 'up' | 'down'): number => {
  const lineStart = lineStartOf(value, position);
  const column = position - lineStart;
  const lineEnd = lineEndOf(value, position);
  if (direction === 'up') {
    if (lineStart === 0) return 0;
    const previousLineEnd = lineStart - 1;
    const previousLineStart = lineStartOf(value, previousLineEnd);
    return Math.min(previousLineStart + column, previousLineEnd);
  }
  if (lineEnd === value.length) return value.length;
  const nextLineStart = lineEnd + 1;
  const nextLineEnd = lineEndOf(value, nextLineStart);
  return Math.min(nextLineStart + column, nextLineEnd);
};

const moveFocus = (value: string, position: number, key: ShiftArrowKey): number => {
  if (key === 'ArrowLeft') return Math.max(0, position - 1);
  if (key === 'ArrowRight') return Math.min(value.length, position + 1);
  return moveVertical(value, position, key === 'ArrowUp' ? 'up' : 'down');
};

export function calculateShiftArrowSelection(
  value: string,
  selectionStart: number,
  selectionEnd: number,
  selectionDirection: TextareaSelectionDirection,
  key: ShiftArrowKey,
): TextareaSelection {
  const anchor = selectionDirection === 'backward' ? selectionEnd : selectionStart;
  const focus = selectionDirection === 'backward' ? selectionStart : selectionEnd;
  const nextFocus = moveFocus(value, focus, key);
  const start = Math.min(anchor, nextFocus);
  const end = Math.max(anchor, nextFocus);
  const direction: TextareaSelectionDirection =
    nextFocus < anchor ? 'backward' : nextFocus > anchor ? 'forward' : 'none';
  return { start, end, direction };
}

export function installShiftArrowTextareaSelection(textarea: HTMLTextAreaElement): void {
  textarea.addEventListener('keydown', (event) => {
    if (
      !event.shiftKey ||
      event.altKey ||
      event.metaKey ||
      event.ctrlKey ||
      event.isComposing ||
      !isShiftArrowKey(event.key)
    ) {
      return;
    }
    const next = calculateShiftArrowSelection(
      textarea.value,
      textarea.selectionStart,
      textarea.selectionEnd,
      textarea.selectionDirection,
      event.key,
    );
    if (
      next.start === textarea.selectionStart &&
      next.end === textarea.selectionEnd &&
      next.direction === textarea.selectionDirection
    ) {
      return;
    }
    event.preventDefault();
    textarea.setSelectionRange(next.start, next.end, next.direction);
  });
}
