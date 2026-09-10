/* ============================================================================
   DuoCast 前端 · API 层（TanStack Query hooks + fetch 封装）
   契约权威：01 §12.1；PATCH 一律携带 expectedRevision（01 §12 冲突保护）
   ========================================================================== */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  ArtifactEntry, Capabilities, Job, MachineSettings, Project, ScriptRevision, SourceKind, Turn,
} from './types';

const BASE = '/api';

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

async function sendJson<T>(path: string, method: 'POST' | 'PATCH', body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

// ---------- 查询 ----------
export const useProjects = () =>
  useQuery({ queryKey: ['projects'], queryFn: () => getJson<Project[]>('/projects') });

export const useProject = (id: string | null) =>
  useQuery({
    queryKey: ['project', id],
    queryFn: () => getJson<Project>(`/projects/${id}`),
    enabled: !!id,
  });

export const useJob = (jobId: string | null) =>
  useQuery({
    queryKey: ['job', jobId],
    queryFn: () => getJson<Job>(`/jobs/${jobId}`),
    enabled: !!jobId,
  });

export const useJobs = (projectId?: string) =>
  useQuery({
    queryKey: ['jobs', projectId ?? 'all'],
    queryFn: () => {
      const q = projectId ? `?projectId=${encodeURIComponent(projectId)}` : '';
      return getJson<Job[]>(`/jobs${q}`);
    },
  });

export const useCapabilities = () =>
  useQuery({ queryKey: ['capabilities'], queryFn: () => getJson<Capabilities>('/providers/capabilities') });

export const useArtifacts = () =>
  useQuery({ queryKey: ['artifacts'], queryFn: () => getJson<{ artifacts: ArtifactEntry[] }>('/artifacts') });

export const useMachine = () =>
  useQuery({ queryKey: ['machine'], queryFn: () => getJson<MachineSettings>('/machine') });

export const useSaveMachine = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: { providerProfiles?: Record<string, { credentialRef?: string }>; vramAllocation?: Record<string, number> }) =>
      sendJson<{ providerProfiles: Record<string, { credentialRef?: string }>; vramAllocation: Record<string, number> }>('/machine', 'PATCH', patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['machine'] }),
  });
};

export const useTestProvider = () =>
  useMutation({
    mutationFn: (providerId: string) => sendJson<{ ok: boolean; provider: string; capabilities?: Record<string, unknown>; error?: string }>(
      `/providers/${providerId}/test`, 'POST', {},
    ),
  });

// ---------- 变更 ----------
export interface CreateProjectInput { id?: string; title: string; }

export const useCreateProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateProjectInput) => sendJson<Project>('/projects', 'POST', input),
    onSuccess: (proj) => {
      qc.setQueryData(['projects'], (old: Project[] | undefined) => [proj, ...(old ?? [])]);
      qc.setQueryData(['project', proj.id], proj);
    },
  });
};

export interface PatchProjectInput {
  projectId: string;
  patch: Record<string, unknown>;
  expectedRevision: number;
}

export const usePatchProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, patch, expectedRevision }: PatchProjectInput) =>
      sendJson<Project>(`/projects/${projectId}`, 'PATCH', { ...patch, expectedRevision }),
    onSuccess: (proj) => {
      qc.setQueryData(['project', proj.id], proj);
      qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
};

export interface GenerateScriptInput {
  projectId: string;
  clientToken: string;
  sourceInput: { kind: SourceKind; content: string };
  brief?: Record<string, unknown>;
}

export interface JobAccepted { jobId: string; clientToken: string; }

export const useGenerateScript = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, clientToken, sourceInput, brief }: GenerateScriptInput) =>
      sendJson<JobAccepted>(`/projects/${projectId}/script/generate`, 'POST', {
        clientToken,
        sourceInput,
        brief: brief ?? {},
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
};

/* ---------- 脚本编辑（01 §4.2：编辑 → 草稿版本；已用版本不覆盖） ---------- */

/** 计算下一个草稿版本号（R 后缀数值 + 1，稳定 ID 不随排序改变）。 */
export function nextRevisionId(proj: Project): string {
  let max = 0;
  for (const r of proj.scriptRevisions) {
    const m = /^R(\d+)$/.exec(r.id);
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `R${max + 1}`;
}

/**
 * 草稿话轮 upsert：
 * - 当前草稿未被确认且位于列表尾部 → 原位替换（不膨胀历史）
 * - 已确认或已被任务绑定 → 新建草稿版本（不覆盖已用版本，01 §13.2）
 */
export function upsertDraftTurns(proj: Project, turns: Turn[]): { patch: Record<string, unknown>; revId: string } {
  const cur = proj.scriptRevisions.find((r) => r.id === proj.currentDraftRevision)
    ?? proj.scriptRevisions[proj.scriptRevisions.length - 1];
  if (!cur) {
    const rev: ScriptRevision = { id: nextRevisionId(proj), sourceInput: { kind: 'topic', content: '' }, turns, textApiConfigRef: '' };
    return { patch: { scriptRevisions: [rev], currentDraftRevision: rev.id }, revId: rev.id };
  }
  const approved = proj.approvals.some(
    (a) => a.kind === 'script' && a.decision === 'accepted' && a.inputRevisionId === cur.id,
  );
  const isLast = proj.scriptRevisions[proj.scriptRevisions.length - 1]?.id === cur.id;
  if (!approved && isLast) {
    const replaced = proj.scriptRevisions.map((r) => (r.id === cur.id ? { ...r, turns } : r));
    return { patch: { scriptRevisions: replaced }, revId: cur.id };
  }
  const rev: ScriptRevision = {
    id: nextRevisionId(proj),
    sourceInput: cur.sourceInput,
    contentBrief: cur.contentBrief,
    turns,
    textApiConfigRef: cur.textApiConfigRef,
  };
  return {
    patch: { scriptRevisions: [...proj.scriptRevisions, rev], currentDraftRevision: rev.id },
    revId: rev.id,
  };
}

export const useSaveDraft = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, patch, expectedRevision }: { projectId: string; patch: Record<string, unknown>; expectedRevision: number }) =>
      sendJson<Project>(`/projects/${projectId}`, 'PATCH', { ...patch, expectedRevision }),
    onSuccess: (proj) => qc.setQueryData(['project', proj.id], proj),
  });
};

/* ---------- 确认点（01 §13.2：approvals 只追加不修改） ---------- */
export interface ConfirmInput {
  projectId: string;
  expectedRevision: number;
  kind: 'script' | 'voice' | 'sample';
  inputRevisionId: string;
  note?: string;
}

export const useConfirm = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, expectedRevision, kind, inputRevisionId, note }: ConfirmInput) => {
      const proj = qc.getQueryData<Project>(['project', projectId]);
      const approval = {
        kind,
        inputRevisionId,
        spec: {},
        decision: 'accepted' as const,
        reasonTarget: null,
        note: note ?? '',
        at: new Date().toISOString(),
      };
      return sendJson<Project>(`/projects/${projectId}`, 'PATCH', {
        // approvals 为整字段替换语义：追加到现有确认记录后（01 §13.2 只追加不修改）
        approvals: [...(proj?.approvals ?? []), approval],
        expectedRevision,
      });
    },
    onSuccess: (proj) => {
      qc.setQueryData(['project', proj.id], proj);
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
};

/* ---------- 阶段任务提交（01 §12.1：一律幂等 clientToken） ---------- */

export interface RewriteInput {
  projectId: string;
  clientToken: string;
  revisionId: string;
  turnIds: string[];
  mode: 'rewrite' | 'casual' | 'probe' | 'dedupe' | 'trim';
}

export const useScriptRewrite = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: RewriteInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/script/rewrite`, 'POST', {
        clientToken: input.clientToken,
        revisionId: input.revisionId,
        turnIds: input.turnIds,
        mode: input.mode,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface VoiceSynthInput {
  projectId: string;
  clientToken: string;
  revisionId: string;
  voiceBindings: Record<string, { id: string; characterId: string; providerProfileId: string; modelId?: string }>;
  transitionGapMs?: number[];
}

export const useVoiceSynthesize = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: VoiceSynthInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/voice/synthesize`, 'POST', {
        clientToken: input.clientToken,
        revisionId: input.revisionId,
        voiceBindings: input.voiceBindings,
        transitionGapMs: input.transitionGapMs ?? [],
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface VisualGenInput {
  projectId: string;
  clientToken: string;
  aspect: 'landscape' | 'portrait';
  prompt?: string;
}

export const useVisualGenerate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: VisualGenInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/visual/generate`, 'POST', {
        clientToken: input.clientToken,
        aspect: input.aspect,
        prompt: input.prompt ?? '',
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface RenderGenInput {
  projectId: string;
  clientToken: string;
  revisionId: string;
  resolution: string;
  fps: number;
}

export const useRenderGenerate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: RenderGenInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/render/generate`, 'POST', {
        clientToken: input.clientToken,
        revisionId: input.revisionId,
        resolution: input.resolution,
        fps: input.fps,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

/* ---------- 任务控制（02 §7.1 状态机经 API 转发） ---------- */

export const useJobPause = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => sendJson<Job>(`/jobs/${jobId}/pause`, 'POST', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export const useJobCancel = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => sendJson<Job>(`/jobs/${jobId}/cancel`, 'POST', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};
