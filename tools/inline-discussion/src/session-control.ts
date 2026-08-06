import { listAppServerSessions } from './main-session.ts';

const socketPath = process.env.IND_MAIN_SESSION_SOCKET;

try {
  const sessions = await listAppServerSessions(socketPath ? { socketPath } : undefined);
  process.stdout.write(`${JSON.stringify(sessions, null, 2)}\n`);
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`app-server session list failed: ${message}`);
  process.exitCode = 1;
}
