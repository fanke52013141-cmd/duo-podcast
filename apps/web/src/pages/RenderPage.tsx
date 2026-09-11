/* ============================================================================
   HF-05 · 阶段⑤ 生成导出（真实数据流，01 §7 权威）
   开始前摘要（输入快照 / 复用与待生成段 / 预估 / 磁盘预算）→ 分段进度网格 → 导出区
   检查器：导出规格 / 后处理顺序 / 竖屏导出
   注意：没有测速数据时显示「尚未测定」，不编造预估（01 §7.1 明确要求）。
   ========================================================================== */
import { useMemo, useState } from 'react';
import {
  useJobs, useProject, useRenderGenerate,
} from '../lib/api';
import type { AudioTimeline, Job, Project, SynthesisUnit } from '../lib/types';
import { Alert, Badge, Button, Choice, Icon } from '../components/wu';
import { AppShell, FootBar, StageHead, TimelineBar } from '../components/AppShell';
import { useProjectStore, useServiceStore } from '../stores';

const RESOLUTION_OPTIONS = [
  { id: '480p', label: '480p（快）', value: '854x480' },
  { id: '720p', label: '720p', value: '1280x720' },
  { id: '1080p', label: '1080p（慢）', value: '1920x1080' },
];

const CODECS = ['H.264', 'H.265'];
const FPSS = [30, 24];

interface Segment {
  id: string;
  units: SynthesisUnit[];
  outputRange: [number, number];
  renderRange: [number, number];
  contextRange: [number, number];
}

/** 演示性分段：按总时长切 ≤60s 一段，展示三段式区间（权威区间在渲染阶段落位）。 */
function planSegments(timeline: AudioTimeline): Segment[] {
  const sr = timeline.sampleRate || 48000;
  const sec = (s: number) => s / sr;
  const units = timeline.units;
  if (!units.length) return [];
  const total = sec(timeline.sampleCount || 0);
  const chunk = Math.max(1, Math.ceil(units.length / Math.max(1, Math.round(total / 60))));
  const out: Segment[] = [];
  for (let i = 0; i < units.length; i += chunk) {
    const slice = units.slice(i, i + chunk);
    const start = sec(timeline.unitOffsets[slice[0].id] ?? 0);
    const end = i + chunk < units.length ? sec(timeline.unitOffsets[units[i + chunk].id] ?? 0) : total;
    const ctx = 8;
    out.push({
      id: `S${String(out.length + 1).padStart(2, '0')}`,
      units: slice,
      outputRange: [i === 0 ? 0 : start, end],
      renderRange: [i === 0 ? 0 : start, end],
      contextRange: [Math.max(0, start - ctx), Math.min(total, end + ctx)],
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
  const setView = useProjectStore((s) => s.setView);
  const simulated = useServiceStore((s) => s.capabilities?.video.mode === 'mock');

  const proj: Project | undefined = project.data;
  const [resolutionId, setResolutionId] = useState('480p');
  const [codec, setCodec] = useState(CODECS[0]);
  const [fps, setFps] = useState(30);
  const [interpolate, setInterpolate] = useState(false);
  const [burnSubtitle, setBurnSubtitle] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;
  const segments = useMemo(() => (timeline ? planSegments(timeline) : []), [timeline]);
  const outputVersion = proj?.outputVersion ?? proj?.selectedOutputVersion ?? null;
  const sampleApproved = proj?.approvals.some((a) => a.kind === 'sample' && a.decision === 'accepted') ?? false;
  const latestVariant = proj?.visualVariants?.[proj.visualVariants.length - 1] ?? null;

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

  const handleRender = () => {
    if (!proj || !timeline) return;
    setError(null);
    render.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      revisionId: timeline.revisionId,
      resolution,
      fps,
    }, { onError: (e) => setError((e as Error).message) });
  };

  const recentRender = (jobs.data ?? []).find((j) => j.kind === 'renders' && j.status === 'succeeded');
  const lastMeta = recentRender?.result?.meta as { outputVersion?: string; resolution?: string; fps?: number } | undefined;

  const voiceSource = simulated ? '模拟语音' : proj?.voiceBindings?.[0]?.providerProfileId === 'minimax' ? 'MiniMax' : '本地引擎';
  const reusedCount = 0; // 未取得后端哈希校验计划，不能把存在旧输出当作可复用。

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      timeline={
        <TimelineBar
          timeline={timeline}
          labels={[segments.length ? `分段边界：${segments.map((_, i) => i + 1).join(' / ')}` : '分段边界：未规划', 'A 轨 / B 轨']}
          speakerOf={(turnId) => speakers.get(turnId) ?? 'A'}
        />
      }
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>导出规格</h4>
            <div className="hf-fld">
              <label>画布</label>
              <input
                className="wu-input" readOnly
                value={proj?.aspect === 'portrait' ? '9:16 导出画布' : '480p → 16:9 导出画布'}
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
              <Choice type="checkbox" checked={interpolate} onChange={setInterpolate} label="补帧（可选）" />
              <Choice type="checkbox" checked={burnSubtitle} onChange={setBurnSubtitle} label="烧录字幕（可选）" />
            </div>
          </div>

          <div className="hf-ins-sec">
            <h4>后处理顺序</h4>
            <p className="wu-caption" style={{ lineHeight: 1.8 }}>
              基础视频 → 按保留区间合成 → 镜头裁切与版式 → 可选补帧 → 可选烧录字幕 → 合并权威音轨 → 编码 → 校验。<br />
              导出失败只重跑相应后处理或编码，不重新生成全部口型视频。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>竖屏导出</h4>
            <p className="wu-caption" style={{ lineHeight: 1.8 }}>
              先展示裁切预览；人物无法同时保留时提供上下双窗或整幅嵌入竖屏画布；能后期实现的只重跑后处理。
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
          输入快照 {timeline?.revisionId ?? 'R—'} / {latestVariant?.id ?? 'V—'} / {segments.length ? `S${segments.length}` : 'S—'} · {outputVersion ? '已冻结' : '未冻结'}
        </Badge>
        <span className="hf-spacer" />
        <Button
          variant="brand" icon="play" busy={rendering} disabled={!canRender}
          onClick={handleRender}
        >{rendering ? '执行中' : simulated ? '运行流程演示' : outputVersion ? '重新生成' : '开始生成'}</Button>
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
            {recentRender?.result?.simulated ? '流程演示完成，无真实视频文件' : '渲染完成'}：{lastMeta.outputVersion} · {lastMeta.resolution} @ {lastMeta.fps}fps。
          </Alert>
        )}

        <div className="hf-s5">
          {/* 开始生成前确认（01 §7.1） */}
          <section className="hf-summary">
            <div className="hf-summary-t">开始生成前确认 <span className="wu-caption">输入快照将在点击「开始生成」时冻结</span></div>
            <div className="hf-summary-grid">
              <div className="hf-kv"><div className="k">{simulated ? '演示估算时长' : '实际时长'}</div><div className="v hf-mono">{timeline ? fmtT(totalSecs) : '—'}</div></div>
              <div className="hf-kv"><div className="k">画布</div><div className="v">{proj?.aspect === 'portrait' ? '竖屏 9:16' : '横屏 16:9'} · {resolution} 导出画布</div></div>
              <div className="hf-kv"><div className="k">语音来源</div><div className="v">{voiceSource}</div></div>
              <div className="hf-kv"><div className="k">视频模式</div><div className="v">双人动态同框</div></div>
              <div className="hf-kv"><div className="k">分段</div><div className="v">复用 {reusedCount} · 待生成 {Math.max(0, segments.length - reusedCount)}</div></div>
              <div className="hf-kv"><div className="k">预估</div><div className="v">{renderJob ? '生成中' : '尚未测定'}</div></div>
              <div className="hf-kv"><div className="k">磁盘预算</div><div className="v">尚未测定</div></div>
              <div className="hf-kv">
                <div className="k">当前版本</div>
                <div className="v hf-mono">{timeline?.revisionId ?? 'R—'} / {latestVariant?.id ?? 'V—'} / {segments.length ? 'S2' : 'S—'}</div>
              </div>
            </div>
          </section>

          {/* 分段进度网格（01 §7.2） */}
          <section>
            <div className="hf-zone-t">分段进度 <span className="wu-caption">基础视频 → 保留区间合成 → 镜头裁切与版式 → 可选补帧 → 可选烧录字幕 → 合并音轨 → 编码 → 校验（02 §6.5）</span></div>
            {segments.length === 0 ? (
              <Alert tone="muted">合成时间轨后在此展示分段计划。</Alert>
            ) : (
              <div className="hf-seg-grid">
                {segments.map((sg, i) => {
                const reused = false;
                  const dur = sg.renderRange[1] - sg.renderRange[0];
                  return (
                    <div className="hf-segcard" key={sg.id}>
                      <div className="top">
                        <Badge tone={reused ? 'success' : 'muted'}>{reused ? '复用' : '待生成'}</Badge>
                        <span className="tt">#{String(i + 1).padStart(2, '0')} {segLabel(sg, i, segments.length, speakers)}</span>
                      </div>
                      <div className="sub">
                        {fmtT(dur)} · {reused ? '快照一致 ✓' : `口型视频 · ${resolutionId}`}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 导出区（01 §7.3） */}
          <section className="hf-export">
            <div className="hf-zone-t">导出 <span className="wu-caption">正式产物使用不可变版本路径，不覆盖同名 final.mp4</span></div>
            <div className="hf-export-item">
              <b>MP4</b>
              <span className="hf-spacer" />
              <span className="wu-caption">成片 · {resolutionId} · {codec}</span>
            </div>
            <div className="hf-export-item">
              <b>混音 WAV</b>
              <span className="hf-spacer" />
              <span className="wu-caption">{timeline?.sampleRate ?? 48000}Hz 主轨 · 样本次数权威</span>
            </div>
            <div className="hf-export-item">
              <b>A/B 分轨</b>
              <span className="hf-spacer" />
              <span className="wu-caption">独立音轨</span>
            </div>
            <div className="hf-export-item">
              <b>SRT 字幕</b>
              <Choice type="checkbox" checked={burnSubtitle} onChange={setBurnSubtitle} label="烧录字幕（可选，仅影响对应后处理）" />
              <span className="hf-spacer" />
              <span className="wu-caption">显示文本不含引擎专用标签</span>
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
              <Button variant="secondary" size="sm" disabled title="接入桌面集成后可打开输出目录">打开输出目录</Button>
              <Button size="sm" disabled={!outputVersion} onClick={() => setView('assets')}>导出</Button>
              <span className="wu-caption">{outputVersion ? `已导出 ${outputVersion}` : '需先生成成片'}</span>
            </div>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
