/* ============================================================================
   HF-05 · 阶段⑤ 生成导出（真实数据流，01 §7 权威）
   主视图：分段计划（三段式区间 outputRange/renderRange/contextRange）+ 渲染控制
   输出：OUT-Rn-vN 版本登记；分段为演示性展示，权威区间由渲染阶段产出
   ========================================================================== */
import { useMemo, useState } from 'react';
import {
  useJobs, useProject, useRenderGenerate,
} from '../lib/api';
import type { AudioTimeline, Job, Project, SynthesisUnit } from '../lib/types';
import { Alert, Badge, Button, Icon, Progress } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const RESOLUTIONS = ['1920x1080', '1280x720'];
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
    const end = start + slice.reduce((n, u) => n + sec(u.sampleCount), 0);
    const ctx = 8;
    out.push({
      id: `S${String(out.length + 1).padStart(2, '0')}`,
      units: slice,
      outputRange: [start, end],
      renderRange: [start, end],
      contextRange: [Math.max(0, start - ctx), Math.min(total, end + ctx)],
    });
  }
  return out;
}

function fmtT(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function RenderPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const render = useRenderGenerate();
  const setView = useProjectStore((s) => s.setView);

  const proj: Project | undefined = project.data;
  const [resolution, setResolution] = useState('1920x1080');
  const [fps, setFps] = useState(30);
  const [error, setError] = useState<string | null>(null);

  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;
  const segments = useMemo(() => (timeline ? planSegments(timeline) : []), [timeline]);
  const outputVersion = proj?.outputVersion ?? proj?.selectedOutputVersion ?? null;
  const sampleApproved = proj?.approvals.some((a) => a.kind === 'sample' && a.decision === 'accepted') ?? false;

  const renderJob: Job | undefined = (jobs.data ?? []).find(
    (j) => j.kind === 'renders' && ['queued', 'running', 'recovering'].includes(j.status),
  );
  const rendering = !!renderJob;

  const canRender = !!proj && !!proj.currentDraftRevision && !!timeline && !rendering;

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

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
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
        }
      />
      }
    >
      <StageHead title="⑤ 生成导出">
        {timeline && <Badge tone="info">时间轨 {timeline.revisionId} · {timeline.units.length} 单元</Badge>}
        {outputVersion && <Badge tone="success">{outputVersion}</Badge>}
        <span className="hf-spacer" />
        <Button
          variant="brand" size="sm" icon="film" busy={rendering} disabled={!canRender}
          onClick={handleRender}
        >{rendering ? '渲染中' : outputVersion ? '重新渲染' : '开始渲染'}</Button>
      </StageHead>

      <div className="hf-body" style={{ display: 'flex', gap: 20, minHeight: 0 }}>
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
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
          {rendering && (
            <Alert tone="info">
              正在渲染 · {renderJob?.stage} 阶段
              <div style={{ marginTop: 8 }}>
                <Progress value={renderJob?.stage === 'post' ? 90 : renderJob?.stage === 'infer' ? 55 : 25} />
              </div>
            </Alert>
          )}
          {lastMeta && (
            <Alert tone="success">
              渲染完成：{lastMeta.outputVersion} · {lastMeta.resolution} @ {lastMeta.fps}fps，已登记为不可变视频资产。
            </Alert>
          )}

          <div className="hf-zone-t">分段计划<span className="wu-caption">三段式区间：outputRange 保留 · renderRange 实际生成 · contextRange 衔接上下文</span></div>
          {segments.length === 0 ? (
            <Alert tone="muted">合成时间轨后在此展示分段计划。</Alert>
          ) : (
            <div style={{ display: 'grid', gap: 8 }}>
              {segments.map((sg) => (
                <div className="hf-out-card" key={sg.id}>
                  <div className="wu-row" style={{ gap: 8 }}>
                    <span className="hf-mono" style={{ fontWeight: 700 }}>{sg.id}</span>
                    <Badge tone="info">{sg.units.length} 单元</Badge>
                    <span className="hf-spacer" />
                    <span className="wu-caption">时长 {fmtT(sg.renderRange[1] - sg.renderRange[0])}</span>
                  </div>
                  {sg.units.map((u) => (
                    <div className="hf-segment" key={u.id}>
                      <span className="id">{u.id}</span>
                      <span className="hf-mono" style={{ minWidth: 60 }}>{u.turnId}</span>
                      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {u.candidateAudioAssetIds[0] ?? '—'}
                      </span>
                      <span className="hf-mono">{fmtT((timeline?.unitOffsets[u.id] ?? 0) / 48000)}</span>
                    </div>
                  ))}
                  <div className="hf-kv" style={{ marginTop: 4 }}>
                    <b>outputRange</b>
                    <span className="hf-mono">{fmtT(sg.outputRange[0])} – {fmtT(sg.outputRange[1])}（保留）</span>
                  </div>
                  <div className="hf-kv"><b>renderRange</b><span className="hf-mono">{fmtT(sg.renderRange[0])} – {fmtT(sg.renderRange[1])}（实际生成）</span></div>
                  <div className="hf-kv"><b>contextRange</b><span className="hf-mono">{fmtT(sg.contextRange[0])} – {fmtT(sg.contextRange[1])}（衔接上下文）</span></div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ---- 检查器 ---- */}
        <aside className="hf-inspector" aria-label="检查器" style={{ position: 'sticky', top: 0, height: 'fit-content' }}>
          <div className="hf-ins-sec">
            <h4>渲染输出</h4>
            <div className="hf-fld">
              <label>分辨率</label>
              <div className="hf-seg" role="group" aria-label="分辨率" style={{ width: '100%' }}>
                {RESOLUTIONS.map((r) => (
                  <button
                    key={r} type="button" aria-pressed={resolution === r}
                    style={{ width: '50%', padding: '0 4px' }}
                    onClick={() => setResolution(r)}
                  >{r}</button>
                ))}
              </div>
            </div>
            <div className="hf-fld">
              <label>帧率</label>
              <div className="hf-seg" role="group" aria-label="帧率" style={{ width: '100%' }}>
                {FPSS.map((f) => (
                  <button
                    key={f} type="button" aria-pressed={fps === f}
                    style={{ width: '50%', padding: '0 4px' }}
                    onClick={() => setFps(f)}
                  >{f} fps</button>
                ))}
              </div>
            </div>
            <div style={{ marginTop: 10 }}>
              <Button variant="brand" size="sm" icon="film" busy={rendering} disabled={!canRender} onClick={handleRender}>
                {rendering ? '渲染中' : '开始渲染'}
              </Button>
            </div>
            <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
              渲染任务走 GPU 队列；完成后登记 OUT-{timeline?.revisionId ?? 'Rn'}-vN 版本，产物为不可变视频资产。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>输出记录</h4>
            {(jobs.data ?? []).filter((j) => j.kind === 'renders').slice(0, 4).map((j) => (
              <div className="hf-kv" key={j.id} style={{ marginBottom: 4 }}>
                <b className="hf-mono" style={{ minWidth: 60 }}>{j.id}</b>
                <span>{j.status}{j.result?.meta ? ` · ${String((j.result.meta as { outputVersion?: string }).outputVersion ?? '')}` : ''}</span>
              </div>
            ))}
            {(jobs.data ?? []).filter((j) => j.kind === 'renders').length === 0 && (
              <p className="wu-caption">暂无渲染记录。</p>
            )}
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
      </div>
    </AppShell>
  );
}
