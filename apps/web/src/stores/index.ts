/* ============================================================================
   DuoCast 前端 · Zustand stores（05 §5：仅存 UI 态与服务端派生结果，前端不自算）
   projectStore：当前工程 / 当前视图 / 选中草稿版本
   jobStore：事件序号推进与活动任务 id 集合
   serviceStore：能力快照（UI 门控，01 §1.6.4）
   ========================================================================== */
import { create } from 'zustand';
import type { Capabilities, JobStatus, StageId } from '../lib/types';

export type View = 'home' | StageId | 'assets' | 'services' | 'tasks';

interface ProjectState {
  currentProjectId: string | null;
  view: View;
  selectedRevisionId: string | null;
  openProject: (id: string) => void;
  setView: (view: View) => void;
  setSelectedRevision: (id: string | null) => void;
  leaveProject: () => void;
}

export const useProjectStore = create<ProjectState>((set) => ({
  currentProjectId: null,
  view: 'home',
  selectedRevisionId: null,
  openProject: (id) => set({ currentProjectId: id, view: 'create', selectedRevisionId: null }),
  setView: (view) => set({ view }),
  setSelectedRevision: (id) => set({ selectedRevisionId: id }),
  leaveProject: () => set({ currentProjectId: null, view: 'home', selectedRevisionId: null }),
}));

interface JobState {
  lastSeq: number;
  activeJobIds: string[];
  noteSeq: (seq: number) => void;
  setActiveJobs: (ids: string[]) => void;
  touch: (jobId: string, status: JobStatus) => void;
}

export const useJobStore = create<JobState>((set) => ({
  lastSeq: 0,
  activeJobIds: [],
  noteSeq: (seq) => set((s) => (seq > s.lastSeq ? { lastSeq: seq } : s)),
  setActiveJobs: (ids) => set({ activeJobIds: ids }),
  touch: (jobId, status) =>
    set((s) => {
      const done = status === 'succeeded' || status === 'failed' || status === 'cancelled';
      const has = s.activeJobIds.includes(jobId);
      return {
        activeJobIds: done ? s.activeJobIds.filter((x) => x !== jobId) : has ? s.activeJobIds : [...s.activeJobIds, jobId],
      };
    }),
}));

interface ServiceState {
  capabilities: Capabilities | null;
  setCapabilities: (caps: Capabilities) => void;
}

export const useServiceStore = create<ServiceState>((set) => ({
  capabilities: null,
  setCapabilities: (caps) => set({ capabilities: caps }),
}));
