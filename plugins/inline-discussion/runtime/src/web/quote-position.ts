export function quoteOccurrence(text: string, quote: string, selectionStart: number): number {
  if (!quote) return 1;
  let occurrence = 0;
  let from = 0;
  while (true) {
    const index = text.indexOf(quote, from);
    if (index === -1) return Math.max(1, occurrence);
    occurrence += 1;
    if (index >= selectionStart) return occurrence;
    from = index + quote.length;
  }
}
