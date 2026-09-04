import { listAppServerSessions, type AppServerHarness } from './main-session.ts';

const socketPath = process.env.IND_MAIN_SESSION_SOCKET;
const requestedHarness = process.env.IND_MAIN_SESSION_HARNESS;
const harness: AppServerHarness = requestedHarness === 'xedoc' ? 'xedoc' : 'codex';

try {
  const sessions = await listAppServerSessions({ harness, ...(socketPath ? { socketPath } : {}) });
  process.stdout.write(`${JSON.stringify(sessions, null, 2)}\n`);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`app-server session list failed: ${message}`);
  process.exitCode = 1;
}
