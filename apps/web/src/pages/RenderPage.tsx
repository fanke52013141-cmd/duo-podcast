/* ============================================================================
   HF-05 · 阶段⑤ 生成导出（真实数据流，01 §7 权威）
   开始前摘要（输入快照 / 复用与待生成段 / 预估 / 磁盘预算）→ 分段进度网格 → 导出区
   检查器：导出规格 / 后处理顺序 / 竖屏导出
   注意：没有测速数据时显示「尚未测定」，不编造预估（01 §7.1 明确要求）。
   ========================================================================== */
import { useEffect, useMemo, useState, useRef } from 'react';
import {
  useJobs, useProject, useRenderGenerate, useRenderAdopt, useOutputSelect, usePatchProject, type ListeningPlan,
} from '../lib/api';
import type { AudioTimeline, Job, Project, SynthesisUnit } from '../lib/types';
import { Alert, Badge, Button, Icon } from '../components/wu';
import { AppShell, FootBar, StageHead, TimelineBar } from '../components/AppShell';
import { useProjectStore, useServiceStore } from '../stores';

const RESOLUTION_OPTIONS = [
  { id: '480p', label: '480p', value: '854x480' },
  { id: '720p', label: '720p', value: '1280x720' },
  { id: '1080p', label: '1080p', value: '1920x1080' },
];

const CODECS = ['H.264'];
const FPSS = [25, 30];

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

function fmtT(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const ds = Math.floor((secs - Math.floor(secs)) * 10);
  return `${m}:${String(s).padStart(2, '0')}.${ds}`;
}

function candidateLabel(job: Job): string {
  const meta = job.result?.meta as { generationProfile?: string; performanceMode?: string; visualMode?: string } | undefined;
  if (meta?.visualMode === 'overShoulder') return '过肩正反打';
  return meta?.generationProfile === 'dialogue' ? '按对话倾听' : meta?.generationProfile === 'listener' ? '分层倾听' : meta?.generationProfile === 'reference' ? '动作参考' : meta?.generationProfile === 'matched' ? '模型匹配' : meta?.performanceMode === 'adaptive' ? '角色编排' : '原有配置';
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
  const render = useRenderGenerate();
  const adopt = useRenderAdopt();
  const selectOutput = useOutputSelect();
  const patchProject = usePatchProject();
  const baselineVideo = useRef<HTMLVideoElement>(null);
  const candidateVideo = useRef<HTMLVideoElement>(null);
  const [redoFromSec, setRedoFromSec] = useState(0);
  const [candidateId, setCandidateId] = useState('');
  const setView = useProjectStore((s) => s.setView);
  const simulated = useServiceStore((s) => s.capabilities?.video.mode === 'mock');

  const proj: Project | undefined = project.data;
  const [resolutionId, setResolutionId] = useState('480p');
  const [codec, setCodec] = useState(CODECS[0]);
  const [fps, setFps] = useState(25);
  const [error, setError] = useState<string | null>(null);

  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;
  const segments = useMemo(() => (timeline ? planSegments(timeline) : []), [timeline]);
  const outputVersion = proj?.selectedOutputVersion ?? proj?.outputVersion ?? null;
  const sampleApproved = proj?.approvals.some((a) => a.kind === 'sample' && a.decision === 'accepted') ?? false;
  const eligibleVariants = proj?.visualVariants.filter(v => v.aspect === (proj.aspect ?? 'landscape') && (v.mode !== 'overShoulder' || v.cameraGroups?.some(g => g.cameraAssets.some(c => c.subjectSpeaker === 'A') && g.cameraAssets.some(c => c.subjectSpeaker === 'B')))) ?? [];
  const latestVariant = eligibleVariants.find(v => v.id === proj?.selectedVisualVariantId) ?? eligibleVariants[eligibleVariants.length - 1] ?? null;

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
  const canRender = !!proj && !!timeline && timeline.revisionId === proj.currentDraftRevision
    && timeline.sampleCount > 0 && timeline.sampleCount <= 300 * timeline.sampleRate && !rendering;
  const resolution = RESOLUTION_OPTIONS.find((r) => r.id === resolutionId)?.value ?? '854x480';
  const totalSecs = timeline ? timeline.sampleCount / (timeline.sampleRate || 48000) : 0;
  const isOverShoulder = latestVariant?.mode === 'overShoulder';
  const standardSource = !!proj?.outputManifest
    && (proj.outputManifest.meta.performanceMode ?? 'legacy') === 'legacy'
    && (proj.outputManifest.meta.generationProfile ?? 'accepted') === 'accepted';

  const handleRender = (localRedo = false) => {
    if (!proj || !timeline) return;
    setError(null);
    render.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      revisionId: timeline.revisionId,
      visualVariantId: proj.selectedVisualVariantId ?? latestVariant?.id,
      cameraMode: isOverShoulder ? 'speaker' : proj.cameraMode ?? 'speaker',
      resolution,
      fps,
      performanceMode: 'legacy',
      generationProfile: 'accepted',
      candidate: true,
      reuseMotionArtifactId: !isOverShoulder && standardSource ? proj.outputManifest?.artifactIds[0] : undefined,
      redoFromSec: localRedo ? redoFromSec : undefined,
    }, { onError: (e) => setError((e as Error).message) });
  };

  const recentRender = (jobs.data ?? []).find((j) => j.kind === 'renders' && j.status === 'succeeded' && (!proj?.outputManifest || j.result?.artifactIds?.includes(proj.outputManifest.artifactIds[0])));
  const adoptedArtifacts = new Set([...(proj?.outputHistory ?? []), ...(proj?.outputManifest ? [proj.outputManifest] : [])].flatMap(v => v.artifactIds));
  const candidates = (jobs.data ?? []).filter(j => j.kind === 'renders' && j.status === 'succeeded' && (j.result?.meta as { candidate?: boolean } | undefined)?.candidate === true && !adoptedArtifacts.has(j.result?.artifactIds?.[0] ?? ''));
  const otsCompleted = (jobs.data ?? []).find(j => j.kind === 'renders' && j.status === 'succeeded' && j.inputSnapshot.visualVariantId === latestVariant?.id && (j.result?.meta as { visualMode?: string } | undefined)?.visualMode === 'overShoulder');
  const candidate = candidates.find(j => j.id === candidateId) ?? candidates[0];
  const candidateMeta = candidate?.result?.meta as { generationStats?: { generated: number; composited?: number; cached: number; retained: number }; performanceMode?: string; generationProfile?: string; automaticReferenceArtifactIds?: string[]; listeningPlan?: ListeningPlan; continuityTransitions?: { atSec: number; frameAtSec?: number; cameraAssetId: string; qualityCheck?: { before: { windowMaxMeanGrayChange: number }; after: { windowMaxMeanGrayChange: number }; needsReview: boolean } }[] } | undefined;
  const comparisonStart = typeof candidate?.inputSnapshot.redoFromSec === 'number' ? candidate.inputSnapshot.redoFromSec : candidateMeta?.listeningPlan?.segments.find(s => s.eligible)?.start;
  const versions = [...new Map([...(proj?.outputHistory ?? []), ...(proj?.outputManifest ? [proj.outputManifest] : [])].map(v => [v.version, v])).values()];
  const lastMeta = recentRender?.result?.meta as { outputVersion?: string; resolution?: string; fps?: number } | undefined;
  // 演示渲染已跑过（模拟通道）：分段卡与快照状态按「演示完成」展示，不再永远停在「待生成」
  const demoRendered = recentRender?.result?.simulated === true;
  const realRendered = !!recentRender && !demoRendered;
  useEffect(() => {
    if (lastMeta?.resolution) setResolutionId(RESOLUTION_OPTIONS.find(r => r.value === lastMeta.resolution)?.id ?? '480p');
    if (lastMeta?.fps) setFps(lastMeta.fps);
  }, [recentRender?.id]);



  const voiceSource = simulated ? '模拟语音' : proj?.voiceBindings?.[0]?.providerProfileId === 'minimax' ? 'MiniMax' : '本地引擎';
  const reusedCount = 0; // 未取得后端哈希校验计划，不能把存在旧输出当作可复用。

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      timeline={
        <TimelineBar
          timeline={timeline}
          labels={[isOverShoulder ? '过肩镜头按配音话轮切换' : segments.length ? `分段边界：${segments.map((_, i) => i + 1).join(' / ')}` : '分段边界：未规划', 'A 轨 / B 轨']}
          speakerOf={(turnId) => speakers.get(turnId) ?? 'A'}
        />
      }
      inspector={
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
                value={proj?.aspect === 'portrait' ? '9:16 导出画布' : `${resolutionId} → 16:9 导出画布`}
              />
            </div>
            <div className="hf-fld">
              <label>分辨率</label>
              <select className="wu-input" aria-label="分辨率" value={resolutionId} onChange={(e) => setResolutionId(e.target.value)}>
                {RESOLUTION_OPTIONS.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
              </select>
            </div>
            <div className="hf-fld">
              <label>帧率</label>
              <select className="wu-input" aria-label="帧率" value={fps} onChange={(e) => setFps(Number(e.target.value))}>
                {FPSS.map((f) => <option key={f} value={f}>{f} fps</option>)}
              </select>
            </div>
            <div className="hf-fld">
              <label>编码</label>
              <select className="wu-input" aria-label="编码" value={codec} onChange={(e) => setCodec(e.target.value)}>
                {CODECS.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div style={{ display: 'grid', gap: 6, marginTop: 8 }}>
              <span className="wu-caption">补帧：当前引擎未开放</span>
              <span className="wu-caption">烧录字幕：当前引擎未开放</span>
            </div>
          </div>

          <div className="hf-ins-sec">
            <h4>后处理顺序</h4>
            <p className="wu-caption" style={{ lineHeight: 1.8 }}>
              基础视频 → 镜头裁切 → 合并配音 → 编码 → 画面与声音校验。<br />
              导出失败只重跑相应后处理或编码，不重新生成全部口型视频。
            </p>
          </div>
          {!isOverShoulder && proj?.cameraMode !== 'twoShot' && <div className="hf-ins-sec">
            <h4>话轮机位</h4>
            <p className="wu-caption">自动模式按整期对话编排；短接话合并到相邻镜头。</p>
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
            <h4>竖屏导出</h4>
            <p className="wu-caption" style={{ lineHeight: 1.8 }}>
              竖屏布局尚未开放；当前提供横屏双人同框与双方特写。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>阶段状态</h4>
            <ul className="hf-ins-list">
              <li>脚本确认：{proj?.approvals.some((a) => a.kind === 'script' && a.decision === 'accepted') ? '已通过' : '未确认'}</li>
              <li>配音确认：{proj?.approvals.some((a) => a.kind === 'voice' && a.decision === 'accepted') ? '已通过' : '未确认'}</li>
              <li>样片确认：{sampleApproved ? '已通过' : '未确认'}</li>
            </ul>
          </div>
        </aside>
      }
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('visual')}>上一步</Button>
            <Badge tone={outputVersion ? 'success' : 'muted'} icon={outputVersion ? 'check' : 'clock'}>
              输出版本{outputVersion ? ` · ${outputVersion}` : ' · 未导出'}
            </Badge>
            <span className="hf-spacer" />
            <span className="wu-caption">导出完成后可从资产库查看产物</span>
            <Button size="sm" onClick={() => setView('assets')}>查看资产库<Icon name="arrowRight" size={14} /></Button>
          </span>
        } />
      }
    >
      <StageHead title="⑤ 生成导出">
        <Badge tone="muted">
          输入快照 {timeline?.revisionId ?? 'R—'} / {latestVariant?.id ?? 'V—'} / {isOverShoulder ? '过肩双机位' : segments.length ? `S${segments.length}` : 'S—'} · {outputVersion ? '已冻结' : demoRendered ? '演示完成 · 未冻结' : '未冻结'}
        </Badge>
        <span className="hf-spacer" />
        <Button
          variant="brand" icon="play" busy={rendering} disabled={!canRender}
          onClick={() => handleRender()}
        >{rendering ? '执行中' : simulated ? '运行流程演示' : '生成候选成片'}</Button>
      </StageHead>

      <div className="hf-body">
        {simulated && <Alert tone="warning">当前为流程演示，不生成 MP4、WAV 或 SRT；下方分段为示意，真实模型效果与规格尚未验证。</Alert>}
        {timeline && totalSecs > 300 && <Alert tone="danger">完整音轨超过 5 分钟，请先精简内容，不会自动截断尾句。</Alert>}
        {timeline && timeline.revisionId !== proj?.currentDraftRevision && <Alert tone="warning">当前配音属于旧脚本，请先生成或采用当前版本配音。</Alert>}
        {error && <Alert tone="danger">{error}</Alert>}
        {!timeline && (
          <Alert tone="warning">
            还没有时间轨，无法渲染。请先在阶段③合成并确认配音。
            <a className="hf-link" onClick={() => setView('voice')}>前往阶段③ 试听配音</a>
          </Alert>
        )}
        {!sampleApproved && timeline && (
          <Alert tone="warning">样片尚未确认；渲染将使用当前草稿脚本与最新主图，建议先完成样片确认。</Alert>
        )}
        {lastMeta && (
          <Alert tone="success">
            {recentRender?.result?.simulated ? '流程演示完成，无真实视频文件' : '当前正式视频'}：{outputVersion ?? lastMeta.outputVersion} · {lastMeta.resolution} @ {lastMeta.fps}fps。
          </Alert>
        )}
        {recentRender?.result?.simulated === false && recentRender.result.artifactIds?.[0] && (
          <section>
            <p>当前采用：{outputVersion}</p>
            <video ref={baselineVideo} controls preload="metadata" style={{ width: '100%', maxHeight: 540 }}
              src={`/api/artifacts/${recentRender.result.artifactIds[0]}/file`} />
            <a href={`/api/artifacts/${recentRender.result.artifactIds[0]}/file`} download>下载完整视频</a>
          </section>
        )}
        {candidate?.result?.artifactIds?.[0] && <section style={{ margin: '16px 0' }}>
          <label htmlFor="candidate-version">比较候选</label>
          <select id="candidate-version" className="wu-input" value={candidate.id} onChange={e => setCandidateId(e.target.value)}>
            {candidates.map(j => <option key={j.id} value={j.id}>{j.id} · {candidateLabel(j)}</option>)}
          </select>
          <p>待比较候选 · {candidateLabel(candidate)} · 画面待人工验收</p>
          <video ref={candidateVideo} controls preload="metadata" style={{ width: '100%', maxHeight: 540 }} src={`/api/artifacts/${candidate.result.artifactIds[0]}/file`} />
          <a className="hf-link" href={`/api/artifacts/${candidate.result.artifactIds[0]}/file`} download={`candidate-${candidate.id}.mp4`}>下载候选视频</a>
          {candidateMeta?.generationStats && <p>实际处理：新生成 {candidateMeta.generationStats.generated} 段 · 合成 {candidateMeta.generationStats.composited ?? 0} 段 · 缓存 {candidateMeta.generationStats.cached} 段 · 保留 {candidateMeta.generationStats.retained} 段</p>}
          {!!candidateMeta?.continuityTransitions?.length && <details open><summary>同机位续接检查</summary>
            <p className="wu-caption">比较续接窗口内的最大帧间变化，仅用于定位跳变；口型、表情和光流形变仍需观看确认。</p>
            {candidateMeta.continuityTransitions.map((transition, i) => <div key={i}>
              <p>{fmtT(transition.frameAtSec ?? transition.atSec)} · {transition.qualityCheck
                ? `窗口帧差 ${transition.qualityCheck.before.windowMaxMeanGrayChange.toFixed(2)} → ${transition.qualityCheck.after.windowMaxMeanGrayChange.toFixed(2)} · ${transition.qualityCheck.needsReview ? '变化增大，请复查' : '未检测到帧差增大，待画面确认'}`
                : '旧候选未记录质量检查，重新导出可补充'}</p>
              <Button variant="secondary" onClick={() => {
                const video = candidateVideo.current;
                if (video) { video.currentTime = Math.max(0, (transition.frameAtSec ?? transition.atSec) - .6); video.play().catch(e => setError(String(e))); }
              }}>播放续接 {i + 1}</Button>
            </div>)}
          </details>}
          {!!candidateMeta?.automaticReferenceArtifactIds?.length && <details><summary>系统生成的倾听动作素材</summary>
            {candidateMeta.automaticReferenceArtifactIds.map((id, i) => <div key={id}><p>动作素材 {i+1} · 已登记资产</p><video controls preload="metadata" style={{width:'100%'}} src={`/api/artifacts/${id}/file`} /></div>)}
          </details>}
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="secondary" disabled={!recentRender} onClick={() => {
              const before = baselineVideo.current, after = candidateVideo.current;
              if (before && after) { before.currentTime = 0; after.currentTime = 0; before.muted = true; after.muted = false; Promise.all([before.play(), after.play()]).catch(e => setError(String(e))); }
            }}>同步从头播放</Button>
            {typeof comparisonStart === 'number' && <Button variant="secondary" disabled={!recentRender} onClick={() => {
              const before = baselineVideo.current, after = candidateVideo.current;
              const start = comparisonStart;
              if (before && after) { before.currentTime = start; after.currentTime = start; before.muted = true; after.muted = false; Promise.all([before.play(), after.play()]).catch(e => setError(String(e))); }
            }}>{candidateMeta?.generationProfile === 'dialogue' ? '同步播放优化片段' : '同步播放重做片段'}</Button>}
            <Button disabled={adopt.isPending} onClick={() => adopt.mutate({ projectId, jobId: candidate.id }, { onError: e => setError((e as Error).message) })}>采用候选</Button>
          </div>
        </section>}
        {!!versions.length && <section style={{ margin: '16px 0', display: 'flex', gap: 12, alignItems: 'center' }}>
          <label htmlFor="output-version">采用版本</label>
          <select id="output-version" className="wu-input" value={outputVersion ?? ''} disabled={selectOutput.isPending} onChange={e => selectOutput.mutate({ projectId, version: e.target.value }, { onError: e => setError((e as Error).message) })}>
            {versions.map(v => <option key={v.version} value={v.version}>{v.version}</option>)}
          </select>
          <label htmlFor="redo-from">重做起点</label>
          <select id="redo-from" className="wu-input" value={redoFromSec} onChange={e => setRedoFromSec(Number(e.target.value))}>
            {segments.map(s => <option key={s.id} value={s.outputRange[0]}>{fmtT(s.outputRange[0])}</option>)}
          </select>
          <Button variant="secondary" disabled={isOverShoulder || !canRender || !standardSource} onClick={() => handleRender(true)}>从此段重做候选</Button>
        </section>}

        <div className="hf-s5">
          {/* 开始生成前确认（01 §7.1） */}
          <section className="hf-summary">
            <div className="hf-summary-t">开始生成前确认 <span className="wu-caption">点击「开始生成」时冻结输入快照</span></div>
            <div className="hf-summary-grid">
              <div className="hf-kv"><div className="k">{simulated ? '演示估算时长' : '实际时长'}</div><div className="v hf-mono">{timeline ? fmtT(totalSecs) : '—'}</div></div>
              <div className="hf-kv"><div className="k">画布</div><div className="v">{proj?.aspect === 'portrait' ? '竖屏 9:16' : '横屏 16:9'} · {resolution} 导出画布</div></div>
              <div className="hf-kv"><div className="k">语音来源</div><div className="v">{voiceSource}</div></div>
              <div className="hf-kv"><div className="k">视频模式</div><div className="v">{isOverShoulder ? '双机位过肩正反打' : '双人动态同框'}</div></div>
              <div className="hf-kv"><div className="k">分段</div><div className="v">{isOverShoulder ? '按发言话轮，长话轮均匀分段' : demoRendered ? `演示完成 ${segments.length} 段（无真实输出）` : realRendered ? `已完成 ${segments.length} 段` : `复用 ${reusedCount} · 待生成 ${Math.max(0, segments.length - reusedCount)}`}</div></div>
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
            <div className="hf-zone-t">分段进度</div>
            {isOverShoulder ? <p>{renderJob?.stage ?? (otsCompleted ? '过肩视频已完成，可在候选区播放比较。' : '按发言话轮生成 A/B 机位，等待生成。')}</p> : segments.length === 0 ? (
              <Alert tone="muted">合成时间轨后在此展示分段计划。</Alert>
            ) : (
              <div className="hf-seg-grid">
                {segments.map((sg, i) => {
                const reused = false;
                  const dur = sg.renderRange[1] - sg.renderRange[0];
                  return (
                    <div className="hf-segcard" key={sg.id}>
                      <div className="top">
                        {demoRendered
                          ? <Badge tone="info">演示完成</Badge>
                          : <Badge tone={reused ? 'success' : 'muted'}>{realRendered ? '已完成' : reused ? '复用' : '待生成'}</Badge>}
                        <span className="tt">#{String(i + 1).padStart(2, '0')} {segLabel(sg, i, segments.length, speakers)}</span>
                      </div>
                      <div className="sub">
                        {fmtT(dur)} · {reused ? '快照一致 ✓' : demoRendered ? '流程演示 · 无真实视频' : `数字人动态视频 · ${resolutionId}`}
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
              <span className="wu-caption">成片 · {resolutionId} · {codec}</span>
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
              <Button variant="secondary" size="sm" disabled title="接入桌面集成后可打开输出目录">打开输出目录</Button>
              {proj?.outputManifest ? <a className="wu-btn wu-btn-brand" href={`/api/projects/${projectId}/export/package`} download>下载交付包</a> : <span className="wu-caption">新版成片生成后可下载完整交付包</span>}
              <span className="wu-caption">{outputVersion ? `已导出 ${outputVersion}` : '需先生成成片'}</span>
            </div>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
