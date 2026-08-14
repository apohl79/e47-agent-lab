import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import {
  installInDocumentNavigation,
  sameDocumentFragment,
  scrollToFragment,
} from '../src/web/navigation.ts';

test('scrollToFragment resolves encoded heading ids and scrolls the target', () => {
  const dom = new JSDOM('<main><h1 id="2026-02-dining">Dining</h1></main>');
  const target = dom.window.document.getElementById('2026-02-dining')!;
  let scrolled = false;
  target.scrollIntoView = () => { scrolled = true; };

  assert.equal(scrollToFragment('#2026-02-dining', dom.window.document), true);
  assert.equal(scrolled, true);
});

test('scrollToFragment ignores malformed or missing fragments', () => {
  const dom = new JSDOM('<main><h1 id="section">Section</h1></main>');

  assert.equal(scrollToFragment('', dom.window.document), false);
  assert.equal(scrollToFragment('#missing', dom.window.document), false);
  assert.equal(scrollToFragment('#%', dom.window.document), false);
});

test('sameDocumentFragment accepts only fragments targeting the current document', () => {
  const current = 'http://127.0.0.1:5000/docs/review.md?mode=read';

  assert.deepEqual({
    hashOnly: sameDocumentFragment('#details', current),
    samePath: sameDocumentFragment('./review.md?mode=read#details', current),
    otherPath: sameDocumentFragment('./other.md?mode=read#details', current),
    otherQuery: sameDocumentFragment('./review.md?mode=edit#details', current),
    otherOrigin: sameDocumentFragment('https://example.com/docs/review.md#details', current),
  }, {
    hashOnly: '#details',
    samePath: '#details',
    otherPath: null,
    otherQuery: null,
    otherOrigin: null,
  });
});

test('installInDocumentNavigation scrolls without navigating away', () => {
  const dom = new JSDOM(
    '<main><a href="#details"><span>Details</span></a><h2 id="details">Details</h2></main>',
    { url: 'http://127.0.0.1:5000/docs/review.md' },
  );
  const root = dom.window.document.querySelector('main')!;
  const target = dom.window.document.getElementById('details')!;
  let scrolled = false;
  target.scrollIntoView = () => { scrolled = true; };
  const uninstall = installInDocumentNavigation(root, {
    document: dom.window.document,
    history: dom.window.history,
    location: dom.window.location,
  });
  const click = new dom.window.MouseEvent('click', { bubbles: true, button: 0, cancelable: true });

  dom.window.document.querySelector('span')!.dispatchEvent(click);

  assert.deepEqual({
    defaultPrevented: click.defaultPrevented,
    scrolled,
    pathname: dom.window.location.pathname,
    hash: dom.window.location.hash,
  }, {
    defaultPrevented: true,
    scrolled: true,
    pathname: '/docs/review.md',
    hash: '#details',
  });
  uninstall();
});
