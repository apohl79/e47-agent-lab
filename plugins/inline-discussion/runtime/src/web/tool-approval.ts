import type { ModalChoiceOptions } from './modal.ts';

export interface ToolApprovalPrompt {
  id: string;
  threadId: string;
  provider: 'claude' | 'codex';
  toolName: string;
  input: Record<string, unknown>;
  title?: string;
  description?: string;
}

export function toolApprovalModalOptions(request: ToolApprovalPrompt): ModalChoiceOptions {
  const input = JSON.stringify(request.input);
  const body = [
    request.title ?? `The ${request.provider} thread agent wants to call ${request.toolName}.`,
    request.description,
    input && input !== '{}' ? `Parameters: ${input.length > 2000 ? `${input.slice(0, 2000)}…` : input}` : undefined,
    'Session approval lasts until this discussion server exits. Project approval is saved only in this checkout.',
  ].filter((part): part is string => Boolean(part)).join('\n\n');
  return {
    title: 'Approve MCP tool call',
    body,
    options: [
      { label: 'Deny', value: 'deny', variant: 'danger' },
      { label: 'Always for project', value: 'project' },
      { label: 'Allow for session', value: 'session' },
      { label: 'Allow once', value: 'once' },
    ],
    cancelValue: 'deny',
  };
}
