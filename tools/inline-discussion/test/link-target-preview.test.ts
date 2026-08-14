import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { installLinkTargetPreview } from '../src/web/link-target-preview.ts';

test('link target preview shows the resolved URL after a one-second hover', () => {
  const dom = new JSDOM('<main id="root"><a href="/docs/guide.md:40-45">Guide</a></main>', {
    url: 'http://127.0.0.1:51168/',
    pretendToBeVisual: true,
  });
  const root = dom.window.document.getElementById('root')!;
  const link = root.querySelector('a')!;
  let callback: (() => void) | null = null;
  let scheduledDelay = 0;
  const cleanup = installLinkTargetPreview(
    root,
    1_000,
    (next, delay) => {
      callback = next;
      scheduledDelay = delay;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    },
    () => {},
  );

  link.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true, clientX: 20, clientY: 30 }));
  assert.equal(scheduledDelay, 1_000);
  assert.equal(dom.window.document.querySelector('.link-target-preview'), null);
  assert.ok(callback);
  (callback as () => void)();
  assert.equal(
    dom.window.document.querySelector('.link-target-preview')?.textContent,
    'http://127.0.0.1:51168/docs/guide.md:40-45',
  );

  link.dispatchEvent(new dom.window.MouseEvent('mouseout', { bubbles: true }));
  assert.equal(dom.window.document.querySelector('.link-target-preview'), null);
  cleanup();
  dom.window.close();
});
