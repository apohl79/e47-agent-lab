export function appendQuoteToTextarea(textarea: HTMLTextAreaElement, text: string): void {
  const quoted = text.split('\n').map((line) => `| ${line}`).join('\n');
  const existing = textarea.value;
  const separator = existing
    ? (existing.endsWith('\n\n') ? '' : existing.endsWith('\n') ? '\n' : '\n\n')
    : '';
  textarea.value = existing + separator + quoted + '\n\n';
  requestAnimationFrame(() => {
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  });
}
