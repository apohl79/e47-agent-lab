type NavigationEnvironment = Readonly<{
  document: Document;
  history: Pick<History, 'pushState'>;
  location: Pick<Location, 'href'>;
}>;

export function scrollToFragment(hash: string, document: Document): boolean {
  const rawHash = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!rawHash) return false;
  let id: string;
  try {
    id = decodeURIComponent(rawHash);
  } catch {
    return false;
  }
  const target = document.getElementById(id);
  if (!target) return false;
  target.scrollIntoView({ block: 'start' });
  return true;
}

export function sameDocumentFragment(href: string, currentHref: string): string | null {
  let target: URL;
  let current: URL;
  try {
    target = new URL(href, currentHref);
    current = new URL(currentHref);
  } catch {
    return null;
  }
  if (
    !target.hash
    || target.origin !== current.origin
    || target.pathname !== current.pathname
    || target.search !== current.search
  ) {
    return null;
  }
  return target.hash;
}

export function installInDocumentNavigation(
  root: HTMLElement,
  environment: NavigationEnvironment,
): () => void {
  const onClick = (event: MouseEvent): void => {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) {
      return;
    }
    const view = root.ownerDocument.defaultView;
    if (!view || !(event.target instanceof view.Element)) return;
    const anchor = event.target.closest<HTMLAnchorElement>('a[href]');
    if (!anchor || !root.contains(anchor) || anchor.hasAttribute('download')) return;
    const target = anchor.getAttribute('target');
    if (target && target.toLowerCase() !== '_self') return;
    const fragment = sameDocumentFragment(anchor.getAttribute('href') ?? '', environment.location.href);
    if (!fragment || !scrollToFragment(fragment, environment.document)) return;
    event.preventDefault();
    if (new URL(environment.location.href).hash !== fragment) {
      environment.history.pushState(null, '', fragment);
    }
  };
  root.addEventListener('click', onClick);
  return () => root.removeEventListener('click', onClick);
}
