import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from '../src/server.ts';
import type { AgentFactory, ThreadAgent } from '../src/agent.ts';
import { DISCUSSION_PROJECT_SETTINGS_FILE } from '../src/tool-approvals.ts';

interface ApprovalEvent {
  id: string;
  threadId: string;
  provider: string;
  toolName: string;
  input: Record<string, unknown>;
}

function approvalAgentFactory(requestCount: { value: number }): AgentFactory {
  return (options) => {
    const agent: ThreadAgent = {
      async *send() {
        requestCount.value += 1;
        const decision = await options.requestToolApproval!({
          provider: 'claude',
          toolKey: 'mcp__gateway__notion__notion-create-pages',
          toolName: 'notion create pages',
          input: { parent: 'docs' },
          title: 'Create a Notion page?',
        });
        const text = decision.approved ? 'approved' : 'denied';
        yield { type: 'delta', text };
        yield { type: 'done', text };
      },
      async *proposeConclusion() { yield { type: 'done', text: 'done' }; },
      snapshot: () => [],
    };
    return agent;
  };
}

function scratchProject() {
  const root = mkdtempSync(join(tmpdir(), 'ind-tool-approval-server-'));
  mkdirSync(join(root, '.git', 'info'), { recursive: true });
  const docPath = join(root, 'doc.md');
  writeFileSync(docPath, '# Discussion\n\nAnchor.\n');
  return {
    root,
    docPath,
    sessionDir: join(root, 'session'),
    prefsPath: join(root, 'prefs.json'),
  };
}

async function waitForApprovalEvent(port: number): Promise<{ event: ApprovalEvent; close: () => void }> {
  const controller = new AbortController();
  const response = await fetch(`http://127.0.0.1:${port}/events`, { signal: controller.signal });
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) throw new Error('SSE stream closed before tool approval');
    buffer += decoder.decode(value);
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (frame.match(/^event: (.+)$/m)?.[1] === 'tool.approval.requested') {
        const data = frame.match(/^data: (.+)$/m)?.[1];
        if (!data) throw new Error('approval event has no data');
        return { event: JSON.parse(data) as ApprovalEvent, close: () => controller.abort() };
      }
      boundary = buffer.indexOf('\n\n');
    }
  }
}

async function waitUntil(check: () => Promise<boolean>): Promise<void> {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('condition did not become true');
}

async function waitForPendingApproval(port: number): Promise<ApprovalEvent> {
  let approval: ApprovalEvent | undefined;
  await waitUntil(async () => {
    const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as {
      pendingToolApprovals: ApprovalEvent[];
    };
    approval = boot.pendingToolApprovals[0];
    return approval !== undefined;
  });
  return approval!;
}

async function createThread(port: number): Promise<string> {
  const boot = await (await fetch(`http://127.0.0.1:${port}/api/bootstrap`)).json() as { blockIds: string[] };
  const response = await fetch(`http://127.0.0.1:${port}/api/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ anchor: { blockId: boot.blockIds[1] }, message: 'Check Notion' }),
  });
  return ((await response.json()) as { threadId: string }).threadId;
}

test('session approval resolves the pending call and auto-approves later calls to the same MCP tool', async () => {
  const project = scratchProject();
  const requestCount = { value: 0 };
  const server = await createServer({
    ...project,
    projectRoot: project.root,
    agentFactory: approvalAgentFactory(requestCount),
    shutdownOnFinish: false,
  });
  const approvalPromise = waitForApprovalEvent(server.port);
  try {
    const threadId = await createThread(server.port);
    const approval = await approvalPromise;
    assert.equal(approval.event.provider, 'claude');
    assert.equal(approval.event.toolName, 'notion create pages');
    assert.deepEqual(approval.event.input, { parent: 'docs' });

    const resolved = await fetch(`http://127.0.0.1:${server.port}/api/tool-approvals/${approval.event.id}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision: 'session' }),
    });
    assert.equal(resolved.status, 200);
    approval.close();
    await waitUntil(async () => {
      const boot = await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json() as { activeThreads: string[] };
      return !boot.activeThreads.includes(threadId);
    });

    const followUp = await fetch(`http://127.0.0.1:${server.port}/api/threads/${threadId}/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message: 'Check again' }),
    });
    assert.equal(followUp.status, 202);
    await waitUntil(async () => {
      const boot = await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json() as {
        activeThreads: string[];
        pendingToolApprovals: unknown[];
      };
      return requestCount.value === 2 && !boot.activeThreads.includes(threadId) && boot.pendingToolApprovals.length === 0;
    });
  } finally {
    await server.close();
  }
});

test('one-time approval prompts again for the next call to the same MCP tool', async () => {
  const project = scratchProject();
  const server = await createServer({
    ...project,
    projectRoot: project.root,
    agentFactory: approvalAgentFactory({ value: 0 }),
    shutdownOnFinish: false,
  });
  const firstApprovalPromise = waitForApprovalEvent(server.port);
  try {
    const threadId = await createThread(server.port);
    const firstApproval = await firstApprovalPromise;
    await fetch(`http://127.0.0.1:${server.port}/api/tool-approvals/${firstApproval.event.id}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision: 'once' }),
    });
    firstApproval.close();
    await waitUntil(async () => {
      const boot = await (await fetch(`http://127.0.0.1:${server.port}/api/bootstrap`)).json() as { activeThreads: string[] };
      return !boot.activeThreads.includes(threadId);
    });

    await fetch(`http://127.0.0.1:${server.port}/api/threads/${threadId}/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ message: 'Check again' }),
    });
    const secondApproval = await waitForPendingApproval(server.port);
    assert.notEqual(secondApproval.id, firstApproval.event.id);
    await fetch(`http://127.0.0.1:${server.port}/api/tool-approvals/${secondApproval.id}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision: 'deny' }),
    });
  } finally {
    await server.close();
  }
});

test('project approval writes the discussion settings file and local Git exclusion', async () => {
  const project = scratchProject();
  const server = await createServer({
    ...project,
    projectRoot: project.root,
    agentFactory: approvalAgentFactory({ value: 0 }),
    shutdownOnFinish: false,
  });
  const approvalPromise = waitForApprovalEvent(server.port);
  try {
    await createThread(server.port);
    const approval = await approvalPromise;
    const resolved = await fetch(`http://127.0.0.1:${server.port}/api/tool-approvals/${approval.event.id}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ decision: 'project' }),
    });
    assert.equal(resolved.status, 200);
    approval.close();
    assert.equal(existsSync(join(project.root, DISCUSSION_PROJECT_SETTINGS_FILE)), true);
    assert.match(readFileSync(join(project.root, '.git', 'info', 'exclude'), 'utf8'), /inline-discussion-settings/);
  } finally {
    await server.close();
  }
});
