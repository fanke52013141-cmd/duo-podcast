/* ============================================================================
   DuoCast 前端 · Zustand stores（05 §5：仅存 UI 态与服务端派生结果，前端不自算）
   projectStore：当前工程 / 当前视图
   serviceStore：能力快照（UI 门控，01 §1.6.4）
   ========================================================================== */
import { create } from 'zustand';
import { navigate } from '../lib/navigation';
import type { Capabilities, StageId } from '../lib/types';

export type View = 'home' | StageId | 'assets' | 'services' | 'tasks' | 'motionPacks';

interface ProjectState {
  currentProjectId: string | null;
  view: View;
  /** openProject 可带目标阶段：首页「继续制作」直达工程当前阶段（第四轮实测修复）。 */
  openProject: (id: string, view?: View) => void;
  setView: (view: View) => void;
  leaveProject: () => void;
}

// ---- 会话持久化：无路由 SPA 刷新即回首页、丢失工程上下文（第四轮实测发现）。
// 只存 {currentProjectId, view}；损坏或缺失时静默回首页。
const SESSION_KEY = 'duocast.session.v1';

export function loadSession(): { currentProjectId: string; view: View } | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw) as { currentProjectId?: string; view?: View };
    if (!saved.currentProjectId || typeof saved.currentProjectId !== 'string') return null;
    return { currentProjectId: saved.currentProjectId, view: (saved.view ?? 'create') as View };
  } catch {
    return null;
  }
}

export function saveSession(currentProjectId: string | null, view: View): void {
  try {
    if (!currentProjectId || view === 'home') localStorage.removeItem(SESSION_KEY);
    else localStorage.setItem(SESSION_KEY, JSON.stringify({ currentProjectId, view }));
  } catch { /* 隐私模式等场景下忽略 */ }
}

// 初始化时就恢复会话：等 useEffect 再恢复会让首帧先渲染首页，
// 刷新工程页时闪一下首页并多发一轮请求（第六轮实测）。
const restoredSession = loadSession();

export const useProjectStore = create<ProjectState>((set) => ({
  currentProjectId: restoredSession?.currentProjectId ?? null,
  view: restoredSession?.view ?? 'home',
  openProject: (id, view) => { void navigate(() => set({ currentProjectId: id, view: view ?? 'create' })); },
  setView: (view) => { void navigate(() => set({ view })); },
  leaveProject: () => { void navigate(() => set({ currentProjectId: null, view: 'home' })); },
}));

interface ServiceState {
  capabilities: Capabilities | null;
  setCapabilities: (caps: Capabilities) => void;
}

export const useServiceStore = create<ServiceState>((set) => ({
  capabilities: null,
  setCapabilities: (caps) => set({ capabilities: caps }),
}));
