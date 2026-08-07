import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { svgToDataUrl } from '../src/web/svg-source.ts';

function svgIn(markup: string): { svg: SVGElement; dom: JSDOM } {
  const dom = new JSDOM(`<!doctype html><html><body>${markup}</body></html>`);
  const svg = dom.window.document.querySelector('svg');
  assert.ok(svg);
  globalThis.XMLSerializer = dom.window.XMLSerializer;
  return { svg: svg as unknown as SVGElement, dom };
}

test('serialized svg carries the namespace and viewBox pixel size', () => {
  const { svg } = svgIn('<svg viewBox="0 0 640 480" style="max-width: 100%"><rect/></svg>');

  const url = svgToDataUrl(svg);
  assert.match(url, /^data:image\/svg\+xml;charset=utf-8,/);

  const markup = decodeURIComponent(url.slice('data:image/svg+xml;charset=utf-8,'.length));
  assert.match(markup, /xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(markup, /width="640"/);
  assert.match(markup, /height="480"/);
  assert.doesNotMatch(markup, /max-width/);
});

test('serialization keeps non-latin1 diagram labels intact', () => {
  const { svg } = svgIn('<svg viewBox="0 0 10 10"><text>fast brain — SAP hosts</text></svg>');

  const url = svgToDataUrl(svg);
  const markup = decodeURIComponent(url.slice('data:image/svg+xml;charset=utf-8,'.length));
  assert.match(markup, /fast brain — SAP hosts/);
});

test('a missing or malformed viewBox leaves dimensions untouched', () => {
  const { svg } = svgIn('<svg viewBox="0 0 bad"><rect/></svg>');

  const markup = decodeURIComponent(svgToDataUrl(svg).slice('data:image/svg+xml;charset=utf-8,'.length));
  assert.doesNotMatch(markup, /width=/);
  assert.doesNotMatch(markup, /height=/);
});

test('the source diagram is not mutated by serialization', () => {
  const { svg } = svgIn('<svg viewBox="0 0 100 50" style="max-width: 100%"><rect/></svg>');

  svgToDataUrl(svg);

  assert.equal(svg.getAttribute('width'), null);
  assert.match(svg.getAttribute('style') ?? '', /max-width/);
});
