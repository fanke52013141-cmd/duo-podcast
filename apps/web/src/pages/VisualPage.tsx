/* ============================================================================
   HF-04 · 阶段④ 预览画面（真实数据流，01 §6 权威）
   三分区锚点同屏：构图（同框底图 + A/B 人物区域）/ 镜头（后处理预设）/ 样片（区间 + 检查项）
   检查器：底图来源 / 人物区域坐标 / 坐标信息（对齐 hf/HF-04-预览画面.html）
   确认：接受样片（sample）→ approvals 只追加
   ========================================================================== */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  useConfirm, useJobs, useProject, useVisualGenerate,
} from '../lib/api';
import type { Job, ScriptRevision, VisualVariant } from '../lib/types';
import { Alert, Badge, Button, Choice, Icon } from '../components/wu';
import { AppShell, FootBar, StageHead, TimelineBar } from '../components/AppShell';
import { useProjectStore } from '../stores';

const ASPECTS: { id: 'landscape' | 'portrait'; label: string }[] = [
  { id: 'landscape', label: '横屏 16:9' },
  { id: 'portrait', label: '竖屏 9:16' },
];

const SHOT_PRESETS = ['稳定同框（默认）', '轻微推拉', '发言人裁切'];

const CHECKS = ['错误开口', '口型同步', '角色一致', '边界连续', '声音与清晰度'];

type Region = { x: number; y: number; width: number; height: number };
const asRegion = (v: unknown): Region | null => {
  if (!v || typeof v !== 'object') return null;
  const r = v as Partial<Region>;
  if (typeof r.x !== 'number' || typeof r.y !== 'number' || typeof r.width !== 'number' || typeof r.height !== 'number') return null;
  return { x: r.x, y: r.y, width: r.width, height: r.height };
};

function fmtSecs(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function VisualPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const gen = useVisualGenerate();
  const confirm = useConfirm();
  const setView = useProjectStore((s) => s.setView);

  const proj = project.data;
  const [aspect, setAspect] = useState<'landscape' | 'portrait'>('landscape');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<'compose' | 'shot' | 'sample'>('compose');
  const [preset, setPreset] = useState(SHOT_PRESETS[0]);
  const [shotOpts, setShotOpts] = useState<Record<string, boolean>>({ 轻微推拉: false, 发言人裁切: false });
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const zoneRefs = useRef<Record<string, HTMLElement | null>>({ compose: null, shot: null, sample: null });

  useEffect(() => {
    if (proj?.aspect) setAspect(proj.aspect);
  }, [proj?.aspect]);

  const revision: ScriptRevision | undefined =
    proj?.scriptRevisions.find((r) => r.id === proj.currentDraftRevision)
    ?? proj?.scriptRevisions[proj.scriptRevisions.length - 1];

  const variants: VisualVariant[] = proj?.visualVariants ?? [];
  const timeline = proj?.audioTimeline ?? null;
  const selected = variants.find((v) => v.id === selectedId) ?? variants[variants.length - 1] ?? null;
  const genJob: Job | undefined = (jobs.data ?? []).find(
    (j) => j.kind === 'visual.generate' && ['queued', 'running', 'recovering'].includes(j.status),
  );
  const generating = !!genJob;
  const voiceApproved = proj?.approvals.some((a) => a.kind === 'voice' && a.decision === 'accepted') ?? false;
  const sampleApproved = proj?.approvals.some(
    (a) => a.kind === 'sample' && a.decision === 'accepted' && proj.currentDraftRevision && a.inputRevisionId === proj.currentDraftRevision,
  ) ?? false;

  const canGenerate = !!proj && !!proj.currentDraftRevision && !generating;
  const canAccept = !!selected && Object.values(checks).filter(Boolean).length === CHECKS.length;

  const handleGenerate = () => {
    if (!proj) return;
    setError(null);
    gen.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      aspect,
      prompt: `主图：${proj.title}（${aspect === 'portrait' ? '竖屏' : '横屏'}）`,
    }, { onError: (e) => setError((e as Error).message) });
  };

  const handleAccept = () => {
    if (!proj) return;
    // 样片确认绑当前草稿；服务端校验绑定关系，过期确认会 422（11 报告 P1-6）
    confirm.mutate({ projectId, expectedRevision: proj.revision, kind: 'sample', inputRevisionId: proj.currentDraftRevision }, {
      onError: (e) => setError((e as Error).message),
    });
  };

  // 人物区域（归一化坐标 → 画布百分比）
  const regions = useMemo(() => {
    const out: { key: 'A' | 'B'; r: Region }[] = [];
    (['A', 'B'] as const).forEach((k) => {
      const r = asRegion(selected?.personRegions?.[k]);
      if (r) out.push({ key: k, r });
    });
    return out;
  }, [selected]);

  // 样片区间：自动选第一个含 A→B→A 的连续区间（01 §6.4）
  const sampleRange = useMemo(() => {
    const tl = proj?.audioTimeline;
    const turns = revision?.turns ?? [];
    if (!tl || !turns.length) return null;
    for (let i = 0; i + 2 < turns.length; i += 1) {
      if (turns[i].speaker === 'A' && turns[i + 1].speaker === 'B' && turns[i + 2].speaker === 'A') {
        const startUnit = tl.units.find((u) => u.turnId === turns[i].id);
        const endUnit = [...tl.units].reverse().find((u) => u.turnId === turns[i + 2].id);
        if (startUnit && endUnit) {
          const s0 = (tl.unitOffsets[startUnit.id] ?? 0) / tl.sampleRate;
          const s1 = ((tl.unitOffsets[endUnit.id] ?? 0) + endUnit.sampleCount) / tl.sampleRate;
          return { start: s0, end: s1, label: `${fmtSecs(s0)} – ${fmtSecs(s1)} · A→B→A` };
        }
      }
    }
    return null;
  }, [proj?.audioTimeline, revision]);

  const goZone = (k: 'compose' | 'shot' | 'sample') => {
    setTab(k);
    zoneRefs.current[k]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      timeline={
        <TimelineBar
          timeline={timeline}
          labels={[sampleRange ? `样片区间 ▎${sampleRange.label}` : '样片区间 ▎未选取', 'A 轨 / B 轨']}
          speakerOf={(turnId) => revision?.turns.find((t) => t.id === turnId)?.speaker ?? 'A'}
        />
      }
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>底图来源</h4>
            <div style={{ display: 'flex', gap: 8 }}>
              <Button variant="secondary" size="sm" busy={generating} disabled={!canGenerate} onClick={handleGenerate}>生成</Button>
              <Button variant="secondary" size="sm" icon="upload" disabled>上传</Button>
            </div>
            <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
              生成时明确引用 A/B 身份图、场景、目标比例；候选图需由人检查身份和遮挡。不支持多参考图编辑时提供上传替代，<b>不默默丢掉人物参考图</b>。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>人物区域</h4>
            {regions.length ? regions.map(({ key, r }) => (
              <div
                key={key}
                className={`hf-region ${key === 'B' ? 'b' : ''}`}
                style={{ position: 'relative', height: 44, marginBottom: 8 }}
              >
                <span className="tag">{key} 区</span>
                <span
                  className="hf-mono"
                  style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--wu-semantic-secondary)' }}
                >{r.x.toFixed(2)}, {r.y.toFixed(2)}, {r.width.toFixed(2)}, {r.height.toFixed(2)}</span>
              </div>
            )) : <p className="wu-caption">未生成主图，暂无人物区域标注。</p>}
            <p className="wu-caption" style={{ lineHeight: 1.6 }}>
              在<b>最终送入视频工作流的图</b>上标注；矩形框为首版，精细笔刷后置。修改构图后需重新生成人物区域与样片，原图和旧样片保留作比较。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>坐标信息</h4>
            <div className="hf-coords"><span>原图</span><b>{aspect === 'portrait' ? '1080 × 1920' : '1920 × 1080'}</b></div>
            <div className="hf-coords"><span>规范化</span><b>0–1 · 人物区域</b></div>
            <div className="hf-coords">
              <span>模型输入</span>
              <b>{selected ? `scale ${selected.modelInputTransform.scale} · pad ${selected.modelInputTransform.pad}` : '—'}</b>
            </div>
            <div className="hf-coords">
              <span>变换</span>
              <b>{selected ? `偏移 (${selected.modelInputTransform.offsetX}, ${selected.modelInputTransform.offsetY})` : '线性仿射 · 底图与 mask 同一几何变换'}</b>
            </div>
          </div>

          <div className="hf-ins-sec">
            <h4>确认点</h4>
            <ul className="hf-ins-list">
              <li><Badge tone={voiceApproved ? 'success' : 'muted'}>配音</Badge> {voiceApproved ? '已确认' : '未确认'}</li>
              <li><Badge tone={sampleApproved ? 'success' : 'warning'}>样片</Badge> {sampleApproved ? '已确认' : '待确认'}</li>
              <li><Badge tone="muted">构图 / 镜头 / 角色</Badge> 可选确认点，可跳过</li>
            </ul>
          </div>
        </aside>
      }
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('voice')}>上一步</Button>
            <Badge tone={sampleApproved ? 'success' : 'warning'} icon={sampleApproved ? 'check' : 'alert'}>
              确认点 · 样片确认{sampleApproved ? '（已确认）' : ''}
            </Badge>
            <span className="wu-caption">接受样片绑定本次输入版本、工作流与规格</span>
            <span className="hf-spacer" />
            <Button size="sm" disabled={!sampleApproved} onClick={() => setView('render')}>
              下一步<Icon name="arrowRight" size={14} />
            </Button>
          </span>
        }
        />
      }
    >
      <StageHead title="④ 预览画面">
        {selected && <Badge tone={sampleApproved ? 'success' : 'muted'}>视觉 {selected.id} · {sampleApproved ? '已用' : '待接受'}</Badge>}
        <span className="hf-spacer" />
        <span className="hf-seg" role="group" aria-label="画幅">
          {ASPECTS.map((a) => (
            <button
              key={a.id} type="button" aria-pressed={aspect === a.id}
              style={{ width: 'auto', padding: '0 10px' }}
              onClick={() => setAspect(a.id)}
            >{a.label}</button>
          ))}
        </span>
        <Button variant="secondary" size="sm" disabled={!canAccept} onClick={handleAccept}>接受样片</Button>
        <span className="wu-caption">{canAccept ? '样片检查通过，可接受' : '需先生成样片并通过全部检查'}</span>
      </StageHead>

      <div className="hf-body" ref={bodyRef}>
        {error && <Alert tone="danger">{error}</Alert>}
        {!voiceApproved && (
          <Alert tone="warning">
            阶段③ 配音尚未确认。画面生成不依赖配音，但样片确认建议在配音锁定后进行。
            <a className="hf-link" onClick={() => setView('voice')}>前往阶段③ 试听配音</a>
          </Alert>
        )}

        <div className="hf-s4">
          {/* 页内 Tab：分区锚点（01 §6.1） */}
          <div className="wu-tabs-list" role="tablist" aria-label="预览画面分区">
            {([['compose', '构图'], ['shot', '镜头'], ['sample', '样片']] as const).map(([k, label]) => (
              <button
                key={k} type="button" role="tab" className="wu-tab"
                aria-selected={tab === k} tabIndex={tab === k ? 0 : -1}
                onClick={() => goZone(k)}
              >{label}</button>
            ))}
          </div>

          {/* 构图区（01 §6.2） */}
          <section className="hf-zone" ref={(el) => { zoneRefs.current.compose = el; }}>
            <div className="hf-zone-t">构图 <span className="wu-caption">同框底图 · 人物区域标注 · 竖屏提示</span></div>
            <div className="hf-canvas" data-aspect={aspect}>
              {!selected && (
                <span className="scene-hint">
                  {generating ? '正在生成同框底图…' : '同框底图 · 点击下方「生成同框底图」提交图片 API 任务'}
                </span>
              )}
              {selected && regions.map(({ key, r }) => (
                <div
                  key={key} className={`hf-region ${key === 'B' ? 'b' : ''}`}
                  style={{ left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.width * 100}%`, height: `${r.height * 100}%` }}
                >
                  <span className="tag">{key} 区</span>
                </div>
              ))}
              <div className="cta">
                <Button
                  variant="secondary" size="sm" icon="sparkle" busy={generating} disabled={!canGenerate}
                  onClick={handleGenerate}
                >生成同框底图</Button>
              </div>
            </div>
            <div className="hf-assetrow">
              <span className="item">主图变体
                <select
                  className="hf-mini-select" aria-label="主图变体"
                  value={selected?.id ?? ''}
                  onChange={(e) => setSelectedId(e.target.value)}
                  disabled={!variants.length}
                >
                  {variants.length
                    ? variants.map((v) => <option key={v.id} value={v.id}>{v.id} · {v.aspect === 'portrait' ? '竖屏' : '横屏'}</option>)
                    : <option value="">尚未生成</option>}
                </select>
              </span>
              <span className="item">场景
                <select className="hf-mini-select" aria-label="场景" defaultValue="录音棚 · 资产库">
                  <option>录音棚 · 资产库</option>
                  <option>生成新场景</option>
                  <option>上传</option>
                </select>
              </span>
              <span className="item">角色 A
                <select className="hf-mini-select" aria-label="角色 A"><option>李雷 · 资产库</option></select>
              </span>
              <span className="item">角色 B
                <select className="hf-mini-select" aria-label="角色 B"><option>韩梅梅 · 资产库</option></select>
              </span>
              <span className="hf-spacer" />
              <span className="wu-caption">不要求每期重新生成；失败时保存提示词与引用关系可重试</span>
            </div>
          </section>

          {/* 镜头区（01 §6.3） */}
          <section className="hf-zone" ref={(el) => { zoneRefs.current.shot = el; }}>
            <div className="hf-zone-t">镜头 <span className="wu-caption">后处理 · 改镜头不触发基础视频重渲染</span></div>
            <div className="hf-sample-head">
              <span className="lb">镜头预设</span>
              <select
                className="wu-input" aria-label="镜头预设" style={{ width: 'auto', minHeight: 32 }}
                value={preset} onChange={(e) => setPreset(e.target.value)}
              >
                {SHOT_PRESETS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              {['轻微推拉', '发言人裁切'].map((o) => (
                <Choice
                  key={o} type="checkbox" checked={shotOpts[o]}
                  onChange={(v) => setShotOpts((s) => ({ ...s, [o]: v }))} label={o}
                />
              ))}
              <span className="hf-spacer" />
              <Button variant="secondary" size="sm" icon="play" disabled={!selected}>轻量预览</Button>
              <span className="wu-caption">构图预览，无动态口型</span>
            </div>
          </section>

          {/* 样片区（01 §6.4） */}
          <section className="hf-zone" ref={(el) => { zoneRefs.current.sample = el; }}>
            <div className="hf-zone-t">样片 <span className="wu-caption">自动选双方都有发言、含 A→B→A 的区间</span></div>
            <div className="hf-sample-head">
              <span className="lb">区间</span>
              <span
                className="hf-mono"
                style={{ color: 'var(--wu-semantic-text)', background: 'var(--wu-semantic-muted)', padding: '3px 10px', borderRadius: 6 }}
              >{sampleRange ? sampleRange.label : '待配音确认后自动选取'}</span>
              <Button variant="ghost" size="sm" disabled>调整</Button>
              <Button variant="brand" size="sm" icon="sparkle" disabled={!sampleRange}>生成样片</Button>
              <span className="hf-spacer" />
              <Button variant="ghost" size="sm" disabled={!selected}>边界预览</Button>
            </div>
            <div className="hf-check">
              {CHECKS.map((c) => (
                <Choice
                  key={c} type="checkbox" checked={!!checks[c]}
                  onChange={(v) => setChecks((s) => ({ ...s, [c]: v }))} label={c}
                />
              ))}
            </div>
            <p className="wu-caption" style={{ marginTop: 8 }}>
              检查<b>不能只检查是否生成成功</b>；接受样片只证明已检查区间和配置，不保证整片无缺陷。
            </p>
          </section>
        </div>
      </div>
    </AppShell>
  );
}
