import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';

const stylesheet = readFileSync(new URL('../src/web/app.css', import.meta.url), 'utf8');

test('thread annotation styles preserve active response bubble geometry', () => {
  const dom = new JSDOM(`
    <style>${stylesheet}</style>
    <main id="doc">
      <div class="msg assistant"></div>
      <div class="msg assistant streaming" data-thread-id="thread-1"></div>
      <div class="msg user"></div>
      <mark class="quote-highlight quote-highlight-thread" data-thread-id="thread-1"></mark>
    </main>
  `);
  const completed = dom.window.document.querySelector<HTMLElement>('.assistant:not(.streaming)');
  const stream = dom.window.document.querySelector<HTMLElement>('.streaming');
  const user = dom.window.document.querySelector<HTMLElement>('.user');
  const mark = dom.window.document.querySelector<HTMLElement>('.quote-highlight');

  assert.ok(completed);
  assert.ok(stream);
  assert.ok(user);
  assert.ok(mark);
  assert.deepEqual(
    {
      completedRadius: dom.window.getComputedStyle(completed).borderRadius,
      completedBottomLeftOverride: dom.window.getComputedStyle(completed).borderBottomLeftRadius,
      streamRadius: dom.window.getComputedStyle(stream).borderRadius,
      streamPadding: dom.window.getComputedStyle(stream).padding,
      userRadius: dom.window.getComputedStyle(user).borderRadius,
      userBottomRightOverride: dom.window.getComputedStyle(user).borderBottomRightRadius,
      markRadius: dom.window.getComputedStyle(mark).borderRadius,
      markPadding: dom.window.getComputedStyle(mark).padding,
    },
    {
      completedRadius: 'var(--radius)',
      completedBottomLeftOverride: '',
      streamRadius: 'var(--radius-lg)',
      streamPadding: '10px 14px 42px',
      userRadius: 'var(--radius)',
      userBottomRightOverride: '',
      markRadius: '2px',
      markPadding: '0px 1px',
    },
  );
});

test('note header keeps actions in a compact row above the anchor quote', () => {
  const dom = new JSDOM(`
    <style>${stylesheet}</style>
    <div class="thread-header note-header">
      <div class="note-header-row">
        <div class="thread-label"><strong>Note</strong></div>
        <div class="thread-actions"><button class="btn">Ask assistant</button></div>
      </div>
      <div class="anchor-quote">A long anchored excerpt</div>
    </div>
  `);
  const header = dom.window.document.querySelector<HTMLElement>('.note-header');
  const row = dom.window.document.querySelector<HTMLElement>('.note-header-row');
  const actions = dom.window.document.querySelector<HTMLElement>('.thread-actions');
  const button = dom.window.document.querySelector<HTMLElement>('.btn');
  const quote = dom.window.document.querySelector<HTMLElement>('.anchor-quote');

  assert.ok(header);
  assert.ok(row);
  assert.ok(actions);
  assert.ok(button);
  assert.ok(quote);
  assert.deepEqual({
    headerDisplay: dom.window.getComputedStyle(header).display,
    rowDisplay: dom.window.getComputedStyle(row).display,
    rowJustification: dom.window.getComputedStyle(row).justifyContent,
    actionWrap: dom.window.getComputedStyle(actions).flexWrap,
    buttonWhiteSpace: dom.window.getComputedStyle(button).whiteSpace,
    quoteDisplay: dom.window.getComputedStyle(quote).display,
  }, {
    headerDisplay: 'block',
    rowDisplay: 'flex',
    rowJustification: 'space-between',
    actionWrap: 'nowrap',
    buttonWhiteSpace: 'nowrap',
    quoteDisplay: 'block',
  });
});
