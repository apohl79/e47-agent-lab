export type OverlayAnchorRect = Readonly<{
  left: number;
  top: number;
  bottom: number;
}>;

export type OverlayPlacementOptions = Readonly<{
  rect: OverlayAnchorRect;
  underQuote: boolean;
  scrollX: number;
  scrollY: number;
  viewportWidth: number;
  width: number;
  offset: number;
  gap?: number;
}>;

export type OverlayPlacement = Readonly<{
  left: number;
  top: number;
}>;

export function calculateOverlayPlacement(options: OverlayPlacementOptions): OverlayPlacement {
  const gap = options.gap ?? 8;
  const minLeft = options.scrollX + gap;
  const maxLeft = Math.max(minLeft, options.scrollX + options.viewportWidth - options.width - gap);
  const left = Math.min(Math.max(options.rect.left + options.scrollX, minLeft), maxLeft);
  const top = (options.underQuote ? options.rect.bottom : options.rect.top)
    + options.scrollY
    + gap
    + options.offset;
  return { left, top };
}
