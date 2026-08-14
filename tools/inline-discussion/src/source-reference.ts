import type { SourceRange } from './types.ts';

export type ParsedSourceReference = Readonly<{
  pathPart: string;
  range: SourceRange | null;
}>;

type RangeParser = (reference: string) => ParsedSourceReference | null;

const validRange = (range: SourceRange): boolean =>
  range.startLine >= 1 &&
  range.endLine >= range.startLine &&
  (range.startColumn === undefined || range.startColumn >= 1) &&
  (range.endColumn === undefined || range.endColumn >= 1) &&
  (range.startLine !== range.endLine ||
    range.startColumn === undefined ||
    range.endColumn === undefined ||
    range.endColumn >= range.startColumn);

const characterRange: RangeParser = (reference) => {
  const match = reference.match(/^(.*):(\d+):(\d+)-(\d+):(\d+)$/);
  const range = match === null ? null : {
    startLine: Number.parseInt(match[2]!, 10),
    startColumn: Number.parseInt(match[3]!, 10),
    endLine: Number.parseInt(match[4]!, 10),
    endColumn: Number.parseInt(match[5]!, 10),
  };
  return match !== null && match[1] && range !== null && validRange(range)
    ? { pathPart: match[1], range }
    : null;
};

const lineRange: RangeParser = (reference) => {
  const match = reference.match(/^(.*):(\d+)-(\d+)$/);
  const range = match === null ? null : {
    startLine: Number.parseInt(match[2]!, 10),
    endLine: Number.parseInt(match[3]!, 10),
  };
  return match !== null && match[1] && range !== null && validRange(range)
    ? { pathPart: match[1], range }
    : null;
};

const singleLine: RangeParser = (reference) => {
  const match = reference.match(/^(.*):(\d+)$/);
  const line = match === null ? 0 : Number.parseInt(match[2]!, 10);
  return match !== null && match[1] && line >= 1
    ? { pathPart: match[1], range: { startLine: line, endLine: line } }
    : null;
};

export const parseSourceReference = (reference: string): ParsedSourceReference => {
  const parsedCharacterRange = characterRange(reference);
  if (/^(.*):(\d+):(\d+)-(\d+):(\d+)$/.test(reference)) {
    return parsedCharacterRange ?? { pathPart: reference, range: null };
  }
  return [lineRange, singleLine]
    .reduce<ParsedSourceReference | null>((parsed, parser) => parsed ?? parser(reference), null)
    ?? { pathPart: reference, range: null };
};

export const formatSourceRange = (range: SourceRange | null): string =>
  range === null
    ? ''
    : range.startColumn !== undefined && range.endColumn !== undefined
      ? `:${range.startLine}:${range.startColumn}-${range.endLine}:${range.endColumn}`
      : range.startLine === range.endLine
        ? `:${range.startLine}`
        : `:${range.startLine}-${range.endLine}`;

export function selectedSourceText(source: string, range: SourceRange | null): string | null {
  if (range === null) return null;
  const lines = source.split(/\r?\n/);
  if (range.endLine > lines.length) return null;
  const selected = lines.slice(range.startLine - 1, range.endLine);
  if (range.startColumn === undefined || range.endColumn === undefined) return selected.join('\n');
  if (selected.length === 1) {
    return selected[0]!.slice(range.startColumn - 1, range.endColumn);
  }
  return [
    selected[0]!.slice(range.startColumn - 1),
    ...selected.slice(1, -1),
    selected[selected.length - 1]!.slice(0, range.endColumn),
  ].join('\n');
}
