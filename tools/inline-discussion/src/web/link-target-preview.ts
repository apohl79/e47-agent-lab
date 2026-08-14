type Schedule = (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
type Cancel = (handle: ReturnType<typeof setTimeout>) => void;

export function installLinkTargetPreview(
  root: HTMLElement,
  delayMs = 1_000,
  schedule: Schedule = setTimeout,
  cancel: Cancel = clearTimeout,
): () => void {
  let pending: ReturnType<typeof setTimeout> | null = null;
  let tooltip: HTMLElement | null = null;

  const clear = (): void => {
    if (pending !== null) cancel(pending);
    pending = null;
    tooltip?.remove();
    tooltip = null;
  };

  const onMouseOver = (event: MouseEvent): void => {
    const elementType = root.ownerDocument.defaultView!.Element;
    const target = event.target instanceof elementType
      ? (event.target as Element).closest<HTMLAnchorElement>('a[href]')
      : null;
    if (target === null || !root.contains(target)) return;
    clear();
    const { clientX, clientY } = event;
    pending = schedule(() => {
      if (!target.isConnected) return;
      tooltip = target.ownerDocument.createElement('div');
      tooltip.className = 'link-target-preview';
      tooltip.setAttribute('role', 'tooltip');
      tooltip.textContent = target.href;
      tooltip.style.left = `${Math.min(clientX + 12, target.ownerDocument.defaultView!.innerWidth - 24)}px`;
      tooltip.style.top = `${Math.min(clientY + 18, target.ownerDocument.defaultView!.innerHeight - 24)}px`;
      target.ownerDocument.body.appendChild(tooltip);
      pending = null;
    }, delayMs);
  };

  const onMouseOut = (event: MouseEvent): void => {
    const window = root.ownerDocument.defaultView!;
    const target = event.target instanceof window.Element
      ? (event.target as Element).closest<HTMLAnchorElement>('a[href]')
      : null;
    if (target === null) return;
    if (event.relatedTarget instanceof window.Node && target.contains(event.relatedTarget)) return;
    clear();
  };

  root.addEventListener('mouseover', onMouseOver);
  root.addEventListener('mouseout', onMouseOut);
  root.addEventListener('click', clear);
  return () => {
    clear();
    root.removeEventListener('mouseover', onMouseOver);
    root.removeEventListener('mouseout', onMouseOut);
    root.removeEventListener('click', clear);
  };
}
