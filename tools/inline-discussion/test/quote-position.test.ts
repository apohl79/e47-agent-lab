import { test } from 'node:test';
import assert from 'node:assert/strict';
import { quoteOccurrence } from '../src/web/quote-position.ts';

test('quoteOccurrence identifies the selected duplicate inside one table block', () => {
  const text = 'account DE655001 source\naccount DE655001 source';
  const quote = 'DE655001';
  const selectedStart = text.lastIndexOf(quote);

  assert.equal(quoteOccurrence(text, quote, selectedStart), 2);
});

test('quoteOccurrence defaults to the first occurrence for block-level anchors', () => {
  assert.equal(quoteOccurrence('same same', 'same', 0), 1);
});
