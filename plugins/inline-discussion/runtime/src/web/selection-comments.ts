export type SelectionCommentTarget = Readonly<{
  blockId: string;
  quote?: string;
  occurrence: number;
}>;

export type SelectionCommentRecipient = 'thread-agent' | 'main-agent';

export function selectionCommentError(
  recipient: SelectionCommentRecipient,
  targetCount: number,
): string | undefined {
  return recipient === 'main-agent' && targetCount > 1
    ? 'Main agent comments support one selected block at a time. Select Thread agent to comment on all selected blocks.'
    : undefined;
}

export async function createSelectionComments(
  targets: readonly SelectionCommentTarget[],
  create: (target: SelectionCommentTarget) => Promise<void>,
): Promise<void> {
  await targets.reduce<Promise<void>>(
    (previous, target) => previous.then(() => create(target)),
    Promise.resolve(),
  );
}
