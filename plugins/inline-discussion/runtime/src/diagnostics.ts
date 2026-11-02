export type DiagnosticValue = string | number | boolean | null | undefined;
export type DiagnosticFields = Record<string, DiagnosticValue>;

const SECRET_PATTERNS = [
  /(?:sk|rk)-[A-Za-z0-9_-]{12,}/g,
  /(?:api[_-]?key|token|password|secret)=\S+/gi,
];

/**
 * Structured, content-free diagnostics for the server log. Callers should
 * record lengths, IDs, phases, and timings — never prompts or model output.
 */
export function diagnosticJson(event: string, fields: DiagnosticFields = {}): string {
  const safe: Record<string, DiagnosticValue> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined) continue;
    safe[key] = typeof value === 'string' ? sanitize(value) : value;
  }
  return JSON.stringify({
    ts: new Date().toISOString(),
    component: 'inline-discussion',
    event,
    ...safe,
  });
}

export function logDiagnostic(event: string, fields: DiagnosticFields = {}): void {
  console.error(diagnosticJson(event, fields));
}

function sanitize(value: string): string {
  let result = value.replace(/[\r\n]+/g, ' ').slice(0, 500);
  for (const pattern of SECRET_PATTERNS) result = result.replace(pattern, '[REDACTED]');
  return result;
}
