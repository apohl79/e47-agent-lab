export type BlockKind = 'heading' | 'paragraph' | 'code' | 'blockquote' | 'list' | 'table' | 'hr' | 'html';

export interface Block {
  id: string;
  kind: BlockKind;
  markdown: string;
  html: string;
}

export interface Anchor {
  blockId: string;
  quote?: string;
  occurrence?: number;
}

export type ThreadStatus = 'open' | 'closed' | 'archived';

export interface ThreadMessage {
  role: 'user' | 'assistant';
  text: string;
  ts: string;
}

export type AgentActivityKind = 'commentary' | 'reasoning' | 'tool';

export interface AgentActivity {
  kind: AgentActivityKind;
  title: string;
  text: string;
}

export type InferenceSettings = Readonly<{
  provider: string;
  model: string;
  reasoningEffort: string;
}>;

export type InferenceReasoningOption = Readonly<{
  reasoningEffort: string;
  description: string;
}>;

export type InferenceModelOption = Readonly<{
  provider: string;
  model: string;
  displayName: string;
  description: string;
  hidden: boolean;
  isDefault: boolean;
  defaultReasoningEffort: string;
  supportedReasoningEfforts: readonly InferenceReasoningOption[];
}>;

export type InferenceCatalog = Readonly<{
  models: readonly InferenceModelOption[];
  defaultSettings: InferenceSettings;
}>;

export type ThreadKind = 'thread' | 'note';

export interface Thread {
  id: string;
  kind: ThreadKind;
  /** Absolute Markdown document path that owns this annotation. */
  documentPath?: string;
  anchor: Anchor;
  status: ThreadStatus;
  messages: ThreadMessage[];
  conclusion?: string;
  createdAt: string;
  closedAt?: string;
  closedBy?: 'user' | 'auto';
  colorIndex?: number;
  inferenceSettings?: InferenceSettings;
}

// Highlights are pure visual session markers. They live alongside threads
// in server state but they are NOT threads — they have no transcript, no
// agent, no conclusion, and no archive. The user converts a highlight to
// a note or thread when they want to attach content to it.
export interface Highlight {
  id: string;
  /** Absolute Markdown document path that owns this visual marker. */
  documentPath?: string;
  anchor: Anchor;
  createdAt: string;
  colorIndex?: number;
}

export interface LiveSessionSnapshot {
  version: 1;
  docPath: string;
  threads: Thread[];
  highlights: Highlight[];
  nextThreadSeq: number;
  nextHighlightSeq: number;
}

export interface FinishResult {
  mode: 'finish';
  docPath: string;
  conclusions: Array<{
    threadId: string;
    anchor: string;           // human label: quote or "entire block — <heading>"
    conclusion: string;
    closedBy: 'user' | 'auto';
  }>;
  threadCount: number;        // new this session
  archivedThreadCount: number;
  finishedAt: string;
}

export interface ApplyResult {
  mode: 'apply';
  applyIndex: number;
  docPath: string;
  /** Main document plus every Markdown document containing applied annotations. */
  documentPaths: string[];
  conclusions: FinishResult['conclusions'];
  threadCount: number;
  archivedThreadCount: number;
  finishedAt: string;
}

export interface PauseResult {
  mode: 'pause';
  docPath: string;
  threadCount: number;
  highlightCount: number;
  pausedAt: string;
}

export interface ApplyProgress {
  status: string;
  percent: number | null;
  current?: number;
  total?: number;
  updatedAt: string;
}

export interface ApplyTask {
  id: string;
  label: string;
  state: 'active' | 'done' | 'error';
  updatedAt: string;
}
