import type { Thread } from '../types.ts';

function sameAnchor(left: Thread, right: Thread): boolean {
  return left.anchor.blockId === right.anchor.blockId &&
    left.anchor.quote === right.anchor.quote &&
    (left.anchor.occurrence ?? 1) === (right.anchor.occurrence ?? 1);
}

function sameMessages(left: Thread, right: Thread): boolean {
  return left.messages.length === right.messages.length &&
    left.messages.every((message, index) => {
      const other = right.messages[index];
      return other?.role === message.role && other.text === message.text;
    });
}

export function isArchivedThreadDuplicate(archived: Thread, liveThreads: Iterable<Thread>): boolean {
  return [...liveThreads].some((live) =>
    live.status === 'closed' &&
    live.kind === archived.kind &&
    sameAnchor(live, archived) &&
    (live.conclusion ?? '') === (archived.conclusion ?? '') &&
    sameMessages(live, archived),
  );
}
