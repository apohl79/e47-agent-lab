import type { Task } from './task-types.ts';

export type PreparationScope = Pick<Task, 'scope' | 'permissions' | 'acceptance' | 'stopCondition'>;

export function proposedPreparation(task: Task): PreparationScope | undefined {
  if (task.permissions.length || !['agent', 'agent_preparable', 'verify_first'].includes(task.route)) return;
  return {
    scope: `Prepare an evidence-linked status review and proposed next steps for: ${task.title}. Use existing sources and the user note. Do not execute the underlying action or change external systems.${task.scope ? `\n\nUnderlying obligation (context only, not execution authority): ${task.scope}` : ''}`,
    permissions: ['read_sources', 'write_artifacts', 'update_task_status'],
    acceptance: `A source-linked report or draft with current status, proposed next steps, and any missing information, ready for user review. Preparation does not establish completion of the underlying obligation.${task.acceptance ? `\n\nUnderlying outcome: ${task.acceptance}` : ''}`,
    stopCondition: task.stopCondition || 'Stop for user input when needed or submit preparation for final user review; no external messages or mutations.',
  };
}
