import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  DISCUSSION_PROJECT_SETTINGS_FILE,
  persistMcpToolApproval,
  readDiscussionProjectSettings,
} from '../src/tool-approvals.ts';

test('project MCP approvals persist locally and add an idempotent Git info/exclude rule', () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-tool-approval-settings-'));
  mkdirSync(join(root, '.git', 'info'), { recursive: true });
  writeFileSync(join(root, '.git', 'info', 'exclude'), 'existing.local\n');

  assert.deepEqual(readDiscussionProjectSettings(root).mcpApprovedTools, []);
  persistMcpToolApproval(root, 'mcp__gateway__notion-search');
  persistMcpToolApproval(root, 'mcp__gateway__notion-search');

  assert.deepEqual(readDiscussionProjectSettings(root).mcpApprovedTools, ['mcp__gateway__notion-search']);
  const persisted = JSON.parse(readFileSync(join(root, DISCUSSION_PROJECT_SETTINGS_FILE), 'utf8')) as {
    version: number;
    mcpApprovedTools: string[];
  };
  assert.equal(persisted.version, 1);
  assert.deepEqual(persisted.mcpApprovedTools, ['mcp__gateway__notion-search']);
  assert.equal(
    readFileSync(join(root, '.git', 'info', 'exclude'), 'utf8'),
    `existing.local\n/${DISCUSSION_PROJECT_SETTINGS_FILE}\n`,
  );
});

test('linked worktrees place the exclusion in the common Git directory', () => {
  const root = mkdtempSync(join(tmpdir(), 'ind-tool-approval-worktree-'));
  const commonGit = join(root, 'common.git');
  const worktreeGit = join(commonGit, 'worktrees', 'discussion');
  const checkout = join(root, 'checkout');
  mkdirSync(worktreeGit, { recursive: true });
  mkdirSync(checkout);
  writeFileSync(join(checkout, '.git'), `gitdir: ${worktreeGit}\n`);
  writeFileSync(join(worktreeGit, 'commondir'), '../..\n');

  persistMcpToolApproval(checkout, 'mcp__docs__search');

  assert.equal(
    readFileSync(join(commonGit, 'info', 'exclude'), 'utf8'),
    `/${DISCUSSION_PROJECT_SETTINGS_FILE}\n`,
  );
});
