export function tabDestination(ids: string[], current: string, key: string): string | null {
  if (!ids.length) return null;
  if (key === 'Home') return ids[0];
  if (key === 'End') return ids[ids.length-1];
  if (!['ArrowLeft','ArrowRight'].includes(key)) return null;
  const index=Math.max(0,ids.indexOf(current));
  return ids[(index+(key==='ArrowRight'?1:ids.length-1))%ids.length];
}
