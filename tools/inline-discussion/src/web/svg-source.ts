// Mermaid renders inline <svg>, but the viewer transforms an <img>. Serializing
// the diagram to a data URL reuses the existing zoom/pan path unchanged.
// encodeURIComponent rather than btoa: diagram labels contain non-Latin1
// characters such as em dashes, which btoa rejects.

function explicitSize(svg: SVGElement): { width: string; height: string } | null {
  const viewBox = svg.getAttribute('viewBox');
  if (!viewBox) return null;
  const parts = viewBox.trim().split(/[\s,]+/);
  if (parts.length !== 4) return null;
  const width = Number(parts[2]);
  const height = Number(parts[3]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
  return { width: String(width), height: String(height) };
}

export function svgToDataUrl(svg: SVGElement): string {
  const clone = svg.cloneNode(true) as SVGElement;
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  // A max-width carried over from the page would cap the zoomed render.
  clone.style.removeProperty('max-width');
  const size = explicitSize(clone);
  if (size) {
    clone.setAttribute('width', size.width);
    clone.setAttribute('height', size.height);
  }
  const markup = new XMLSerializer().serializeToString(clone);
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
}
