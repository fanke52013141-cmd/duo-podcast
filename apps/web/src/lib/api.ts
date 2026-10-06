/* ============================================================================
   DuoCast 前端 · API 层（TanStack Query hooks + fetch 封装）
   契约权威：01 §12.1；PATCH 一律携带 expectedRevision（01 §12 冲突保护）
   ========================================================================== */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type {
  AudioTimeline,
  ArtifactEntry, Capabilities, Character, Job, MachineSettings, ProgramTemplate, Project,
  PronunciationEntry, ScriptRevision, SourceKind, Turn,
} from './types';

const BASE = '/api';

function is409(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith('409');
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

async function sendJson<T>(path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
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
    // SSE 只失效 ['jobs'] 列表键；单任务查询需自轮询到终态，否则永远停在 running。
    // refetchIntervalInBackground：react-query 默认在窗口失焦时跳过轮询，最小化/后台页需要照常更新。
    refetchInterval: (q) => {
      if (q.state.status === 'error') return false;
      const s = q.state.data?.status;
      return s === 'succeeded' || s === 'failed' || s === 'cancelled' ? false : 1500;
    },
    refetchIntervalInBackground: true,
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
    mutationFn: (patch: { providerProfiles?: Record<string, { credentialRef?: string }>; vramAllocation?: Record<string, number>; pronunciationDict?: PronunciationEntry[] }) =>
      sendJson<{ providerProfiles: Record<string, { credentialRef?: string }>; vramAllocation: Record<string, number>; pronunciationDict?: PronunciationEntry[] }>('/machine', 'PATCH', patch),
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
export interface CreateProjectInput { id?: string; title: string; templateId?: string; }

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

/* ---------- 节目模板库（01 §1.5：GET/POST /api/templates） ---------- */
export const useTemplates = () =>
  useQuery({ queryKey: ['templates'], queryFn: () => getJson<ProgramTemplate[]>('/templates') });

export const useCreateTemplate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string; desc?: string; projectId?: string; config?: ProgramTemplate['config'] }) =>
      sendJson<ProgramTemplate>('/templates', 'POST', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['templates'] }),
  });
};

export const useDeleteTemplate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (templateId: string) => sendJson<{ deleted: string }>(`/templates/${templateId}`, 'DELETE'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['templates'] }),
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
 * - 基版本为当前草稿、未被确认且位于列表尾部 → 原位替换（不膨胀历史）
 * - 其余情况（已确认 / 非尾部 / **基版本不是当前草稿**）→ 新建草稿版本
 *   （11 报告 P0-1：baseRevId 是编辑所基于的版本；查看历史版本时编辑必须新建草稿，
 *    绝不把历史内容写进当前草稿。已用版本不覆盖，01 §13.2）
 */
export function upsertDraftTurns(proj: Project, turns: Turn[], baseRevId?: string): { patch: Record<string, unknown>; revId: string } {
  const base = (baseRevId ? proj.scriptRevisions.find((r) => r.id === baseRevId) : undefined)
    ?? proj.scriptRevisions.find((r) => r.id === proj.currentDraftRevision)
    ?? proj.scriptRevisions[proj.scriptRevisions.length - 1];
  if (!base) {
    const rev: ScriptRevision = { id: nextRevisionId(proj), sourceInput: { kind: 'topic', content: '' }, turns, textApiConfigRef: '' };
    return { patch: { scriptRevisions: [rev], currentDraftRevision: rev.id }, revId: rev.id };
  }
  const approved = proj.approvals.some(
    (a) => a.kind === 'script' && a.decision === 'accepted' && a.inputRevisionId === base.id,
  );
  const isLast = proj.scriptRevisions[proj.scriptRevisions.length - 1]?.id === base.id;
  const isDraft = base.id === proj.currentDraftRevision;
  if (!approved && isLast && isDraft) {
    const replaced = proj.scriptRevisions.map((r) => (r.id === base.id ? { ...r, turns } : r));
    return { patch: { scriptRevisions: replaced }, revId: base.id };
  }
  const rev: ScriptRevision = {
    id: nextRevisionId(proj),
    sourceInput: base.sourceInput,
    contentBrief: base.contentBrief,
    turns,
    textApiConfigRef: base.textApiConfigRef,
  };
  return {
    patch: { scriptRevisions: [...proj.scriptRevisions, rev], currentDraftRevision: rev.id },
    revId: rev.id,
  };
}

/** PATCH + 冲突重放（11 报告 P0-2）：409 时以服务端最新 revision 重放一次同一 patch；
 *  再次 409（真实的并发竞争或校验拒绝）向上抛给调用方展示。 */
async function patchProjectWithReplay(projectId: string, patch: Record<string, unknown>, expectedRevision: number): Promise<Project> {
  try {
    return await sendJson<Project>(`/projects/${projectId}`, 'PATCH', { ...patch, expectedRevision });
  } catch (err) {
    if (!is409(err)) throw err;
    const fresh = await getJson<Project>(`/projects/${projectId}`);
    return await sendJson<Project>(`/projects/${projectId}`, 'PATCH', { ...patch, expectedRevision: fresh.revision });
  }
}

export const useSaveDraft = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, patch, expectedRevision }: { projectId: string; patch: Record<string, unknown>; expectedRevision: number }) =>
      patchProjectWithReplay(projectId, patch, expectedRevision),
    onSuccess: (proj) => qc.setQueryData(['project', proj.id], proj),
  });
};

/* ---------- 确认点（01 §13.2：approvals 只追加不修改） ---------- */
export interface ConfirmInput {
  projectId: string;
  expectedRevision: number;
  kind: 'script' | 'voice' | 'sample';
  spec?: Record<string, unknown>;
  inputRevisionId: string;
  note?: string;
}

export const useConfirm = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, expectedRevision, kind, inputRevisionId, note, spec }: ConfirmInput) => {
      const proj = qc.getQueryData<Project>(['project', projectId]);
      const approval = {
        kind,
        inputRevisionId,
        spec: spec ?? {},
        decision: 'accepted' as const,
        reasonTarget: null,
        note: note ?? '',
        at: new Date().toISOString(),
      };
      // approvals 为整字段替换语义：追加到现有确认记录后（01 §13.2 只追加不修改）；
      // 冲突重放见 patchProjectWithReplay（11 报告 P0-2）。绑定校验在服务端（P1-6）。
      return patchProjectWithReplay(projectId, {
        approvals: [...(proj?.approvals ?? []), approval],
      }, expectedRevision);
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
  /** Omit for a full pass; pass one or more current-script turns to make candidates only. */
  turnIds?: string[];
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
        ...(input.turnIds ? { turnIds: input.turnIds } : {}),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export const useVoiceRhythm = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { projectId: string; transitionGapMs: number[] }) => sendJson<AudioTimeline>(`/projects/${input.projectId}/voice/rhythm`, 'POST', { transitionGapMs: input.transitionGapMs }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['project'] }); qc.invalidateQueries({ queryKey: ['projects'] }); },
  });
};

export interface VoiceAdoptInput {
  projectId: string;
  unitId: string;
  candidateAudioAssetId: string;
}

/** Make one immutable candidate the adopted audio for a unit and rebuild the master track. */
export const useVoiceAdopt = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: VoiceAdoptInput) =>
      sendJson(`/projects/${input.projectId}/voice/units/${input.unitId}/adopt`, 'POST', {
        candidateAudioAssetId: input.candidateAudioAssetId,
      }),
    onSuccess: (_data, input) => {
      qc.invalidateQueries({ queryKey: ['project', input.projectId] });
      qc.invalidateQueries({ queryKey: ['jobs', input.projectId] });
    },
  });
};

export interface VoiceAuditionInput {
  projectId: string;
  speaker: 'A' | 'B';
  text?: string;
  referenceAudio?: { artifactId: string; path: string; fileHash: string };
}

export const useVoiceAudition = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: VoiceAuditionInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/voice/audition`, 'POST', {
        speaker: input.speaker, text: input.text ?? '', referenceAudio: input.referenceAudio,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface VisualGenInput {
  projectId: string;
  clientToken: string;
  aspect: 'landscape' | 'portrait';
  prompt?: string;
  mode?: 'twoShot' | 'overShoulder';
  cameraGroupId?: string;
  cameraAssetId?: string;
  subjectSpeaker?: 'A' | 'B';
  foregroundSpeaker?: 'A' | 'B';
}

export const useVisualGenerate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: VisualGenInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/visual/generate`, 'POST', {
        clientToken: input.clientToken,
        aspect: input.aspect,
        prompt: input.prompt ?? '',
        mode: input.mode ?? 'twoShot',
        cameraGroupId: input.cameraGroupId,
        cameraAssetId: input.cameraAssetId,
        subjectSpeaker: input.subjectSpeaker,
        foregroundSpeaker: input.foregroundSpeaker,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export interface SampleGenInput {
  performanceMode?: 'legacy' | 'adaptive';
  visualVariantId?: string;
  cameraMode?: 'twoShot' | 'speaker';
  projectId: string;
  clientToken: string;
  revisionId: string;
  rangeStartSec: number;
  rangeEndSec: number;
  aspect: 'landscape' | 'portrait';
}

export const useSampleGenerate = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: SampleGenInput) =>
      sendJson<JobAccepted>(`/projects/${input.projectId}/visual/sample`, 'POST', {
        visualVariantId: input.visualVariantId,
        cameraMode: input.cameraMode,
        performanceMode: input.performanceMode,
        clientToken: input.clientToken,
        revisionId: input.revisionId,
        rangeStartSec: input.rangeStartSec,
        rangeEndSec: input.rangeEndSec,
        aspect: input.aspect,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

// ---------- 资产库：全局角色 + 图片上传（01 §1.5） ----------

async function uploadFile<T>(path: string, file: File, extra?: Record<string, string>): Promise<T> {
  const fd = new FormData();
  fd.append('file', file);
  Object.entries(extra ?? {}).forEach(([k, v]) => fd.append(k, v));
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body: fd });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const useCharacters = () =>
  useQuery({ queryKey: ['characters'], queryFn: () => getJson<Character[]>('/characters') });

export const useCreateCharacter = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string; speaker?: 'A' | 'B'; role?: string; desc?: string; traits?: string }) =>
      sendJson<Character>('/characters', 'POST', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['characters'] }),
  });
};

export const useUploadCharacterImage = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ characterId, file }: { characterId: string; file: File }) =>
      uploadFile<Character>(`/characters/${characterId}/image`, file),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['characters'] }),
  });
};

export interface UploadedImage { path: string; webPath: string; fileHash: string }

export const useUploadMasterImage = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ projectId, file, aspect, ...camera }: { projectId: string; file: File; aspect: 'landscape' | 'portrait'; mode?: 'overShoulder'; cameraGroupId?: string; cameraAssetId?: string; subjectSpeaker?: 'A' | 'B'; foregroundSpeaker?: 'A' | 'B' }) =>
      uploadFile<{ variantId: string; webPath: string }>(
      `/projects/${projectId}/visual/upload-master`, file, { aspect, ...camera })
      ,
    onSuccess: (_data, input) => {
      qc.invalidateQueries({ queryKey: ['project', input.projectId] });
      qc.invalidateQueries({ queryKey: ['artifacts'] });
    },
  });
};

export interface RenderGenInput {
  generationProfile?: 'accepted' | 'matched' | 'reference' | 'listener' | 'dialogue';
  listeningSeed?: number;
  motionReferenceArtifactId?: string;
  performanceMode?: 'legacy' | 'adaptive';
  reuseMotionArtifactId?: string;
  redoFromSec?: number;
  candidate?: boolean;
  visualVariantId?: string;
  cameraMode?: 'twoShot' | 'speaker';
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
        visualVariantId: input.visualVariantId,
        cameraMode: input.cameraMode,
        resolution: input.resolution,
        generationProfile: input.generationProfile,
        listeningSeed: input.listeningSeed,
        motionReferenceArtifactId: input.motionReferenceArtifactId,
        performanceMode: input.performanceMode,
        reuseMotionArtifactId: input.reuseMotionArtifactId,
        redoFromSec: input.redoFromSec,
        candidate: input.candidate,
        fps: input.fps,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export const useRenderAdopt = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { projectId: string; jobId: string }) => sendJson<{ version: string }>(`/projects/${input.projectId}/render/adopt`, 'POST', { jobId: input.jobId }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['project'] }); qc.invalidateQueries({ queryKey: ['jobs'] }); },
  });
};

export const useOutputSelect = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { projectId: string; version: string }) => sendJson<{ version: string }>(`/projects/${input.projectId}/render/select-version`, 'POST', { version: input.version }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['project'] }),
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

export const useJobResume = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => sendJson<Job>(`/jobs/${jobId}/resume`, 'POST', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};

export const useJobAbandon = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => sendJson<Job>(`/jobs/${jobId}/abandon`, 'POST', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs'] }),
  });
};


export interface ListeningPlan {
  version: string;
  seed: number;
  referenceCount: number;
  requiresVisualReview: boolean;
  segments: { start: number; end: number; eligible: boolean; reason: string; listener?: 'A' | 'B'; label?: string; reactionAtSec?: number }[];
}
export const useListeningPlan = (projectId: string, visualId: string | undefined, seed: number, enabled: boolean) =>
  useQuery({ queryKey: ['listening-plan', projectId, visualId, seed],
    queryFn: () => getJson<ListeningPlan>(`/projects/${projectId}/render/listening-plan?seed=${seed}${visualId ? `&visualVariantId=${encodeURIComponent(visualId)}` : ''}`), enabled });
