export interface ComposerKeyInput {
  key: string;
  shiftKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  isComposing: boolean;
}

export type ComposerKeyAction = 'thread' | 'note' | 'none';

export type ComposerPlatform = 'macos' | 'other';

export function detectComposerPlatform(platform: string): ComposerPlatform {
  return /Mac|iPhone|iPad|iPod/.test(platform) ? 'macos' : 'other';
}

/**
 * Resolve the composer action for an Enter key event.
 *
 * Meta+Enter is the macOS note shortcut. On macOS, Control is deliberately
 * excluded so Ctrl+Enter has no inline-discussion effect.
 */
export function composerKeyAction(event: ComposerKeyInput, platform: ComposerPlatform): ComposerKeyAction {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return 'none';
  if (platform === 'macos' && event.ctrlKey) return 'none';
  return event.metaKey || event.ctrlKey ? 'note' : 'thread';
}

export function composerNoteModifierActive(
  event: Pick<ComposerKeyInput, 'metaKey' | 'ctrlKey'>,
  platform: ComposerPlatform,
): boolean {
  return platform === 'macos' ? event.metaKey && !event.ctrlKey : event.metaKey || event.ctrlKey;
}
