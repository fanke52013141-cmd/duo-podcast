let guard: (() => Promise<boolean>) | null = null;
let navigating = false;
export function registerNavigationGuard(next: () => Promise<boolean>) {
  guard = next;
  return () => { if (guard === next) guard = null; };
}
export async function navigate(action: () => void) {
  if (navigating) return;
  navigating = true;
  try { if (!guard || await guard()) action(); }
  finally { navigating = false; }
}
export function recoveryRead<T>(key: string): T | null {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null') as T | null; } catch { return null; }
}
export function recoveryWrite(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Server save remains authoritative. */ }
}
export function recoveryRemove(key: string) {
  try { localStorage.removeItem(key); } catch { /* Failed cleanup must not turn a successful server save into a failure. */ }
}

export function createSingleFlight() {
  let busy=false;
  return async (action:()=>Promise<void>)=>{
    if(busy)return;
    busy=true;
    try {await action();} finally {busy=false;}
  };
}
