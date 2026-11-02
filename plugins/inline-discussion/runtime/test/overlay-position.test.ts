import { test } from 'node:test';
import assert from 'node:assert/strict';
import { calculateOverlayPlacement } from '../src/web/overlay-position.ts';

test('calculateOverlayPlacement keeps an overlay attached to its anchor after scrolling', () => {
  const beforeScroll = calculateOverlayPlacement({
    rect: { left: 900, top: 100, bottom: 140 },
    underQuote: true,
    scrollX: 0,
    scrollY: 0,
    viewportWidth: 1_200,
    width: 400,
    offset: 0,
  });
  const afterScroll = calculateOverlayPlacement({
    rect: { left: 900, top: -300, bottom: -260 },
    underQuote: true,
    scrollX: 0,
    scrollY: 400,
    viewportWidth: 1_200,
    width: 400,
    offset: 0,
  });

  assert.deepEqual(afterScroll, beforeScroll);
});

test('calculateOverlayPlacement clamps overlays to the viewport edges', () => {
  const placement = calculateOverlayPlacement({
    rect: { left: -40, top: 10, bottom: 20 },
    underQuote: false,
    scrollX: 0,
    scrollY: 0,
    viewportWidth: 500,
    width: 400,
    offset: 0,
  });

  assert.equal(placement.left, 8);
  assert.equal(placement.top, 18);
});
