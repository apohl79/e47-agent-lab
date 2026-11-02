export function updateApplyCount(buttons: readonly HTMLButtonElement[], count: number): void {
  const visible = count > 0;
  const label = visible
    ? `Apply ${count} ${count === 1 ? 'note or thread' : 'notes or threads'}`
    : 'Apply';

  for (const button of buttons) {
    const badge = button.querySelector<HTMLElement>('.apply-count');
    if (!badge) continue;
    badge.textContent = String(count);
    badge.hidden = !visible;
    button.setAttribute('aria-label', label);
  }
}
