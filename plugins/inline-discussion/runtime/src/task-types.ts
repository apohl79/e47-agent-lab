export const TASK_STATES = ['identified', 'awaiting_approval', 'ready', 'in_progress', 'needs_input', 'blocked', 'ready_for_review', 'completed', 'cancelled'] as const;
export type TaskState = typeof TASK_STATES[number];
export type TaskRoute = 'agent' | 'agent_preparable' | 'human_delegable' | 'you' | 'verify_first';
export interface Task {
  id: string;
  title: string;
  source: string;
  owner: string;
  state: TaskState;
  route: TaskRoute;
  classificationConfidence: number;
  readinessConfidence: number;
  humanDelegable: boolean;
  candidateDelegate: string;
  handoff: 'none' | 'proposed' | 'accepted';
  scope: string;
  permissions: string[];
  acceptance: string;
  stopCondition: string;
  dependencies: string[];
  firstSeen: string;
  updatedAt: string;
  revision: number;
  progress: string;
  question: string;
  session: string;
  deliverable: string;
  evidence: string;
  approval?: string;
  history: Array<{ at: string; actor: string; state: TaskState; note: string }>;
}
export interface TaskRegister { version: 1; revision: number; tasks: Task[] }
export interface TaskCommand {
  action: 'create' | 'classify' | 'approve' | 'start' | 'progress' | 'needs_input' | 'block' | 'submit' | 'resume' | 'request_changes' | 'accept' | 'cancel';
  expectedRevision: number;
  id?: string;
  note?: string;
  task?: Partial<Task>;
}
export interface TaskSnapshot {
  enabled: boolean;
  revision: number;
  tasks: Task[];
  error?: string;
}
