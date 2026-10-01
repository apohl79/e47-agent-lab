import type { Thread, ThreadRecipient } from './types.ts';

export function recoverMainAgentThreadAnchor(
  thread: Thread,
  previousBlockIds: readonly string[],
  nextBlockIds: readonly string[],
): Thread {
  const previousIndex = previousBlockIds.indexOf(thread.anchor.blockId);
  const nearestSurvivingBlock = previousBlockIds.reduce<Readonly<{ id: string; index: number }> | undefined>(
    (nearest, blockId, index) => {
      const distance = Math.abs(index - previousIndex);
      const nearestDistance = nearest ? Math.abs(nearest.index - previousIndex) : Infinity;
      const followingTie = nearest !== undefined
        && distance === nearestDistance
        && index > nearest.index;
      return nextBlockIds.includes(blockId)
        && blockId !== thread.anchor.blockId
        && (distance < nearestDistance || followingTie)
        ? { id: blockId, index }
        : nearest;
    },
    undefined,
  );
  const replacement = thread.recipient === 'main-agent'
    && !nextBlockIds.includes(thread.anchor.blockId)
    && previousIndex >= 0
    && nextBlockIds.length > 0
    ? nearestSurvivingBlock?.id ?? nextBlockIds[Math.min(previousIndex, nextBlockIds.length - 1)]
    : undefined;
  return replacement
    ? { ...thread, anchor: { ...thread.anchor, blockId: replacement } }
    : thread;
}

export function canFinishThread(recipient: ThreadRecipient | undefined): boolean {
  return recipient !== 'main-agent';
}
