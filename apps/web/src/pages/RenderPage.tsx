import { motionGenerationAllowed } from '../lib/motionPacks';
import { ProjectMotionBinding, useMotionReadiness } from '../components/MotionPackSelector';
/* ============================================================================
   HF-05 · 阶段⑤ 生成导出（真实数据流，01 §7 权威）
   开始前摘要（输入快照 / 复用与待生成段 / 预估 / 磁盘预算）→ 分段进度网格 → 导出区
   检查器：导出规格 / 后处理顺序 / 竖屏导出
   注意：没有测速数据时显示「尚未测定」，不编造预估（01 §7.1 明确要求）。
   ========================================================================== */
import { useEffect, useMemo, useState, useRef } from 'react';
import {
  useJobs, useProject, useRenderGenerate, useRenderAdopt, useOutputSelect, usePatchProject, useRenderSegmentPlan, useImportOutput, useMachine, type ListeningPlan,
} from '../lib/api';
import type { AudioTimeline, Job, Project, SynthesisUnit } from '../lib/types';
import { Alert, Badge, Button, Icon } from '../components/wu';
import { InfoTip } from '../components/InfoTip';
import { AppShell, FootBar, StageHead, TimelineBar } from '../components/AppShell';
import { formatTimecode } from '../lib/format';
import { useError } from '../lib/useError';
import { useProjectStore, useServiceStore } from '../stores';
import { ImpactPlan } from '../components/ImpactPlan';
import { DeliveryCheck } from '../components/DeliveryCheck';

const RESOLUTION_OPTIONS = [
  { id: '480p', label: '480p', value: '854x480' },
  { id: '720p', label: '720p', value: '1280x720' },
  { id: '1080p', label: '1080p', value: '1920x1080' },
];

const CODEC = 'H.264';
const FPSS = [25, 30];

type WorkflowMode = 'local' | 'api' | 'direct';
type ViduModel = '3.4' | '3.2';
type ViduMode = 'text' | 'first-frame' | 'first-last-frame' | 'reference';
type ViduResolution = '540p' | '720p' | '1080p' | '2k' | '4k';
type ViduRatio = '16:9' | '9:16' | '1:1' | '4:3' | '3:4';

const VIDU_RATIOS: ViduRatio[] = ['16:9', '9:16', '1:1', '4:3', '3:4'];
const VIDU_Q4_RESOLUTIONS: ViduResolution[] = ['540p', '720p', '1080p', '2k', '4k'];

interface Segment {
  id: string;
  units: SynthesisUnit[];
  outputRange: [number, number];
  renderRange: [number, number];
  contextRange: [number, number];
}

/** 与当前后端一致的五秒分段。 */
function planSegments(timeline: AudioTimeline): Segment[] {
  const sr = timeline.sampleRate || 48000;
  const sec = (s: number) => s / sr;
  const units = timeline.units;
  if (!units.length) return [];
  const total = sec(timeline.sampleCount || 0);
  const out: Segment[] = [];
  for (let start = 0; start < total; start += 5) {
    const end = Math.min(total, start + 5);
    const slice = units.filter(u => sec(timeline.unitOffsets[u.id] ?? 0) < end && sec((timeline.unitOffsets[u.id] ?? 0) + u.sampleCount) > start);
    out.push({
      id: `S${String(out.length + 1).padStart(2, '0')}`,
      units: slice,
      outputRange: [start, end],
      renderRange: [start, end],
      contextRange: [0, start],
    });
  }
  return out;
}

function candidateLabel(job: Job): string {
  const meta = job.result?.meta as { generationMode?: string; generationProfile?: string; performanceMode?: string; visualMode?: string; imported?: boolean; mode?: string; provider?: string } | undefined;
  if (meta?.provider === 'vidu') return 'Vidu API';
  if (meta?.mode === 'direct') return '直接上传视频';
  if (meta?.imported) return '外部导入';
  if (meta?.visualMode === 'overShoulder') return '过肩正反打';
  return meta?.generationMode === 'fast' ? '快速模式' : meta?.generationProfile === 'dialogue' ? '按对话倾听' : meta?.generationProfile === 'listener' ? '分层倾听' : meta?.generationProfile === 'reference' ? '动作参考' : meta?.generationProfile === 'matched' ? '模型匹配' : meta?.performanceMode === 'adaptive' ? '角色编排' : '原有配置';
}

function segLabel(seg: Segment, idx: number, total: number, speakers: Map<string, 'A' | 'B'>): string {
  if (idx === 0) return '片头';
  if (idx === total - 1) return '片尾';
  const seq = seg.units.map((u) => speakers.get(u.turnId) ?? 'A');
  const uniq = [...new Set(seq)];
  const shape = uniq.length === 1 ? `${uniq[0]} 独白` : `${seq[0]}→${seq[seq.length - 1]}`;
  return `段${idx} ${shape}`;
}

export function RenderPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const machine = useMachine();
  const render = useRenderGenerate();
  const adopt = useRenderAdopt();
  const selectOutput = useOutputSelect();
  const patchProject = usePatchProject();
  // 批 2（42 号方案）：外部成片导入为候选（不自动采用）
  const importOutput = useImportOutput();
  const outputUploadRef = useRef<HTMLInputElement>(null);
  const [importAudioMode, setImportAudioMode] = useState<'keep' | 'master'>('keep');
  const [workflowMode, setWorkflowMode] = useState<WorkflowMode>('local');
  const isDirectMode = workflowMode === 'direct';
  const isApiMode = workflowMode === 'api';
  const isLocalMode = workflowMode === 'local';
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const baselineVideo = useRef<HTMLVideoElement>(null);
  const candidateVideo = useRef<HTMLVideoElement>(null);
  const [redoFromSec, setRedoFromSec] = useState(0);
  const [candidateId, setCandidateId] = useState('');
  const setView = useProjectStore((s) => s.setView);
  const simulated = useServiceStore((s) => s.capabilities?.video.mode === 'mock');
  const activeConfig = machine.data?.activeConfig;
  const activeVideoProvider = typeof activeConfig?.video_provider === 'string' ? activeConfig.video_provider : '';
  const configuredViduModel = activeConfig?.vidu_model;
  const viduActive = activeVideoProvider === 'vidu';
  const viduCredentialPresent = activeConfig?.viduCredentialPresent === true;
  const [viduPrompt, setViduPrompt] = useState('');
  const [viduModel, setViduModel] = useState<ViduModel>('3.4');
  const [viduMode, setViduMode] = useState<ViduMode>('reference');
  const [viduDuration, setViduDuration] = useState('4');
  const [viduResolution, setViduResolution] = useState<ViduResolution>('540p');
  const [viduRatio, setViduRatio] = useState<ViduRatio>('16:9');
  const [selectedImageArtifactIds, setSelectedImageArtifactIds] = useState<string[]>([]);
  const [withMasterAudio, setWithMasterAudio] = useState(false);
  const [rangeStartSec, setRangeStartSec] = useState('0');
  useEffect(() => {
    if (configuredViduModel !== '3.4' && configuredViduModel !== '3.2') return;
    setViduModel(configuredViduModel);
    setViduResolution(configuredViduModel === '3.2' ? '1080p' : '540p');
    if (configuredViduModel === '3.4') setViduMode('reference');
  }, [configuredViduModel]);

  const proj: Project | undefined = project.data;
  const pinnedReady=useMotionReadiness(isLocalMode ? proj?.motionPack : undefined,isLocalMode ? proj?.shotStyle : undefined);
  const isFast = proj?.generationMode === 'fast';
  const saveSpec = (patch: NonNullable<Project['renderSpec']>) => {
    if (proj) patchProject.mutate({projectId, expectedRevision:proj.revision, patch:{renderSpec:{...proj.renderSpec,...patch}}},{onError:e=>setError(e.message)});
  };
  const [resolutionId, setResolutionId] = useState('480p');
  const [fps, setFps] = useState(25);
  const [fastQuality, setFastQuality] = useState<'original' | 'clear' | 'crisp'>('original');
  const [exportSr, setExportSr] = useState(false);
  useEffect(()=>{
    if (proj?.renderSpec) {
      setResolutionId(RESOLUTION_OPTIONS.find(r=>r.value===proj.renderSpec?.resolution)?.id??'480p');
      setFps(proj.renderSpec.fps??25);setFastQuality(proj.renderSpec.fastQuality??'original');
    }
  },[proj?.renderSpec]);
  const { error, setError, dismiss: dismissError } = useError();

  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;
  const hasMasterAudio = !!timeline?.masterAudioAssetId;
  useEffect(() => {
    if (!hasMasterAudio) {
      setWithMasterAudio(false);
      setRangeStartSec('0');
    }
  }, [hasMasterAudio]);
  const segments = useMemo(() => (timeline ? planSegments(timeline) : []), [timeline]);
  const outputVersion = proj?.selectedOutputVersion ?? proj?.outputVersion ?? null;
  const sampleApproved = proj?.stageProgress.visual === 'done';
  const eligibleVariants = proj?.visualVariants.filter(v => v.aspect === (proj.aspect ?? 'landscape') && (v.mode !== 'overShoulder' || v.cameraGroups?.some(g => g.cameraAssets.some(c => c.subjectSpeaker === 'A') && g.cameraAssets.some(c => c.subjectSpeaker === 'B')))) ?? [];
  const latestVariant = eligibleVariants.find(v => v.id === proj?.selectedVisualVariantId) ?? eligibleVariants[eligibleVariants.length - 1] ?? null;
  const viduImages = useMemo(() => (proj?.visualVariants ?? [])
    .filter(v => !!v.masterImage?.artifactId)
    .map(v => ({ variantId: v.id, artifactId: v.masterImage.artifactId })), [proj?.visualVariants]);
  useEffect(() => {
    const preferred = viduImages.find(image => image.variantId === proj?.selectedVisualVariantId) ?? viduImages[viduImages.length - 1];
    if (viduMode === 'text' || !preferred) {
      setSelectedImageArtifactIds([]);
      return;
    }
    if (viduMode !== 'first-last-frame') {
      setSelectedImageArtifactIds([preferred.artifactId]);
      return;
    }
    const last = viduImages[viduImages.length - 1];
    const secondary = (last?.artifactId !== preferred.artifactId ? last : undefined)
      ?? [...viduImages].reverse().find(image => image.artifactId !== preferred.artifactId);
    setSelectedImageArtifactIds([preferred.artifactId, ...(secondary ? [secondary.artifactId] : [])]);
  }, [viduMode, viduImages, proj?.selectedVisualVariantId]);

  const speakers = useMemo(() => {
    const rev = proj?.scriptRevisions.find((r) => r.id === timeline?.revisionId)
      ?? proj?.scriptRevisions[proj.scriptRevisions.length - 1];
    const m = new Map<string, 'A' | 'B'>();
    rev?.turns.forEach((t) => m.set(t.id, t.speaker));
    return m;
  }, [proj?.scriptRevisions, timeline?.revisionId]);

  const renderJob: Job | undefined = (jobs.data ?? []).find(
    (j) => j.kind === 'renders' && ['queued', 'running', 'recovering'].includes(j.status),
  );
  const rendering = !!renderJob;
  const localProviderReady = !!machine.data && (activeVideoProvider === 'mock' || activeVideoProvider === 'comfyui');
  const canRender = !!proj && !!timeline && localProviderReady && timeline.revisionId === proj.currentDraftRevision
    && motionGenerationAllowed(proj.motionPack,pinnedReady.data?.ready)
    && timeline.units.length > 0 && timeline.units.every(unit => unit.sampleCount > 0)
    && timeline.sampleCount > 0 && timeline.sampleCount <= 300 * timeline.sampleRate && !rendering && !render.isPending;
  const resolution = RESOLUTION_OPTIONS.find((r) => r.id === resolutionId)?.value ?? '854x480';
  const totalSecs = timeline ? timeline.sampleCount / (timeline.sampleRate || 48000) : 0;
  const isOverShoulder = latestVariant?.mode === 'overShoulder';
  const standardSource = !isFast && !!proj?.outputManifest
    && (proj.outputManifest.meta.performanceMode ?? 'legacy') === 'legacy'
    && (proj.outputManifest.meta.generationProfile ?? 'accepted') === 'accepted';

  const handleRender = (localRedo = false) => {
    if (!proj || !timeline || render.isPending) return;
    setError(null);
    render.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      revisionId: timeline.revisionId,
      visualVariantId: proj.motionPack?undefined:proj.selectedVisualVariantId ?? latestVariant?.id,
      cameraMode: isOverShoulder ? 'speaker' : proj.cameraMode ?? 'speaker',
      resolution,
      fps,
      performanceMode: proj.performanceMode ?? 'legacy',
      generationProfile: 'accepted',
      fastQuality: isFast ? fastQuality : undefined,
      exportSr: isFast ? undefined : exportSr,
      candidate: true,
      reuseMotionArtifactId: !isOverShoulder && standardSource ? proj.outputManifest?.artifactIds[0] : undefined,
      redoFromSec: localRedo ? redoFromSec : undefined,
    }, { onError: (e) => setError((e as Error).message) });
  };

  const viduDurationNumber = Number(viduDuration);
  const rangeStartNumber = Number(rangeStartSec);
  const viduDurationValid = viduDuration.trim() !== '' && Number.isInteger(viduDurationNumber)
    && viduDurationNumber >= 1 && viduDurationNumber <= 16;
  const selectedImagesExist = selectedImageArtifactIds.every(id => viduImages.some(image => image.artifactId === id));
  const viduImageCountValid = viduMode === 'text'
    ? selectedImageArtifactIds.length === 0
    : viduMode === 'first-frame'
      ? selectedImageArtifactIds.length === 1
      : viduMode === 'first-last-frame'
        ? selectedImageArtifactIds.length === 2
        : selectedImageArtifactIds.length >= 1 && selectedImageArtifactIds.length <= 7;
  let viduConfigIssue: string | null = null;
  if (!machine.data) viduConfigIssue = '正在读取当前生效的服务配置，请稍候。';
  else if (!viduActive) viduConfigIssue = '当前生效的视频提供方不是 Vidu；请在服务设置选择 Vidu，保存并重启后再提交。';
  else if (simulated || machine.data.capabilities.video.mode === 'mock') viduConfigIssue = '当前视频服务仍为演示模式，不能提交 Vidu API 生成。';
  else if (!viduCredentialPresent) viduConfigIssue = '当前进程未读取 DUOCAST_VIDU_TOKEN；请在服务环境中设置后重启。';
  else if (!viduPrompt.trim()) viduConfigIssue = '请填写 Vidu 提示词。';
  else if (viduModel === '3.4' && viduMode !== 'reference') viduConfigIssue = 'Vidu Q4（3.4）仅支持参考生视频。';
  else if (!viduDurationValid) viduConfigIssue = 'Vidu 时长须为 1–16 秒整数。';
  else if (viduModel === '3.2' ? viduResolution !== '1080p' : !VIDU_Q4_RESOLUTIONS.includes(viduResolution)) viduConfigIssue = '当前模型的分辨率无效，请重新选择。';
  else if (!VIDU_RATIOS.includes(viduRatio)) viduConfigIssue = '请选择有效的 Vidu 画幅。';
  else if (!selectedImagesExist || !viduImageCountValid) {
    viduConfigIssue = viduMode === 'first-last-frame' ? '首尾帧模式需按顺序选择 2 张不同的视觉资产图像。'
      : viduMode === 'first-frame' ? '首帧模式需选择 1 张视觉资产图像。'
        : viduMode === 'reference' ? '参考生视频需选择 1–7 张视觉资产图像。'
          : '文生视频不应选择参考图像。';
  } else if (withMasterAudio && (!hasMasterAudio || rangeStartSec.trim() === '' || !Number.isFinite(rangeStartNumber) || rangeStartNumber < 0)) {
    viduConfigIssue = '叠加母轨时需要有效母轨，且 rangeStartSec 须为不小于 0 的数值。';
  } else if (withMasterAudio && rangeStartNumber + viduDurationNumber > totalSecs + 0.2) {
    viduConfigIssue = `配音区间超出母轨总长 ${totalSecs.toFixed(2)} 秒，请减小起点或时长。`;
  }
  const canViduRender = !!proj && !viduConfigIssue && !rendering && !render.isPending;
  const handleViduRender = () => {
    if (!proj || !canViduRender) {
      setError(viduConfigIssue ?? 'Vidu 任务正在提交或执行，请稍候。');
      return;
    }
    setError(null);
    render.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      provider: 'vidu',
      prompt: viduPrompt.trim(),
      viduModel,
      viduMode,
      viduDuration: viduDurationNumber,
      viduResolution,
      viduRatio,
      imageArtifactIds: viduMode === 'text' ? [] : selectedImageArtifactIds,
      withMasterAudio: hasMasterAudio && withMasterAudio,
      rangeStartSec: hasMasterAudio && withMasterAudio ? rangeStartNumber : undefined,
      candidate: true,
      revisionId: proj.currentDraftRevision || 'VIDU',
      resolution,
      fps,
    }, {
      onSuccess: (result) => {
        setCandidateId(result.jobId);
        setImportNotice('Vidu API 任务已提交；完成后仅登记为候选，请人工播放审看再采用。');
      },
      onError: (e) => setError((e as Error).message),
    });
  };

  // 先算版本集合：判断"当前正式成片"必须看产物是否已被采用，
  // 否则首个未采用的候选会被当成正式视频（分段全绿、基线自己比自己）。
  const versions = [...new Map([...(proj?.outputHistory ?? []), ...(proj?.outputManifest ? [proj.outputManifest] : [])].map(v => [v.version, v])).values()];
  const adoptedArtifacts = new Set(versions.flatMap(v => v.artifactIds ?? []));
  const recentRender = (jobs.data ?? []).find((j) => {
    if (j.kind !== 'renders' || j.status !== 'succeeded') return false;
    const meta = j.result?.meta as { candidate?: boolean } | undefined;
    const artifactId = j.result?.artifactIds?.[0];
    if (artifactId && adoptedArtifacts.has(artifactId)) return true;
    // 还没有采用任何成片时，只有正式渲染（非候选）才算当前视频
    return meta?.candidate !== true && !proj?.outputManifest;
  });
  const candidates = (jobs.data ?? []).filter(j => {
    const meta = j.result?.meta as { candidate?: boolean; mode?: string; provider?: string } | undefined;
    const inWorkflow = isApiMode ? meta?.provider === 'vidu'
      : isDirectMode ? meta?.mode === 'direct'
        : meta?.provider !== 'vidu' && meta?.mode !== 'direct';
    return j.kind === 'renders' && j.status === 'succeeded' && meta?.candidate === true
      && inWorkflow
      && !adoptedArtifacts.has(j.result?.artifactIds?.[0] ?? '');
  });
  const otsCompleted = (jobs.data ?? []).find(j => j.kind === 'renders' && j.status === 'succeeded' && j.inputSnapshot.visualVariantId === latestVariant?.id && (j.result?.meta as { visualMode?: string } | undefined)?.visualMode === 'overShoulder');
  const candidate = candidates.find(j => j.id === candidateId) ?? candidates[0];
  const candidateMeta = candidate?.result?.meta as { generationStats?: { wallSeconds?: number; generated: number; composited?: number; cached: number; retained: number }; performanceMode?: string; generationProfile?: string; automaticReferenceArtifactIds?: string[]; listeningPlan?: ListeningPlan; continuityTransitions?: { atSec: number; frameAtSec?: number; cameraAssetId: string; qualityCheck?: { before: { windowMaxMeanGrayChange: number }; after: { windowMaxMeanGrayChange: number }; needsReview: boolean } }[] } | undefined;
  const comparisonStart = typeof candidate?.inputSnapshot.redoFromSec === 'number' ? candidate.inputSnapshot.redoFromSec : candidateMeta?.listeningPlan?.segments.find(s => s.eligible)?.start;
  const lastMeta = recentRender?.result?.meta as { outputVersion?: string; resolution?: string; fps?: number } | undefined;
  // 演示渲染已跑过（模拟通道）：分段卡与快照状态按「演示完成」展示，不再永远停在「待生成」
  const demoRendered = recentRender?.result?.simulated === true;
  const realRendered = !!recentRender && !demoRendered;
  useEffect(() => {
    if (lastMeta?.resolution) setResolutionId(RESOLUTION_OPTIONS.find(r => r.value === lastMeta.resolution)?.id ?? '480p');
    if (lastMeta?.fps) setFps(lastMeta.fps);
  }, [recentRender?.id]);



  const voiceProfile = proj?.voiceBindings?.[0]?.providerProfileId;
  const voiceSource = voiceProfile === 'minimax' ? 'MiniMax'
    : voiceProfile === 'volcengine' ? '火山引擎'
    : voiceProfile === 'comfyui-indextts' ? '本地引擎'
    : '上传录音 / 演示语音';
  // P0-C（42 号方案）：分段复用读后端真实哈希校验计划，不再硬编码 0。
  const segmentPlan = useRenderSegmentPlan(projectId, proj?.revision ?? 0, isLocalMode && !!proj && !!timeline && !isFast && !isOverShoulder);
  const reusedCount = segmentPlan.data?.available ? segmentPlan.data.reusableCount : 0;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      timeline={isLocalMode ? (
        <TimelineBar
          timeline={timeline}
          labels={[isOverShoulder ? '过肩镜头按配音话轮切换' : segments.length ? '时间区间示意 · 实际进度以任务中心为准' : '时间区间：未规划', 'A 轨 / B 轨']}
          speakerOf={(turnId) => speakers.get(turnId) ?? 'A'}
        />
      ) : null}
      inspector={isDirectMode ? (
        <aside className="hf-inspector" aria-label="直接上传检查器">
          <div className="hf-ins-sec">
            <h4>直接上传视频</h4>
            <p className="wu-caption">不读取脚本、配音时间轨或视觉资产，不运行本地数字人合成与母轨重混；上传文件及原音轨保持不变。</p>
          </div>
          <div className="hf-ins-sec">
            <h4>候选流程</h4>
            <ul className="hf-ins-list">
              <li>上传后仅登记为待审看候选</li>
              <li>播放确认画面与声音后手动采用</li>
              <li>候选与已采用视频均可下载</li>
            </ul>
          </div>
          <div className="hf-ins-sec">
            <h4>输出状态</h4>
            <p className="wu-caption">{outputVersion ? `当前已采用 ${outputVersion}` : '尚未采用直接上传候选'}</p>
          </div>
        </aside>
      ) : isApiMode ? (
        <aside className="hf-inspector" aria-label="Vidu API 检查器">
          <div className="hf-ins-sec">
            <h4>当前生效服务</h4>
            <p className="wu-caption">视频提供方：{activeVideoProvider || '未读取'}<br />默认模型：{typeof configuredViduModel === 'string' ? configuredViduModel : '未设置'}<br />DUOCAST_VIDU_TOKEN：{viduCredentialPresent ? '已读取' : '未读取'}</p>
          </div>
          <div className="hf-ins-sec">
            <h4>生成边界</h4>
            <ul className="hf-ins-list">
              <li>Vidu 不是口型同步引擎</li>
              <li>单次生成 1–16 秒，不自动拼接整期</li>
              <li>结果只进入候选，需人工审看后采用</li>
              <li>母轨叠加可选，不等于口型对齐</li>
            </ul>
          </div>
        </aside>
      ) : (
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>导出规格</h4>
            <div className="hf-fld"><label>画面机位</label><select className="wu-input" aria-label="画面机位" value={latestVariant?.id ?? ''} onChange={e => {
              if (proj) patchProject.mutate({ projectId, expectedRevision: proj.revision, patch: { selectedVisualVariantId: e.target.value } }, { onError: e => setError((e as Error).message) });
            }}>{eligibleVariants.map(v => <option key={v.id} value={v.id}>{v.id} · {v.mode === 'overShoulder' ? '过肩正反打' : '双人同框'}</option>)}</select></div>
            <div className="hf-fld">
              <label>画布</label>
              <input
                className="wu-input" readOnly
                title="竖屏布局尚未开放；当前提供横屏双人同框与双方特写"
                value={proj?.aspect === 'portrait' ? '9:16 导出画布' : `${resolutionId} → 16:9 导出画布`}
              />
            </div>
            <div className="hf-fld">
              <label>分辨率</label>
              <select className="wu-input" aria-label="分辨率" value={resolutionId} onChange={(e) => {setResolutionId(e.target.value);saveSpec({resolution:RESOLUTION_OPTIONS.find(r=>r.id===e.target.value)?.value});}}>
                {RESOLUTION_OPTIONS.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
              </select>
            </div>
            <div className="hf-fld">
              <label>帧率</label>
              <select className="wu-input" aria-label="帧率" value={fps} onChange={(e) => {setFps(Number(e.target.value));saveSpec({fps:Number(e.target.value)});}}>
                {FPSS.map((f) => <option key={f} value={f}>{f} fps</option>)}
              </select>
            </div>
            {isFast && <div className="hf-fld">
              <label htmlFor="fast-quality">画面清晰度 <InfoTip text="动作素材的原生分辨率决定细节上限，特写放大会损失细节。增强会增大文件，较强锐化可能产生边缘光晕；不会恢复原本缺失的细节。" /></label>
              <select id="fast-quality" className="wu-input" value={fastQuality} onChange={e => {setFastQuality(e.target.value as 'original' | 'clear' | 'crisp');saveSpec({fastQuality:e.target.value as 'original' | 'clear' | 'crisp'});}}>
                <option value="original">原始 · 不增强</option>
                <option value="clear">清晰 · 轻度锐化与低压缩（推荐）</option>
                <option value="crisp">更清晰 · 较强锐化与低压缩</option>
              </select>
            </div>}
            {!isFast && <div className="hf-fld">
              <label htmlFor="export-sr">AI 超分</label>
              <select id="export-sr" className="wu-input" aria-label="AI 超分" value={exportSr ? 'on' : 'off'} onChange={e => setExportSr(e.target.value === 'on')}>
                <option value="off">关闭 · 普通放大（默认，可导出后用外部工具超分）</option>
                <option value="on">开启 · SeedVR2 本机超分（画质更好，约 2.5 小时/2 分钟片）</option>
              </select>
            </div>}
            {!isFast && exportSr && <p className="hf-sr-hint">仅导出分辨率高于 480p 时生效；本机超分较慢（约 2.5 小时/2 分钟片），赶时间建议关闭后拿外部工具处理。</p>}
            <div className="hf-fld">
              <label>编码</label>
              <input className="wu-input" aria-label="编码" value={`${CODEC} · 当前固定编码`} readOnly />
            </div>
            <p className="wu-caption">补帧 / 烧录字幕 <InfoTip text="当前引擎未开放补帧与烧录字幕。" /></p>
          </div>

          <div className="hf-ins-sec">
            <h4>后处理顺序 <InfoTip text="基础视频 → 镜头裁切 → 合并配音 → 编码 → 画面与声音校验。导出失败只重跑相应后处理或编码，不重新生成全部口型视频。" /></h4>
          </div>
          {!isOverShoulder && proj?.cameraMode !== 'twoShot' && <div className="hf-ins-sec">
            <h4>话轮机位 <InfoTip text="自动模式按整期对话编排；短接话合并到相邻镜头。" /></h4>
            {proj?.scriptRevisions.find(r => r.id === timeline?.revisionId)?.turns.map(turn => <div className="hf-fld" key={turn.id}>
              <label>{turn.id} · {turn.speaker}</label>
              <select className="wu-input" aria-label={`${turn.id}机位`} value={proj.shotCameraOverrides?.[turn.id] ?? 'auto'} onChange={e => {
                const overrides = { ...proj.shotCameraOverrides };
                if (e.target.value === 'auto') delete overrides[turn.id];
                else overrides[turn.id] = e.target.value as 'twoShot' | 'A' | 'B';
                patchProject.mutate({ projectId, expectedRevision: proj.revision, patch: { shotCameraOverrides: overrides } }, { onError: e => setError((e as Error).message) });
              }}>
                <option value="auto">自动</option><option value="twoShot">两人同框</option><option value="A">A特写</option><option value="B">B特写</option>
              </select>
            </div>)}
          </div>}

          <div className="hf-ins-sec">
            <h4>阶段状态</h4>
            <ul className="hf-ins-list">
              <li>脚本确认：{proj?.stageProgress.script === 'done' ? '已通过' : '未确认'}</li>
              <li>配音确认：{proj?.stageProgress.voice === 'done' ? '已通过' : '未确认'}</li>
              <li>样片确认：{sampleApproved ? '已通过' : '未确认'}</li>
            </ul>
          </div>
        </aside>
      )}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('visual')}>上一步</Button>
            <Badge tone={outputVersion ? 'success' : 'muted'} icon={outputVersion ? 'check' : 'clock'}>
              输出版本{outputVersion ? ` · ${outputVersion}` : isDirectMode ? ' · 未采用' : ' · 未导出'}
            </Badge>
            <span className="hf-spacer" />
            <Button size="sm" onClick={() => setView('assets')}>查看资产库<Icon name="arrowRight" size={14} /></Button>
          </span>
        } />
      }
    >
      <StageHead title="⑤ 生成导出">
        <select className="wu-input" aria-label="成片工作模式" style={{ width: 'auto' }}
          value={workflowMode} onChange={(e) => {
            setWorkflowMode(e.target.value as WorkflowMode);
            setError(null);
            setImportNotice(null);
          }}>
          <option value="local">本地生成</option>
          <option value="api">Vidu API 生成</option>
          <option value="direct">直接上传视频</option>
        </select>
        {isDirectMode ? <Badge tone="info">保留原音轨 · 无需时间轨 · 候选需人工采用</Badge> : isApiMode ? <Badge tone="info">云端短视频 · 非口型引擎 · 候选需人工审看</Badge> : <Badge tone="muted">
          输入快照 {timeline?.revisionId ?? 'R—'} / {latestVariant?.id ?? 'V—'} / {isOverShoulder ? '过肩双机位' : segments.length ? `S${segments.length}` : 'S—'} · {outputVersion ? '已冻结' : demoRendered ? '演示完成 · 未冻结' : '未冻结'}
        </Badge>}
        <span className="hf-spacer" />
        <a className="hf-link" href={`/api/projects/${projectId}/backup`} download>备份工程</a>
        {isLocalMode && <select className="wu-input" aria-label="导入成片音轨处理" style={{ width: 'auto' }}
          value={importAudioMode} onChange={(e) => setImportAudioMode(e.target.value as 'keep' | 'master')}>
          <option value="keep">导入保留原音轨</option>
          <option value="master">导入仅取画面（重混母轨）</option>
        </select>}
        {!isApiMode && <>
          <Button variant="secondary" icon="upload" busy={importOutput.isPending}
            disabled={!proj || importOutput.isPending || (isLocalMode && !timeline)}
            title={isDirectMode ? '上传已剪辑视频，不经过本地合成，保留原音轨并进入候选审看' : '上传外部剪辑的成片（时长须与配音一致），成为候选后人工采用'}
            onClick={() => outputUploadRef.current?.click()}>{isDirectMode ? '上传视频为候选' : '导入外部成片'}</Button>
          <input ref={outputUploadRef} type="file" accept="video/mp4,video/*" hidden aria-label={isDirectMode ? '直接上传视频文件' : '导入外部成片文件'}
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = '';
              if (!f) return;
              setError(null);
              setImportNotice(null);
              importOutput.mutate({ projectId, file: f, audioMode: isDirectMode ? 'keep' : importAudioMode, mode: isDirectMode ? 'direct' : undefined }, {
                onSuccess: (r) => {
                  setCandidateId(r.jobId);
                  setImportNotice(`已上传 ${f.name}（${r.durationSec.toFixed(2)}s · ${r.resolution}）为候选；请在下方候选视频中审看并点「采用候选」生效。`);
                },
                onError: (err) => setError((err as Error).message),
              });
            }} />
        </>}
        {isLocalMode && <Button
          variant="brand" icon="play" busy={rendering || render.isPending} disabled={!canRender}
          onClick={() => handleRender()}
        ><span id="render-generate-action">{rendering ? '执行中' : simulated ? '运行流程演示' : '生成候选成片'}</span></Button>}
        {isApiMode && <Button
          variant="brand" icon="play" busy={rendering || render.isPending} disabled={!canViduRender}
          title={viduConfigIssue ?? '提交一次 Vidu API 生成，结果仅作为待审看候选'}
          onClick={handleViduRender}
        ><span id="render-generate-action">{render.isPending ? '提交中' : rendering ? '执行中' : 'Vidu API 生成'}</span></Button>}
      </StageHead>

      <div className="hf-body">
        {isDirectMode && <Alert tone="info">直接上传已剪辑视频，不经过本地合成，也不需要脚本或配音时间轨。视频与原音轨保持不变；上传后请先播放审看，再手动采用或下载。</Alert>}
        {isApiMode && <section className="hf-card" aria-label="Vidu API 生成参数">
          <div className="hf-card-t">Vidu API 生成<span className="hf-pill" data-tone="warn">付费服务 · 候选需人工审看</span></div>
          <Alert tone="info">Vidu 不是口型同步引擎；单次仅生成 1–16 秒短视频，不会自动拼接整期。结果只进入候选，必须人工播放审看后再采用。</Alert>
          <div className="hf-fld">
            <label htmlFor="vidu-prompt">提示词（必填）</label>
            <textarea id="vidu-prompt" className="wu-input" aria-label="Vidu 提示词" rows={3} value={viduPrompt} onChange={e => setViduPrompt(e.target.value)} placeholder="描述镜头、人物动作、场景与氛围" />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 12 }}>
            <div className="hf-fld">
              <label htmlFor="vidu-model">模型</label>
              <select id="vidu-model" className="wu-input" aria-label="Vidu 模型" value={viduModel} onChange={e => {
                const model = e.target.value as ViduModel;
                setViduModel(model);
                setViduResolution(model === '3.2' ? '1080p' : '540p');
                if (model === '3.4') setViduMode('reference');
              }}>
                <option value="3.4">3.4 · Q4</option>
                <option value="3.2">3.2 · Q3</option>
              </select>
            </div>
            <div className="hf-fld">
              <label htmlFor="vidu-mode">生成模式</label>
              <select id="vidu-mode" className="wu-input" aria-label="Vidu 生成模式" value={viduMode} onChange={e => setViduMode(e.target.value as ViduMode)}>
                {viduModel === '3.2' ? <>
                  <option value="text">文生视频</option>
                  <option value="first-frame">首帧生视频</option>
                  <option value="first-last-frame">首尾帧生视频</option>
                  <option value="reference">参考生视频（1–7 张）</option>
                </> : <option value="reference">参考生视频（1–7 张）</option>}
              </select>
            </div>
            <div className="hf-fld">
              <label htmlFor="vidu-duration">时长（1–16 秒整数）</label>
              <input id="vidu-duration" className="wu-input" aria-label="Vidu 时长（秒）" type="number" min={1} max={16} step={1} value={viduDuration} onChange={e => setViduDuration(e.target.value)} />
            </div>
            <div className="hf-fld">
              <label htmlFor="vidu-resolution">分辨率</label>
              <select id="vidu-resolution" className="wu-input" aria-label="Vidu 分辨率" value={viduResolution} onChange={e => setViduResolution(e.target.value as ViduResolution)}>
                {(viduModel === '3.2' ? ['1080p'] : VIDU_Q4_RESOLUTIONS).map(value => <option key={value} value={value}>{value}</option>)}
              </select>
            </div>
            <div className="hf-fld">
              <label htmlFor="vidu-ratio">画幅</label>
              <select id="vidu-ratio" className="wu-input" aria-label="Vidu 画幅" value={viduRatio} onChange={e => setViduRatio(e.target.value as ViduRatio)}>
                {VIDU_RATIOS.map(value => <option key={value} value={value}>{value}</option>)}
              </select>
            </div>
          </div>
          <p className="wu-caption">Q3（3.2）支持文生、首帧、首尾帧和参考生视频，仅 1080p；Q4（3.4）仅支持参考生视频。首帧/首尾帧的实际画幅由输入图片决定。</p>
          {viduMode === 'text' ? <p className="wu-caption">文生视频不使用参考图像。</p> : <fieldset aria-label="Vidu 图像选择" style={{ border: 0, padding: 0, margin: '12px 0' }}>
            <legend>{viduMode === 'first-last-frame' ? '首尾帧图像（按选择顺序取前 2 张）' : viduMode === 'first-frame' ? '首帧图像（1 张）' : '参考图像（1–7 张）'}</legend>
            {!viduImages.length && <Alert tone="warning">工程尚无可用视觉资产，请先在阶段④上传或生成母版图像。</Alert>}
            <div className="wu-stack">
              {viduImages.map(image => {
                const checked = selectedImageArtifactIds.includes(image.artifactId);
                const selectionFull = (viduMode === 'reference' && selectedImageArtifactIds.length >= 7)
                  || (viduMode === 'first-last-frame' && selectedImageArtifactIds.length >= 2);
                return <label key={image.variantId} className="wu-row" style={{ gap: 8 }}>
                  <input type="checkbox" aria-label={`Vidu 图像 ${image.variantId}`} checked={checked} disabled={!checked && selectionFull} onChange={e => {
                    const nextChecked = e.target.checked;
                    setSelectedImageArtifactIds(current => {
                      if (viduMode === 'first-frame') return nextChecked ? [image.artifactId] : [];
                      if (!nextChecked) return current.filter(id => id !== image.artifactId);
                      if (current.includes(image.artifactId)) return current;
                      const limit = viduMode === 'first-last-frame' ? 2 : 7;
                      return current.length < limit ? [...current, image.artifactId] : current;
                    });
                  }} />
                  <span>{image.variantId} · {image.artifactId}</span>
                </label>;
              })}
            </div>
            {viduMode === 'first-last-frame' && <p className="wu-caption">首帧：{selectedImageArtifactIds[0] ?? '未选择'}；尾帧：{selectedImageArtifactIds[1] ?? '未选择'}。</p>}
          </fieldset>}
          <div className="hf-fld">
            <label className="wu-row" style={{ gap: 8 }}>
              <input type="checkbox" aria-label="叠加当前母轨" checked={withMasterAudio} disabled={!hasMasterAudio} onChange={e => setWithMasterAudio(e.target.checked)} />
              <span>叠加当前母轨（非口型同步）</span>
            </label>
            <input className="wu-input" aria-label="母轨起点秒（rangeStartSec）" type="number" min={0} step="0.1" value={rangeStartSec} disabled={!hasMasterAudio || !withMasterAudio} onChange={e => setRangeStartSec(e.target.value)} />
            <p className="wu-caption">{hasMasterAudio ? 'rangeStartSec 指定从当前母轨开始取音的秒数。' : '当前时间轨没有 masterAudioAssetId，母轨选项不可用。'}</p>
          </div>
          {viduConfigIssue && <Alert tone="warning">当前不能提交：{viduConfigIssue}</Alert>}
        </section>}
        {isLocalMode && viduActive && <Alert tone="warning">当前生效的视频提供方为 Vidu，本地生成已停用。请切换到「Vidu API 生成」，或在服务设置中改为 ComfyUI／演示并重启。</Alert>}
        {isLocalMode && proj&&<ProjectMotionBinding project={proj}/>}
        {isLocalMode && proj && <ImpactPlan projectId={projectId} revision={proj.revision}/>}
        {isLocalMode && proj && <DeliveryCheck projectId={projectId} revision={proj.revision}/>}
        {isLocalMode && simulated && <Alert tone="warning">当前为流程演示，不生成 MP4、WAV 或 SRT；下方分段为示意，真实模型效果与规格尚未验证。</Alert>}
        {isLocalMode && timeline && totalSecs > 300 && <Alert tone="danger">完整音轨超过 5 分钟（超出 {(totalSecs - 300).toFixed(1)} 秒），不会自动截断尾句。
          <a className="hf-link" onClick={() => setView('script')}>去阶段②精简内容</a>（建议优先拆分/删减最长独白）。</Alert>}
        {isLocalMode && timeline && timeline.revisionId !== proj?.currentDraftRevision && <Alert tone="warning">当前配音属于旧脚本，请先生成或采用当前版本配音。</Alert>}
        {/* P2-B（42 号方案）：成片过期横幅——指明需重跑并给一键入口 */}
        {isLocalMode && proj?.stageProgress.render === 'stale' && outputVersion && (
          <Alert tone="warning">
            已采用的成片 {outputVersion} 已过期：脚本、配音或画面在采用后发生了变化。
            <Button variant="secondary" size="sm" icon="refresh" disabled={!canRender} busy={rendering}
              onClick={() => handleRender()}>重新生成候选</Button>
            <InfoTip text="旧成片仍可导出；交付当前版本前请重新生成并采用。" />
          </Alert>
        )}
        {error && <Alert tone="danger" onClose={dismissError}>{error}</Alert>}
        {importNotice && <Alert tone="success" onClose={() => setImportNotice(null)}>{importNotice}</Alert>}
        {isLocalMode && !timeline && (
          <Alert tone="warning">
            还没有时间轨，无法渲染。请先在阶段③合成并确认配音。
            <a className="hf-link" onClick={() => setView('voice')}>前往阶段③ 试听配音</a>
          </Alert>
        )}
        {isLocalMode && !sampleApproved && timeline && (
            <Alert tone="warning">样片尚未确认；建议先完成样片确认。</Alert>
        )}
        {lastMeta && (
          <Alert tone="success">
            {recentRender?.result?.simulated ? '流程演示完成，无真实视频文件' : '当前正式视频'}：{outputVersion ?? lastMeta.outputVersion} · {lastMeta.resolution} @ {lastMeta.fps}fps。
          </Alert>
        )}
        {recentRender?.result && recentRender.result.simulated !== true && recentRender.result.artifactIds?.[0] && (
          <section className="hf-card">
            <div className="hf-card-t">当前采用：{outputVersion}<span className="hf-pill" data-tone="ok">已采用</span></div>
            <video ref={baselineVideo} className="hf-videoframe" controls preload="metadata"
              src={`/api/artifacts/${recentRender.result.artifactIds[0]}/file`} />
            <a className="wu-btn" data-variant="ghost" data-size="sm" href={`/api/artifacts/${recentRender.result.artifactIds[0]}/file`} download><Icon name="download" size={16} /><span className="wu-btn-label">下载完整视频</span></a>
          </section>
        )}
        {candidate?.result?.artifactIds?.[0] && <section className="hf-card">
          <div className="hf-card-t">{isDirectMode ? '候选视频' : isApiMode ? 'Vidu 候选视频' : '比较候选'}<span className="hf-pill" data-tone="warn">待人工审看</span></div>
          <label htmlFor="candidate-version">选择候选版本</label>
          <select id="candidate-version" className="wu-input" value={candidate.id} onChange={e => setCandidateId(e.target.value)}>
            {candidates.map(j => <option key={j.id} value={j.id}>{j.id} · {candidateLabel(j)}</option>)}
          </select>
          <p>{isDirectMode || isApiMode ? '待审看候选' : '待比较候选'} · {candidateLabel(candidate)}</p>
          <video ref={candidateVideo} className="hf-videoframe" controls preload="metadata" src={`/api/artifacts/${candidate.result.artifactIds[0]}/file`} />
          <a className="wu-btn" data-variant="ghost" data-size="sm" href={`/api/artifacts/${candidate.result.artifactIds[0]}/file`} download={`candidate-${candidate.id}.mp4`}><Icon name="download" size={16} /><span className="wu-btn-label">下载候选视频</span></a>
          {isLocalMode && candidateMeta?.generationStats?.wallSeconds != null && <p>快速模式：画面处理耗时 {candidateMeta.generationStats.wallSeconds.toFixed(1)} 秒</p>}
          {isLocalMode && candidateMeta?.generationStats && candidateMeta.generationStats.wallSeconds == null && <p>实际处理：新生成 {candidateMeta.generationStats.generated} 段 · 合成 {candidateMeta.generationStats.composited ?? 0} 段 · 缓存 {candidateMeta.generationStats.cached} 段 · 保留 {candidateMeta.generationStats.retained} 段</p>}
          {isLocalMode && !!candidateMeta?.continuityTransitions?.length && <details open><summary>同机位续接检查</summary>
            <p className="wu-caption">比较续接窗口内的最大帧间变化，仅用于定位跳变；口型、表情和光流形变仍需观看确认。</p>
            {candidateMeta.continuityTransitions.map((transition, i) => <div key={i}>
              <p>{formatTimecode(transition.frameAtSec ?? transition.atSec, { tenths: true })} · {transition.qualityCheck
                ? `窗口帧差 ${transition.qualityCheck.before.windowMaxMeanGrayChange.toFixed(2)} → ${transition.qualityCheck.after.windowMaxMeanGrayChange.toFixed(2)} · ${transition.qualityCheck.needsReview ? '变化增大，请复查' : '未检测到帧差增大，待画面确认'}`
                : '旧候选未记录质量检查，重新导出可补充'}</p>
              <Button variant="secondary" onClick={() => {
                const video = candidateVideo.current;
                if (video) { video.currentTime = Math.max(0, (transition.frameAtSec ?? transition.atSec) - .6); video.play().catch(e => setError(String(e))); }
              }}>播放续接 {i + 1}</Button>
            </div>)}
          </details>}
          {isLocalMode && !!candidateMeta?.automaticReferenceArtifactIds?.length && <details><summary>系统生成的倾听动作素材</summary>
            {candidateMeta.automaticReferenceArtifactIds.map((id, i) => <div key={id}><p>动作素材 {i+1} · 已登记资产</p><video controls preload="metadata" style={{width:'100%'}} src={`/api/artifacts/${id}/file`} /></div>)}
          </details>}
          <div style={{ display: 'flex', gap: 8 }}>
            {isLocalMode && <>
              <Button variant="secondary" disabled={!recentRender} onClick={() => {
                const before = baselineVideo.current, after = candidateVideo.current;
                if (before && after) { before.currentTime = 0; after.currentTime = 0; before.muted = true; after.muted = false; Promise.all([before.play(), after.play()]).catch(e => setError(String(e))); }
              }}>同步从头播放</Button>
              {typeof comparisonStart === 'number' && <Button variant="secondary" disabled={!recentRender} onClick={() => {
                const before = baselineVideo.current, after = candidateVideo.current;
                const start = comparisonStart;
                if (before && after) { before.currentTime = start; after.currentTime = start; before.muted = true; after.muted = false; Promise.all([before.play(), after.play()]).catch(e => setError(String(e))); }
              }}>{candidateMeta?.generationProfile === 'dialogue' ? '同步播放优化片段' : '同步播放重做片段'}</Button>}
            </>}
            <Button disabled={adopt.isPending} onClick={() => adopt.mutate({ projectId, jobId: candidate.id }, { onError: e => setError((e as Error).message) })}>采用候选</Button>
          </div>
        </section>}
        {!!versions.length && <section className="hf-card" style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <div className="hf-card-t" style={{ marginBottom: 0 }}>采用版本</div>
          <label htmlFor="output-version" className="wu-caption">当前版本</label>
          <select id="output-version" className="wu-input" value={outputVersion ?? ''} disabled={selectOutput.isPending} onChange={e => selectOutput.mutate({ projectId, version: e.target.value }, { onError: e => setError((e as Error).message) })}>
            {versions.map(v => <option key={v.version} value={v.version}>{v.version}</option>)}
          </select>
          {isLocalMode && <>
            <label htmlFor="redo-from">重做后续内容的起点</label>
            <select id="redo-from" className="wu-input" disabled={isFast} value={redoFromSec} onChange={e => setRedoFromSec(Number(e.target.value))}>
              {segments.map(s => <option key={s.id} value={s.outputRange[0]}>{formatTimecode(s.outputRange[0], { tenths: true })}</option>)}
            </select>
            <Button variant="secondary" disabled={isFast || isOverShoulder || !canRender || !standardSource}
              title="此功能从选定时间开始重做后续内容，并非只重做一个话轮。快速模式和过肩机位暂不支持。"
              onClick={() => handleRender(true)}>从此时间重做后续候选</Button>
          </>}
        </section>}

        {isLocalMode && <div className="hf-s5">
          {/* 开始生成前确认（01 §7.1） */}
          <section className="hf-summary">
            <div className="hf-summary-t">开始生成前确认 <InfoTip text={`点击「${simulated ? '运行流程演示' : '生成候选成片'}」时冻结输入快照。`} /></div>
            <div className="hf-summary-grid">
              <div className="hf-kv"><div className="k">{simulated ? '演示估算时长' : '实际时长'}</div><div className="v hf-mono">{timeline ? formatTimecode(totalSecs, { tenths: true }) : '—'}</div></div>
              <div className="hf-kv"><div className="k">画布</div><div className="v">{proj?.aspect === 'portrait' ? '竖屏 9:16' : '横屏 16:9'} · {resolution} 导出画布</div></div>
              <div className="hf-kv"><div className="k">语音来源</div><div className="v">{voiceSource}</div></div>
              <div className="hf-kv"><div className="k">视频模式</div><div className="v">{isFast ? '快速模式 · 双人演播室' : isOverShoulder ? '双机位过肩正反打' : '双人动态同框'}</div></div>
              <div className="hf-kv"><div className="k">分段</div><div className="v">{isFast ? '按整条配音更新口型' : isOverShoulder ? '按发言话轮，长话轮均匀分段' : demoRendered ? `演示完成 ${segments.length} 段（无真实输出）` : realRendered ? `已完成 ${segments.length} 段` : `复用 ${reusedCount} · 待生成 ${Math.max(0, segments.length - reusedCount)}`}</div></div>
              <div className="hf-kv"><div className="k">预估</div><div className="v">{renderJob ? '生成中' : '尚未测定'}</div></div>
              <div className="hf-kv"><div className="k">磁盘预算</div><div className="v">尚未测定</div></div>
              <div className="hf-kv">
                <div className="k">当前版本</div>
                <div className="v hf-mono">{timeline?.revisionId ?? 'R—'} / {latestVariant?.id ?? 'V—'} / {isOverShoulder ? '按话轮双机位' : segments.length ? `S${segments.length}` : 'S—'}</div>
              </div>
            </div>
          </section>

          {/* 分段进度网格（01 §7.2） */}
          <section>
            <div className="hf-zone-t">{isFast || isOverShoulder ? '生成进度' : '分段进度'}</div>
            {!isFast && !isOverShoulder && <p className="wu-caption" title="复用判定来自后端依赖哈希与片段完整性校验，实际执行状态见任务中心">复用状态由后端哈希校验判定；执行进度见任务中心。</p>}
            {isFast ? <p>{renderJob?.stage ?? (realRendered ? '快速模式成片已完成，可播放和下载。' : '等待按当前配音生成。')}</p> : isOverShoulder ? <p>{renderJob?.stage ?? (otsCompleted ? '过肩视频已完成，可在候选区播放比较。' : '按发言话轮生成 A/B 机位，等待生成。')}</p> : segments.length === 0 ? (
              <Alert tone="muted">合成时间轨后在此展示分段计划。</Alert>
            ) : (
              <div className="hf-seg-grid">
                {segments.map((sg, i) => {
                const planRow = segmentPlan.data?.available ? segmentPlan.data.segments[i] : undefined;
                const reused = planRow?.reusable ?? false;
                  const dur = sg.renderRange[1] - sg.renderRange[0];
                  return (
                    <div className="hf-segcard" key={sg.id} title={planRow?.reason}>
                      <div className="top">
                        {demoRendered
                          ? <Badge tone="info">演示完成</Badge>
                          : <Badge tone={reused ? 'success' : 'muted'}>{realRendered ? '已完成' : reused ? '复用' : '待生成'}</Badge>}
                        <span className="tt">#{String(i + 1).padStart(2, '0')} {segLabel(sg, i, segments.length, speakers)}</span>
                      </div>
                      <div className="sub">
                        {formatTimecode(dur, { tenths: true })} · {reused ? '快照一致 ✓' : demoRendered ? '流程演示 · 无真实视频' : `数字人动态视频 · ${resolutionId}`}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 导出区（01 §7.3） */}
          <section className="hf-export">
            <div className="hf-zone-t">导出</div>
            <div className="hf-export-item">
              <b>MP4</b>
              <span className="hf-spacer" />
              <span className="wu-caption">成片 · {resolutionId} · {CODEC}</span>
            </div>
            <div className="hf-export-item">
              <b>混音 WAV</b>
              <span className="hf-spacer" />
              <span className="wu-caption">{timeline?.sampleRate ?? 48000}Hz 主轨</span>
            </div>
            <div className="hf-export-item">
              <b>A/B 分轨</b>
              <span className="hf-spacer" />
              <span className="wu-caption">独立音轨</span>
            </div>
            <div className="hf-export-item">
              <b>SRT 字幕</b>
              <span className="hf-spacer" />
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
              {proj?.outputManifest ? <a className="wu-btn" data-variant="brand" data-size="sm" href={`/api/projects/${projectId}/export/package`} download><Icon name="download" size={16} /><span className="wu-btn-label">下载交付包</span></a> : <span className="wu-caption">新版成片生成后可下载完整交付包</span>}
              <span className="wu-caption">{outputVersion ? `已导出 ${outputVersion}` : demoRendered ? '演示产物 · 无真实文件可导出' : '需先生成成片'}</span>
            </div>
          </section>
        </div>}
      </div>
    </AppShell>
  );
}
