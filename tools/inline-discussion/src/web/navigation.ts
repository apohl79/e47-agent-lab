export function scrollToFragment(hash: string, document: Document): boolean {
  const rawHash = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!rawHash) return false;
  let id: string;
  try {
    id = decodeURIComponent(rawHash);
  } catch {
    return false;
  }
  const target = document.getElementById(id);
  if (!target) return false;
  target.scrollIntoView({ block: 'start' });
  return true;
}
