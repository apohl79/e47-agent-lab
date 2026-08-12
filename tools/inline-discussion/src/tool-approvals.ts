import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';

export const DISCUSSION_PROJECT_SETTINGS_FILE = '.inline-discussion-settings.json';
const SETTINGS_VERSION = 1;

export type ToolApprovalScope = 'once' | 'session' | 'project';

export interface DiscussionProjectSettings {
  version: 1;
  mcpApprovedTools: string[];
}

function emptySettings(): DiscussionProjectSettings {
  return { version: SETTINGS_VERSION, mcpApprovedTools: [] };
}

function normalizedToolNames(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((entry): entry is string => typeof entry === 'string' && entry.trim().length > 0))]
    .map((entry) => entry.trim())
    .sort();
}

export function readDiscussionProjectSettings(projectRoot: string): DiscussionProjectSettings {
  const path = join(projectRoot, DISCUSSION_PROJECT_SETTINGS_FILE);
  if (!existsSync(path)) return emptySettings();
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>;
    return {
      version: SETTINGS_VERSION,
      mcpApprovedTools: normalizedToolNames(parsed['mcpApprovedTools']),
    };
  } catch {
    return emptySettings();
  }
}

function resolveCommonGitDir(projectRoot: string): string | null {
  const dotGit = join(projectRoot, '.git');
  if (!existsSync(dotGit)) return null;
  if (statSync(dotGit).isDirectory()) return dotGit;

  const match = readFileSync(dotGit, 'utf8').trim().match(/^gitdir:\s*(.+)$/i);
  if (!match?.[1]) return null;
  const gitDir = resolve(projectRoot, match[1]);
  const commonDirPath = join(gitDir, 'commondir');
  if (!existsSync(commonDirPath)) return gitDir;
  return resolve(gitDir, readFileSync(commonDirPath, 'utf8').trim());
}

export function ensureDiscussionSettingsGitExcluded(projectRoot: string): void {
  const gitDir = resolveCommonGitDir(projectRoot);
  if (!gitDir) return;
  const excludePath = join(gitDir, 'info', 'exclude');
  mkdirSync(dirname(excludePath), { recursive: true });
  const current = existsSync(excludePath) ? readFileSync(excludePath, 'utf8') : '';
  const rule = `/${DISCUSSION_PROJECT_SETTINGS_FILE}`;
  if (current.split(/\r?\n/).includes(rule)) return;
  const prefix = current.length === 0 || current.endsWith('\n') ? current : `${current}\n`;
  writeFileSync(excludePath, `${prefix}${rule}\n`);
}

export function persistMcpToolApproval(projectRoot: string, toolKey: string): DiscussionProjectSettings {
  const current = readDiscussionProjectSettings(projectRoot);
  const next: DiscussionProjectSettings = {
    version: SETTINGS_VERSION,
    mcpApprovedTools: normalizedToolNames([...current.mcpApprovedTools, toolKey]),
  };
  // Install the repository-local exclusion before creating the settings file,
  // so a failure cannot leave a permanent approval visible to Git.
  ensureDiscussionSettingsGitExcluded(projectRoot);
  const path = join(projectRoot, DISCUSSION_PROJECT_SETTINGS_FILE);
  const temporaryPath = `${path}.tmp-${process.pid}`;
  writeFileSync(temporaryPath, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
  renameSync(temporaryPath, path);
  chmodSync(path, 0o600);
  return next;
}
