export type TaskCheckboxChange = Readonly<{
  blockId: string;
  checkboxIndex: number;
  checked: boolean;
}>;

export type TaskCheckboxChangeHandler = (change: TaskCheckboxChange, input: HTMLInputElement) => void;

export function bindTaskCheckboxes(root: ParentNode, onChange: TaskCheckboxChangeHandler): void {
  for (const input of root.querySelectorAll<HTMLInputElement>('input[data-task-checkbox-index]')) {
    if (input.dataset.taskCheckboxBound) continue;
    const block = input.closest<HTMLElement>('[data-block-id]');
    const blockId = block?.dataset.blockId;
    const checkboxIndex = Number(input.dataset.taskCheckboxIndex);
    if (!blockId || !Number.isSafeInteger(checkboxIndex) || checkboxIndex < 0) continue;
    input.disabled = false;
    input.dataset.taskCheckboxBound = '1';
    input.addEventListener('change', () => onChange({ blockId, checkboxIndex, checked: input.checked }, input));
  }
}
