import { createInterface } from 'node:readline';
import { existsSync, mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from './server.js';
import { discoverAppServerInferenceCatalog, xedocAgentFactory } from './agent.ts';
import { readCodexSessionInferenceSettings } from './transcript.ts';

const lines = createInterface({ input: process.stdin });
const line = await new Promise<string>((accept, reject) => {
  const timeout = setTimeout(() => reject(new Error('Panel launch input timed out')), 10_000);
  lines.once('line', (value) => { clearTimeout(timeout); accept(value); });
  lines.once('close', () => { clearTimeout(timeout); reject(new Error('Missing panel launch input')); });
});
lines.close();
const input = JSON.parse(line) as Record<string, unknown>;
for (const key of ['threadId', 'projectRoot', 'target', 'sessionSocket', 'home', 'sessionDir']) {
  if (typeof input[key] !== 'string' || !input[key]) throw new Error(`Missing ${key}`);
}
if (input.protocol !== 'xedoc.plugin-panel/v1' || input.panelId !== 'discussion') {
  throw new Error('Unsupported panel protocol');
}
const docPath = input.target as string;
if (!isAbsolute(docPath) || !/\.(md|markdown)$/i.test(docPath) || !statSync(docPath).isFile()) {
  throw new Error('Discussion target must be an absolute Markdown file');
}
const home = input.home as string;
process.env.XEDOC_HOME = home;
process.env.XEDOC_APP_SERVER_SOCKET = input.sessionSocket as string;
const threadId = input.threadId as string;
const sessionDir = input.sessionDir as string;
const projectRoot = input.projectRoot as string;
mkdirSync(sessionDir, { recursive: true, mode: 0o700 });
function findRollout(root: string): string | undefined {
  if (!existsSync(root)) return undefined;
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) {
      const found = findRollout(path);
      if (found) return found;
    } else if (entry.name.endsWith(`${threadId}.jsonl`)) return path;
  }
  return undefined;
}
let mainJsonlPath = typeof input.mainJsonlPath === 'string' && existsSync(input.mainJsonlPath)
  ? input.mainJsonlPath
  : findRollout(join(home, 'sessions'));
if (!mainJsonlPath) {
  mainJsonlPath = join(sessionDir, 'main-session.jsonl');
  writeFileSync(mainJsonlPath, '', { mode: 0o600 });
}
const inferenceCatalog = await discoverAppServerInferenceCatalog({ cwd: projectRoot, harness: 'xedoc' });
const server = await createServer({
  docPath,
  projectRoot,
  sessionDir,
  staticDir: resolve(dirname(fileURLToPath(import.meta.url))),
  mainJsonlPath,
  mainSessionId: threadId,
  mainSessionSocket: input.sessionSocket as string,
  mainSessionHarness: 'xedoc',
  hosted: true,
  inferenceCatalog,
  initialInferenceSettings: readCodexSessionInferenceSettings(mainJsonlPath, inferenceCatalog),
  agentFactory: xedocAgentFactory({ cwd: projectRoot }),
});
const url = `http://127.0.0.1:${server.port}/`;
writeFileSync(join(sessionDir, 'url.txt'), url, { mode: 0o600 });
console.log(JSON.stringify({ url, title: basename(docPath) }));
const shutdown = async (): Promise<void> => { await server.close(); process.exit(0); };
process.on('SIGINT', () => { void shutdown(); });
process.on('SIGTERM', () => { void shutdown(); });
