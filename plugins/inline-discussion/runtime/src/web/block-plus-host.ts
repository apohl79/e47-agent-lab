export function blockPlusHost(element: HTMLElement): HTMLElement {
  const parent = element.parentElement;
  if (parent?.classList.contains('block-shell')) return parent;
  if (element.tagName !== 'PRE') return element;

  const shell = element.ownerDocument.createElement('div');
  shell.className = 'block-shell';
  element.replaceWith(shell);
  shell.appendChild(element);
  return shell;
}
