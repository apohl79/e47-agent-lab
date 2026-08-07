// Mermaid replaces a diagram's innerHTML with rendered SVG, destroying the
// source. Re-rendering after a theme switch needs that source back, so it is
// stashed on the element before the first render.

export type MermaidThemeName = 'neutral' | 'dark';

export function mermaidThemeFor(pageTheme: string | undefined): MermaidThemeName {
  return pageTheme === 'dark' ? 'dark' : 'neutral';
}

export function pendingDiagrams(root: ParentNode): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>('.mermaid:not([data-processed])')];
}

// A block awaiting render must stay untouched: mermaid reads innerHTML as
// diagram source, so injected UI becomes part of the graph and fails to parse.
export function awaitsMermaidRender(element: HTMLElement): boolean {
  return element.classList.contains('mermaid') && !element.hasAttribute('data-processed');
}

export function renderedDiagrams(root: ParentNode): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>('.mermaid[data-mermaid-source]')];
}

export function captureDiagramSource(element: HTMLElement): void {
  if (element.dataset.mermaidSource === undefined) {
    element.dataset.mermaidSource = element.textContent ?? '';
  }
}

export function restoreDiagramSource(element: HTMLElement): void {
  const source = element.dataset.mermaidSource;
  if (source === undefined) return;
  element.textContent = source;
  element.removeAttribute('data-processed');
}
