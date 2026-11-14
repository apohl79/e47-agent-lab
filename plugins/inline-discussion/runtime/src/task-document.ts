import { readFileSync, realpathSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve } from 'node:path';
import { JSDOM } from 'jsdom';
import { parseDoc } from './markdown.ts';
import { readTaskRegister, type Task } from './task-register.ts';
import type { TaskSnapshot } from './task-types.ts';
export type { TaskSnapshot } from './task-types.ts';

export function documentTaskBinding(markdown: string): { register: string; ids: string[] } | null {
  const blocks = parseDoc(markdown).blocks;
  const paths = blocks.filter((block) => block.kind === 'html')
    .map((block) => block.markdown.trim().match(/^<!-- task-register: ([^\r\n]+) -->$/)?.[1])
    .filter((value): value is string => value !== undefined);
  if (!paths.length) return null;
  if (paths.length !== 1) throw new Error('exactly one task-register declaration required');
  const ids: string[] = [];
  for (const block of blocks.filter((entry) => entry.kind === 'list')) {
    const dom = new JSDOM(block.html);
    const walker = dom.window.document.createTreeWalker(dom.window.document.body, 128);
    while (walker.nextNode()) {
      const id = walker.currentNode.textContent?.trim().match(/^task-id: ([A-Za-z0-9][A-Za-z0-9_-]{0,79})$/)?.[1];
      if (id && walker.currentNode.parentElement?.closest('li')?.querySelector('input[type="checkbox"]')) ids.push(id);
    }
    dom.window.close();
  }
  if (new Set(ids).size !== ids.length) throw new Error('duplicate task ID in document');
  return { register: paths[0]!, ids };
}
export function documentTaskRegisterPath(documentPath: string, root: string): string | null {
  const binding = documentTaskBinding(readFileSync(documentPath, 'utf8'));
  if (!binding) return null;
  if (isAbsolute(binding.register) || !binding.register.endsWith('.json')) throw new Error('task register must be a relative JSON path');
  const path = realpathSync(resolve(dirname(documentPath), binding.register));
  const within = relative(realpathSync(root), path);
  if (within.startsWith('..') || isAbsolute(within)) throw new Error('task register is outside project root');
  return path;
}
export function documentTaskSnapshot(documentPath: string, root: string): TaskSnapshot {
  try {
    const binding = documentTaskBinding(readFileSync(documentPath, 'utf8'));
    if (!binding) return { enabled: false, revision: 0, tasks: [] };
    const path = documentTaskRegisterPath(documentPath, root)!;
    const register = readTaskRegister(path);
    const tasks = binding.ids.map((id) => {
      const task = register.tasks.find((entry) => entry.id === id);
      if (!task) throw new Error(`task ${id} is missing from register`);
      return task;
    });
    return { enabled: true, revision: register.revision, tasks };
  } catch (error) {
    return { enabled: true, revision: -1, tasks: [], error: error instanceof Error ? error.message : String(error) };
  }
}
