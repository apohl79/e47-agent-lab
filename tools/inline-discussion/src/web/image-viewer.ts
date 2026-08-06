const MIN_ZOOM = 0.5;
const MAX_ZOOM = 5;
const ZOOM_STEP = 0.25;

export type ImageViewerController = Readonly<{
  close: () => void;
  open: (image: HTMLImageElement) => void;
}>;

export function installImageViewer(root: HTMLElement): ImageViewerController {
  const doc = root.ownerDocument;
  let backdrop: HTMLDivElement | null = null;
  let viewerImage: HTMLImageElement | null = null;
  let zoomLabel: HTMLSpanElement | null = null;
  let zoomIn: HTMLButtonElement | null = null;
  let zoomOut: HTMLButtonElement | null = null;
  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let dragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragOriginX = 0;
  let dragOriginY = 0;
  let trigger: HTMLImageElement | null = null;

  const render = (): void => {
    if (!viewerImage || !zoomLabel || !zoomIn || !zoomOut) return;
    viewerImage.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`;
    zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    zoomIn.disabled = zoom >= MAX_ZOOM;
    zoomOut.disabled = zoom <= MIN_ZOOM;
  };

  const setZoom = (next: number): void => {
    zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(next.toFixed(2))));
    if (zoom <= 1) {
      panX = 0;
      panY = 0;
    }
    render();
  };

  const reset = (): void => {
    zoom = 1;
    panX = 0;
    panY = 0;
    render();
  };

  const finishDrag = (): void => {
    if (!dragging || !viewerImage) return;
    dragging = false;
    viewerImage.classList.remove('is-dragging');
  };

  const close = (): void => {
    if (!backdrop) return;
    finishDrag();
    backdrop.remove();
    doc.body.classList.remove('image-viewer-open');
    doc.removeEventListener('keydown', onKeyDown, true);
    trigger?.focus();
    backdrop = null;
    viewerImage = null;
    zoomLabel = null;
    zoomIn = null;
    zoomOut = null;
    trigger = null;
  };

  const onKeyDown = (event: KeyboardEvent): void => {
    if (!backdrop) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      setZoom(zoom + ZOOM_STEP);
    } else if (event.key === '-') {
      event.preventDefault();
      setZoom(zoom - ZOOM_STEP);
    } else if (event.key === '0') {
      event.preventDefault();
      reset();
    }
  };

  const createButton = (label: string, ariaLabel: string, action: () => void): HTMLButtonElement => {
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'image-viewer-btn';
    button.textContent = label;
    button.setAttribute('aria-label', ariaLabel);
    button.addEventListener('click', action);
    return button;
  };

  const createViewer = (): void => {
    backdrop = doc.createElement('div');
    backdrop.className = 'image-viewer-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-label', 'Image viewer');
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) close();
    });

    const header = doc.createElement('div');
    header.className = 'image-viewer-header';
    const title = doc.createElement('span');
    title.className = 'image-viewer-title';
    title.textContent = 'Image viewer';
    header.appendChild(title);
    header.appendChild(createButton('×', 'Close image viewer', close));

    const viewport = doc.createElement('div');
    viewport.className = 'image-viewer-viewport';
    viewport.addEventListener('wheel', (event) => {
      event.preventDefault();
      setZoom(zoom + (event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP));
    }, { passive: false });

    viewerImage = doc.createElement('img');
    viewerImage.className = 'image-viewer-image';
    viewerImage.draggable = false;
    viewerImage.addEventListener('pointerdown', (event) => {
      if (zoom <= 1 || !viewerImage) return;
      dragging = true;
      dragStartX = event.clientX;
      dragStartY = event.clientY;
      dragOriginX = panX;
      dragOriginY = panY;
      viewerImage.classList.add('is-dragging');
      viewerImage.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    });
    viewerImage.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      panX = dragOriginX + event.clientX - dragStartX;
      panY = dragOriginY + event.clientY - dragStartY;
      render();
    });
    viewerImage.addEventListener('pointerup', finishDrag);
    viewerImage.addEventListener('pointercancel', finishDrag);
    viewport.appendChild(viewerImage);

    const toolbar = doc.createElement('div');
    toolbar.className = 'image-viewer-toolbar';
    zoomOut = createButton('−', 'Zoom out', () => setZoom(zoom - ZOOM_STEP));
    zoomIn = createButton('+', 'Zoom in', () => setZoom(zoom + ZOOM_STEP));
    const resetButton = createButton('Reset', 'Reset image zoom and position', reset);
    zoomLabel = doc.createElement('span');
    zoomLabel.className = 'image-viewer-zoom-label';
    zoomLabel.setAttribute('aria-live', 'polite');
    toolbar.append(zoomOut, zoomLabel, zoomIn, resetButton);

    backdrop.append(header, viewport, toolbar);
    doc.body.appendChild(backdrop);
  };

  const open = (image: HTMLImageElement): void => {
    if (!backdrop) createViewer();
    if (!viewerImage) return;
    trigger = image;
    viewerImage.src = image.currentSrc || image.getAttribute('src') || '';
    viewerImage.alt = image.alt;
    reset();
    doc.body.classList.add('image-viewer-open');
    doc.addEventListener('keydown', onKeyDown, true);
    const closeButton = backdrop?.querySelector<HTMLButtonElement>('.image-viewer-header .image-viewer-btn');
    closeButton?.focus();
  };

  const onRootClick = (event: MouseEvent): void => {
    const view = doc.defaultView;
    if (!view || !(event.target instanceof view.Element)) return;
    const image = event.target.closest('img');
    if (!image || !root.contains(image)) return;
    event.preventDefault();
    open(image as HTMLImageElement);
  };

  root.addEventListener('click', onRootClick);
  return { open, close };
}
