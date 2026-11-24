import { createInterface } from 'node:readline';
import { existsSync, mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer, type ServerHandle } from './server.js';
import { parseInferenceModels, xedocAgentFactory } from './agent.ts';
import type { InferenceCatalog, InferenceSettings } from './types.ts';

interface Instance {
  server: ServerHandle;
  target: unknown;
  url: string;
  title: string;
}

const protocol = 'xedoc.plugin-panel-runtime/v1';
const instances = new Map<string, Instance>();
const servers = new Set<ServerHandle>();
const lines = createInterface({ input: process.stdin });
let stopping = false;

async function shutdown(): Promise<void> {
  if (stopping) return;
  stopping = true;
  lines.close();
  await Promise.all([...servers].map((server) => server.close()));
  process.exit(0);
}
process.on('SIGINT', () => { void shutdown(); });
process.on('SIGTERM', () => { void shutdown(); });

async function launch(input: Record<string, unknown>, onShutdown?: () => void): Promise<Instance> {
  for (const key of ['threadId', 'projectRoot', 'target', 'sessionSocket', 'home', 'sessionDir']) {
    if (typeof input[key] !== 'string' || !input[key]) throw new Error(`Missing ${key}`);
  }
  if (input.protocol !== 'xedoc.plugin-panel/v1' || input.panelId !== 'discussion') {
    throw new Error('Unsupported panel protocol');
  }
  const inference = input.inference;
  if (!inference || typeof inference !== 'object' || Array.isArray(inference)) {
    throw new Error('Missing or malformed hosted inference snapshot');
  }
  const snapshot = inference as Record<string, unknown>;
  if (snapshot.threadId !== input.threadId || typeof snapshot.model !== 'string' || !snapshot.model
    || typeof snapshot.modelProvider !== 'string' || !snapshot.modelProvider
    || (snapshot.reasoningEffort !== null && typeof snapshot.reasoningEffort !== 'string')
    || !Array.isArray(snapshot.models)) {
    throw new Error('Invalid hosted inference snapshot or thread mismatch');
  }
  const suppliedModels = snapshot.models;
  const models = parseInferenceModels({ data: suppliedModels });
  if (models.length === 0 || models.length !== suppliedModels.length
    || models.some((model, index) => {
      const raw = suppliedModels[index] as Record<string, unknown>;
      return !Array.isArray(raw.supportedReasoningEfforts)
        || model.supportedReasoningEfforts.length !== raw.supportedReasoningEfforts.length
        || model.supportedReasoningEfforts.some((option) => !option.reasoningEffort)
        || !model.supportedReasoningEfforts.some((option) => option.reasoningEffort === model.defaultReasoningEffort);
    })) {
    throw new Error('Malformed hosted inference model catalog');
  }
  const selectedModel = models.find((model) =>
    model.provider === snapshot.modelProvider && model.model === snapshot.model);
  if (!selectedModel) throw new Error('Hosted inference provider/model is not in the supplied catalog');
  const reasoningEffort = typeof snapshot.reasoningEffort === 'string'
    ? snapshot.reasoningEffort : selectedModel.defaultReasoningEffort;
  if (!selectedModel.supportedReasoningEfforts.some((option) => option.reasoningEffort === reasoningEffort)) {
    throw new Error('Hosted inference reasoning effort is not supported by the selected model');
  }
  const initialInferenceSettings: InferenceSettings = Object.freeze({
    provider: selectedModel.provider, model: selectedModel.model, reasoningEffort,
  });
  const inferenceCatalog: InferenceCatalog = Object.freeze({
    models: Object.freeze(models), defaultSettings: initialInferenceSettings,
  });
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
    initialInferenceSettings,
    agentFactory: xedocAgentFactory({ cwd: projectRoot }),
    onShutdown,
  });
  if (stopping) { await server.close(); throw new Error('Runtime is stopping'); }
  const url = `http://127.0.0.1:${server.port}/`;
  try {
    writeFileSync(join(sessionDir, 'url.txt'), url, { mode: 0o600 });
  } catch (error) {
    await server.close();
    throw error;
  }
  servers.add(server);
  return { server, target: input.target, url, title: basename(docPath) };
}

if (process.env.XEDOC_PLUGIN_PANEL_REUSE === 'session') {
  let scope: unknown[] | undefined;
  console.log(JSON.stringify({ protocol, ready: true }));
  for await (const line of lines) {
    let instanceId: unknown;
    try {
      const command = JSON.parse(line) as Record<string, unknown>;
      instanceId = command.instanceId;
      if (command.protocol !== protocol) throw new Error('Unsupported runtime protocol');
      if (command.op === 'status') {
        console.log(JSON.stringify({ protocol, instances: [...instances.keys()] }));
        continue;
      }
      if (typeof instanceId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(instanceId)) {
        throw new Error('Invalid runtime instanceId');
      }
      if (command.op === 'close') {
        const instance = instances.get(instanceId);
        instances.delete(instanceId);
        if (instance) {
          await instance.server.close();
          servers.delete(instance.server);
        }
        console.log(JSON.stringify({ protocol, instanceId, ok: true }));
        continue;
      }
      if (command.op !== 'open' || !command.context || typeof command.context !== 'object'
        || Array.isArray(command.context)) throw new Error('Invalid runtime command');
      const input = command.context as Record<string, unknown>;
      const nextScope = ['threadId', 'projectRoot', 'home', 'pluginId', 'panelId']
        .map((key) => input[key]);
      if (scope && scope.some((value, index) => value !== nextScope[index])) {
        throw new Error('Runtime belongs to a different session');
      }
      let instance = instances.get(instanceId);
      if (instance && instance.target !== input.target) throw new Error('Runtime instance target changed');
      if (!instance) {
        if (instances.size >= 16) throw new Error('Runtime instance limit reached');
        const id = instanceId;
        instance = await launch(input, () => {
          const closed = instances.get(id);
          if (closed) servers.delete(closed.server);
          instances.delete(id);
        });
        instances.set(id, instance);
        scope = nextScope;
      }
      console.log(JSON.stringify({ protocol, instanceId, url: instance.url, title: instance.title }));
    } catch (error) {
      console.log(JSON.stringify({ protocol, instanceId, error: error instanceof Error ? error.message : String(error) }));
    }
  }
  await shutdown();
} else {
  const line = await new Promise<string>((accept, reject) => {
    const timeout = setTimeout(() => reject(new Error('Panel launch input timed out')), 10_000);
    lines.once('line', (value) => { clearTimeout(timeout); accept(value); });
    lines.once('close', () => { clearTimeout(timeout); reject(new Error('Missing panel launch input')); });
  });
  lines.close();
  const instance = await launch(JSON.parse(line) as Record<string, unknown>);
  console.log(JSON.stringify({ url: instance.url, title: instance.title }));
}
