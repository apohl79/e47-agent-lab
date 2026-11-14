import { readFileSync } from 'node:fs';
import { initializeTaskRegister, readTaskRegister, writeTaskCommand, type TaskCommand } from './task-register.ts';

try {
  const [action, path, input] = process.argv.slice(2);
  if (!path || !['init', 'read', 'update'].includes(action ?? '')) {
    throw new Error('Usage: node task-control.js init|read|update REGISTER [COMMAND.json|-]. Updates run as agent; approve tasks in the discussion UI.');
  }
  if (action === 'init') initializeTaskRegister(path);
  if (action === 'update') {
    if (!input) throw new Error('command JSON file or - required');
    writeTaskCommand(path, JSON.parse(readFileSync(input === '-' ? 0 : input, 'utf8')) as TaskCommand, 'agent');
  }
  console.log(JSON.stringify(readTaskRegister(path), null, 2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
