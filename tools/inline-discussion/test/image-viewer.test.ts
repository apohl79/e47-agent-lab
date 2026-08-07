import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { installImageViewer } from '../src/web/image-viewer.ts';

function setupDom(): JSDOM {
  return new JSDOM(
    '<!doctype html><html><body><main id="doc"><img src="/diagram.svg" alt="Architecture diagram"></main></body></html>',
    {
      url: 'http://localhost/',
      pretendToBeVisual: true,
    },
  );
}

function dispatch(document: Document, target: EventTarget, type: string, properties: Record<string, unknown> = {}): void {
  const event = document.createEvent('Event');
  event.initEvent(type, true, true);
  for (const [name, value] of Object.entries(properties)) {
    Object.defineProperty(event, name, { configurable: true, value });
  }
  target.dispatchEvent(event);
}

test('image viewer opens from document images and supports zoom, pan, reset, and close', () => {
  const dom = setupDom();
  const { document } = dom.window;
  const root = document.getElementById('doc');
  assert.ok(root);
  const image = root.querySelector('img');
  assert.ok(image);
  image.tabIndex = 0;
  installImageViewer(root);

  image.focus();
  dispatch(document, image, 'click');

  const backdrop = document.querySelector('.image-viewer-backdrop');
  assert.ok(backdrop);
  assert.equal(backdrop.getAttribute('role'), 'dialog');
  assert.equal(document.body.classList.contains('image-viewer-open'), true);
  assert.equal(backdrop.querySelector('.image-viewer-image')?.getAttribute('alt'), 'Architecture diagram');
  const zoomLabel = backdrop.querySelector('.image-viewer-zoom-label');
  assert.ok(zoomLabel);
  assert.equal(zoomLabel.textContent, '100%');

  const zoomIn = backdrop.querySelector<HTMLButtonElement>('[aria-label="Zoom in"]');
  assert.ok(zoomIn);
  dispatch(document, zoomIn, 'click');
  assert.equal(zoomLabel.textContent, '125%');

  const viewerImage = backdrop.querySelector<HTMLImageElement>('.image-viewer-image');
  assert.ok(viewerImage);
  dispatch(document, viewerImage, 'pointerdown', { clientX: 10, clientY: 20 });
  dispatch(document, viewerImage, 'pointermove', { clientX: 30, clientY: 35 });
  assert.equal(viewerImage.classList.contains('is-dragging'), true);
  assert.equal(viewerImage.style.transform, 'translate3d(20px, 15px, 0) scale(1.25)');
  dispatch(document, viewerImage, 'pointerup');
  assert.equal(viewerImage.classList.contains('is-dragging'), false);

  const reset = backdrop.querySelector<HTMLButtonElement>('[aria-label="Reset image zoom and position"]');
  assert.ok(reset);
  dispatch(document, reset, 'click');
  assert.equal(zoomLabel.textContent, '100%');
  assert.equal(viewerImage.style.transform, 'translate3d(0px, 0px, 0) scale(1)');

  dispatch(document, document, 'keydown', { key: 'Escape' });
  assert.equal(document.querySelector('.image-viewer-backdrop'), null);
  assert.equal(document.body.classList.contains('image-viewer-open'), false);
  assert.equal(document.activeElement, image);
  dom.window.close();
});

test('image viewer remains delegated when the document HTML is refreshed', () => {
  const dom = setupDom();
  const { document } = dom.window;
  const root = document.getElementById('doc');
  assert.ok(root);
  installImageViewer(root);
  root.innerHTML = '<p>Updated</p><img src="/updated.svg" alt="Updated diagram">';

  const image = root.querySelector('img');
  assert.ok(image);
  dispatch(document, image, 'click');
  assert.ok(document.querySelector('.image-viewer-backdrop'));
  dom.window.close();
});

test('clicking a rendered mermaid diagram opens it in the viewer', () => {
  const dom = setupDom();
  const { document } = dom.window;
  globalThis.XMLSerializer = dom.window.XMLSerializer;
  const root = document.getElementById('doc');
  assert.ok(root);
  installImageViewer(root);
  root.innerHTML = '<pre class="mermaid" data-block-id="a" data-processed="true">'
    + '<svg viewBox="0 0 300 150"><text>Node</text></svg></pre>';

  const label = root.querySelector('text');
  assert.ok(label);
  dispatch(document, label, 'click');

  const viewerImage = document.querySelector<HTMLImageElement>('.image-viewer-image');
  assert.ok(viewerImage);
  assert.equal(viewerImage.alt, 'Diagram');
  assert.match(viewerImage.getAttribute('src') ?? '', /^data:image\/svg\+xml;charset=utf-8,/);
  dom.window.close();
});

test('an unrendered mermaid block is not clickable', () => {
  const dom = setupDom();
  const { document } = dom.window;
  const root = document.getElementById('doc');
  assert.ok(root);
  installImageViewer(root);
  root.innerHTML = '<pre class="mermaid" data-block-id="a">flowchart LR\n  A --&gt; B</pre>';

  const diagram = root.querySelector('.mermaid');
  assert.ok(diagram);
  dispatch(document, diagram, 'click');

  assert.equal(document.querySelector('.image-viewer-backdrop'), null);
  dom.window.close();
});
