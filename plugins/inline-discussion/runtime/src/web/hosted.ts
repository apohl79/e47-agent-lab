const requestedBase = new URLSearchParams(window.location.search).get('xedocBase')
  ?? new URL(document.baseURI).pathname;
const base = /^\/plugin-ui\/[a-zA-Z0-9_-]+\/$/.test(requestedBase) ? requestedBase : '/';

export const documentRoute = base === '/'
  ? window.location.pathname
  : `/${window.location.pathname.slice(base.length)}`;

export function panelUrl(path: string): string {
  return base !== '/' && path.startsWith('/') && !path.startsWith('//') && !path.startsWith(base)
    ? `${base}${path.slice(1)}`
    : path;
}

export const panelFetch: typeof window.fetch = (input, init) =>
  window.fetch(typeof input === 'string' ? panelUrl(input) : input, init);

export function installHostedLinks(): void {
  if (base === '/') return;
  const rewrite = (root: ParentNode): void => {
    for (const element of root.querySelectorAll<HTMLElement>('[href], [src]')) {
      for (const attr of ['href', 'src']) {
        const value = element.getAttribute(attr);
        if (value === null) continue;
        const mounted = panelUrl(value);
        if (mounted !== value) element.setAttribute(attr, mounted);
      }
    }
  };
  rewrite(document.body);
  new MutationObserver(() => rewrite(document.body))
    .observe(document.body, { childList: true, subtree: true });
}
