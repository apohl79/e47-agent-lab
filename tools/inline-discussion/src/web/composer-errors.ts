export function replaceComposerError(box: HTMLElement, message: string): void {
  box.querySelector('.composer-error')?.remove();
  const error = box.ownerDocument.createElement('div');
  error.className = 'composer-error';
  error.textContent = message;
  box.appendChild(error);
}
