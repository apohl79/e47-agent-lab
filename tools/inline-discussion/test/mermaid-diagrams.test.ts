import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import {
  awaitsMermaidRender,
  captureDiagramSource,
  mermaidThemeFor,
  pendingDiagrams,
  renderedDiagrams,
  restoreDiagramSource,
} from '../src/web/mermaid-diagrams.ts';

test('mermaid theme follows the page theme', () => {
  assert.equal(mermaidThemeFor('dark'), 'dark');
  assert.equal(mermaidThemeFor('light'), 'neutral');
  assert.equal(mermaidThemeFor(undefined), 'neutral');
});

test('diagram source survives render so a theme switch can re-render it', () => {
  const dom = new JSDOM(
    '<!doctype html><html><body><main id="doc">'
      + '<pre class="mermaid" data-block-id="a">flowchart LR\n  A --&gt; B</pre>'
      + '</main></body></html>',
  );
  const { document } = dom.window;
  const diagram = document.querySelector<HTMLElement>('.mermaid');
  assert.ok(diagram);

  assert.equal(pendingDiagrams(document).length, 1);
  assert.equal(renderedDiagrams(document).length, 0);

  captureDiagramSource(diagram);
  assert.equal(diagram.dataset.mermaidSource, 'flowchart LR\n  A --> B');

  // What mermaid.run() does: mark processed and replace the source with SVG.
  diagram.setAttribute('data-processed', 'true');
  diagram.innerHTML = '<svg id="rendered"></svg>';
  // What installBlockPluses() adds once the diagram is rendered.
  diagram.insertAdjacentHTML('beforeend', '<button class="block-plus"></button>');
  assert.equal(pendingDiagrams(document).length, 0);
  assert.equal(renderedDiagrams(document).length, 1);

  restoreDiagramSource(diagram);
  assert.equal(diagram.textContent, 'flowchart LR\n  A --> B');
  assert.equal(diagram.hasAttribute('data-processed'), false);
  assert.equal(diagram.querySelector('#rendered'), null);
  assert.equal(diagram.querySelector('.block-plus'), null);
  assert.equal(pendingDiagrams(document).length, 1);
});

test('captureDiagramSource keeps the original source when called twice', () => {
  const dom = new JSDOM('<!doctype html><html><body><pre class="mermaid">flowchart LR</pre></body></html>');
  const diagram = dom.window.document.querySelector<HTMLElement>('.mermaid');
  assert.ok(diagram);

  captureDiagramSource(diagram);
  diagram.innerHTML = '<svg></svg>';
  captureDiagramSource(diagram);

  assert.equal(diagram.dataset.mermaidSource, 'flowchart LR');
});

test('unrendered diagrams are shielded from injected block UI', () => {
  const dom = new JSDOM(
    '<!doctype html><html><body><main id="doc">'
      + '<pre class="mermaid" data-block-id="a">flowchart LR\n  A --&gt; B</pre>'
      + '<pre class="mermaid" data-block-id="b" data-processed="true"><svg></svg></pre>'
      + '<p data-block-id="c">text</p>'
      + '</main></body></html>',
  );
  const { document } = dom.window;
  const [unrendered, rendered] = [...document.querySelectorAll<HTMLElement>('.mermaid')];
  const paragraph = document.querySelector<HTMLElement>('p');
  assert.ok(unrendered && rendered && paragraph);

  assert.equal(awaitsMermaidRender(unrendered), true);
  assert.equal(awaitsMermaidRender(rendered), false);
  assert.equal(awaitsMermaidRender(paragraph), false);
});
