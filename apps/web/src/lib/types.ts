/* ============================================================================
   DuoCast 前端 · 领域类型（与后端 domain 模型 camelCase 契约 1:1，权威：01 §11 / 06 §9）
   ========================================================================== */

export type StageId = 'create' | 'script' | 'voice' | 'visual' | 'render';
export type StageState =
  | 'not_ready' | 'ready' | 'running' | 'pending_confirm' | 'done' | 'stale' | 'failed';
export type Speaker = 'A' | 'B';
export type Aspect = 'landscape' | 'portrait';
export type SourceKind = 'topic' | 'article' | 'script';
export type JobStatus =
  | 'queued' | 'running' | 'pause_requested' | 'paused' | 'waiting_confirmation'
  | 'recovering' | 'succeeded' | 'failed' | 'cancel_requested' | 'cancelled' | 'unknown';
export type QueueClass = 'gpu' | 'api' | 'cpu';
export type ApprovalKind = 'script' | 'voice' | 'sample' | 'character' | 'composition' | 'shot';

export interface SourceAnchor { ref: string; quote: string; }

export interface Line {
  id: string;
  displayText: string;
  spokenText: string;
}

export interface Turn {
  id: string;
  speaker: Speaker;
  intent: 'explain' | 'probe' | 'example' | 'challenge' | 'summarize' | 'other';
  lines: Line[];
  tone: string;
  speedRatio: number;
  sourceAnchors?: SourceAnchor[];
}

export interface ContentBrief {
  coreQuestion: string;
  keyPoints: string[];
  necessaryFacts: string[];
  paragraphGoals: string[];
  speakerDuties: string;
}

export interface ScriptRevision {
  id: string;
  sourceInput: { kind: SourceKind; content: string };
  contentBrief?: ContentBrief;
  turns: Turn[];
  textApiConfigRef: string;
}

export interface Approval {
  kind: ApprovalKind;
  inputRevisionId: string;
  spec: Record<string, unknown>;
  decision: 'accepted' | 'rejected';
  reasonTarget?: StageId | null;
  note: string;
  at: string;
}

/* ---- 阶段③ 声音三层对象（01 §11.2，与后端 domain/audio.py 对齐） ---- */
export interface VoiceBinding {
  id: string;
  characterId: string;
  providerProfileId: string;
  modelId: string;
  localRefAudio?: { artifactId: string; path: string; fileHash: string } | null;
  minimaxVoiceId?: string | null;
  auditionState: 'none' | 'passed' | 'stale';
}

export interface SynthesisUnit {
  id: string;
  turnId: string;
  lineIds: string[];
  providerProfileId: string;
  modelId: string;
  voiceBindingId: string;
  emotion: Record<string, unknown>;
  speedRatio: number;
  pronunciationRevision: string;
  adoptedAudioAssetId: string | null;
  candidateAudioAssetIds: string[];
  sampleCount: number;
}

export interface AudioTimeline {
  revisionId: string;
  sampleRate: number;
  sampleCount: number;
  units: SynthesisUnit[];
  unitOffsets: Record<string, number>;
  transitionGapMs: number[];
  leadInMs: number;
  tailOutMs: number;
  annotationVersion: string;
}

/* ---- 阶段④ 视觉变体（01 §11.3） ---- */
export interface VisualVariant {
  id: string;
  masterImage: { artifactId: string; path: string; fileHash: string };
  aspect: Aspect;
  modelInputTransform: { scale: number; offsetX: number; offsetY: number; crop?: unknown; pad: string };
  personRegions: Record<string, unknown>;
  imageApiConfigRef: string | null;
}

/* ---- 资产清单（06 §7.2 manifest） ---- */
export interface ArtifactEntry {
  artifactId: string;
  kind: 'audio' | 'image' | 'video' | 'subtitle' | 'mixed';
  path: string;
  fileHash: string;
  dependencyHash: string;
  paramsSnapshot: Record<string, unknown>;
  workflowVersion: string | null;
}

export interface Project {
  schemaVersion: number;
  id: string;
  title: string;
  currentDraftRevision: string;
  selectedOutputVersion: string | null;
  aspect: Aspect;
  scriptRevisions: ScriptRevision[];
  voiceBindings: VoiceBinding[];
  audioTimeline: AudioTimeline | null;
  visualVariants: VisualVariant[];
  outputVersion: string | null;
  stageProgress: Partial<Record<StageId, StageState>>;
  approvals: Approval[];
  revision: number;
}

export interface Job {
  id: string;
  projectId: string;
  kind: string;
  queueClass: QueueClass;
  clientToken: string;
  inputSnapshot: Record<string, unknown>;
  stage: 'prepare' | 'infer' | 'download' | 'verify' | 'post';
  status: JobStatus;
  providerTaskId: string | null;
  attempt: number;
  error: string | null;
  retryable: boolean;
  result: Record<string, unknown> | null;
  createdAt: string;
}

export interface ProviderCaps {
  ready: boolean;
  capabilities: Record<string, unknown>;
}

export interface Capabilities {
  textApi: ProviderCaps;
  imageApi: ProviderCaps;
  tts: ProviderCaps;
  video: ProviderCaps;
}

export interface ServerEvent {
  seq: number;
  type: string;
  [key: string]: unknown;
}

/* ---- 机器设置（06 §7.3，与 /api/machine 契约对齐） ---- */
export interface MachineSettings {
  providerProfiles: Record<string, { credentialRef?: string }>;
  vramAllocation: Record<string, number>;
  queueCaps: Record<string, number>;
  capabilities: Capabilities;
}

/** 服务提供方展示元数据（能力徽章中文名，01 §1.6.4） */
export const PROVIDER_META: Record<'textApi' | 'imageApi' | 'tts' | 'video', { label: string; icon: 'wand' | 'image' | 'mic' | 'film'; desc: string }> = {
  textApi: { label: '文本 API', icon: 'wand', desc: '脚本生成与改写请求发送到此提供方' },
  imageApi: { label: '图片 API', icon: 'image', desc: '阶段④ 主图与构图参考' },
  tts: { label: '本地 TTS', icon: 'mic', desc: '合成单元朗读与试听' },
  video: { label: '视频渲染', icon: 'film', desc: '分段渲染与成片导出' },
};

/** 阶段七态 → 中文徽章文案与色调（01 §13.1） */
export const STAGE_META: Record<StageState, { label: string; tone: 'muted' | 'success' | 'warning' | 'danger' | 'info' | 'brand' }> = {
  not_ready: { label: '未准备', tone: 'muted' },
  ready: { label: '可执行', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  pending_confirm: { label: '待确认', tone: 'warning' },
  done: { label: '已就绪', tone: 'success' },
  stale: { label: '已过期', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
};

/** Job 状态 → 中文文案（02 §7.1） */
export const JOB_LABEL: Record<JobStatus, string> = {
  queued: '排队中',
  running: '执行中',
  pause_requested: '暂停请求',
  paused: '已暂停',
  waiting_confirmation: '待确认',
  recovering: '恢复中',
  succeeded: '已完成',
  failed: '失败',
  cancel_requested: '取消请求',
  cancelled: '已取消',
  unknown: '未知',
};

export const STAGE_ORDER: { id: StageId; title: string; sub: string }[] = [
  { id: 'create', title: '创建本期', sub: '话题 / 文章 / 已有脚本' },
  { id: 'script', title: '编辑对话', sub: '话轮级编辑' },
  { id: 'voice', title: '试听配音', sub: '三层合成单元' },
  { id: 'visual', title: '预览画面', sub: '构图 / 镜头 / 样片' },
  { id: 'render', title: '生成导出', sub: '分段渲染与导出' },
];
