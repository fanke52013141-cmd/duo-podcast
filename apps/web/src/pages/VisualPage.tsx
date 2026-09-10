/* ============================================================================
   HF-04 · 阶段④ 预览画面（真实数据流，01 §6 权威）
   主视图：变体网格（主图 + 构图参考）+ 生成任务；检查器：变体属性 / 确认点
   确认：样片确认（sample）→ approvals 只追加；构图/镜头/角色为可选确认点
   ========================================================================== */
import { useEffect, useState } from 'react';
import {
  useConfirm, useJobs, useProject, useVisualGenerate,
} from '../lib/api';
import type { Job, VisualVariant } from '../lib/types';
import { Alert, Badge, Button, Icon, Progress } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const ASPECTS: { id: 'landscape' | 'portrait'; label: string; ratio: string }[] = [
  { id: 'landscape', label: '横屏 16:9', ratio: '16/9' },
  { id: 'portrait', label: '竖屏 9:16', ratio: '9/16' },
];

export function VisualPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const gen = useVisualGenerate();
  const confirm = useConfirm();
  const setView = useProjectStore((s) => s.setView);

  const proj = project.data;
  const [aspect, setAspect] = useState<'landscape' | 'portrait'>('landscape');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 画幅跟随工程默认（工程为竖屏时默认竖屏生成）
  useEffect(() => {
    if (proj?.aspect) setAspect(proj.aspect);
  }, [proj?.aspect]);

  const variants: VisualVariant[] = proj?.visualVariants ?? [];
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

  const handleConfirm = () => {
    if (!proj) return;
    confirm.mutate({ projectId, expectedRevision: proj.revision, kind: 'sample', inputRevisionId: proj.currentDraftRevision });
  };

  const regionCount = selected ? Object.keys(selected.personRegions ?? {}).length : 0;
  const canConfirm = !!proj && !!selected && !!proj.currentDraftRevision && !sampleApproved;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('voice')}>上一步</Button>
            <Badge tone={sampleApproved ? 'success' : 'warning'} icon={sampleApproved ? 'check' : 'alert'}>
              确认点 · 样片确认{sampleApproved ? '（已确认）' : ''}
            </Badge>
            <span className="wu-caption">构图 / 镜头 / 角色为可选确认点</span>
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
        {proj?.currentDraftRevision && <Badge tone="info">脚本 {proj.currentDraftRevision}</Badge>}
        {selected && <Badge tone="muted">{selected.id}</Badge>}
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
        <Button
          variant="brand" size="sm" icon="image" busy={generating} disabled={!canGenerate}
          onClick={handleGenerate}
        >{generating ? '生成中' : '生成主图'}</Button>
      </StageHead>

      <div className="hf-body" style={{ display: 'flex', gap: 20, minHeight: 0 }}>
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
          {error && <Alert tone="danger">{error}</Alert>}
          {!voiceApproved && (
            <Alert tone="warning">
              阶段③ 配音尚未确认。画面生成不依赖配音，但样片确认建议在配音锁定后进行。
              <a className="hf-link" onClick={() => setView('voice')}>前往阶段③ 试听配音</a>
            </Alert>
          )}
          {generating && (
            <Alert tone="info">
              正在生成主图 · {genJob?.stage} 阶段
              <div style={{ marginTop: 8 }}>
                <Progress value={genJob?.stage === 'post' ? 90 : genJob?.stage === 'infer' ? 55 : 25} />
              </div>
            </Alert>
          )}

          <div className="hf-zone-t">构图变体<span className="wu-caption">每期生成 1+ 个主图；可继续生成追加变体（V 编号递增）</span></div>
          {variants.length === 0 && !generating && (
            <Alert tone="muted">
              还没有主图。点击上方「生成主图」提交图片 API 任务；完成后按
              <span className="hf-mono"> modelInputTransform</span> 记录构图变换。
            </Alert>
          )}
          <div className="hf-variant-grid">
            {variants.map((v) => (
              <div
                key={v.id}
                className="hf-variant"
                data-aspect={v.aspect}
                role="button"
                tabIndex={0}
                aria-pressed={selected?.id === v.id}
                style={selected?.id === v.id ? { outline: '1px solid var(--wu-semantic-brand)', outlineOffset: 1 } : undefined}
                onClick={() => setSelectedId(v.id)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedId(v.id); } }}
              >
                <div className="frame"><Icon name="image" size={16} />主图 {v.id}</div>
                <div className="meta">
                  <Badge tone={v.aspect === 'portrait' ? 'speaker-b' : 'info'}>{v.aspect === 'portrait' ? '竖屏' : '横屏'}</Badge>
                  <span className="hf-spacer" />
                  <span className="wu-caption">scale {v.modelInputTransform.scale} · {v.modelInputTransform.pad}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ---- 检查器 ---- */}
        <aside className="hf-inspector" aria-label="检查器" style={{ position: 'sticky', top: 0, height: 'fit-content' }}>
          <div className="hf-ins-sec">
            <h4>所选变体 <span className="wu-caption" style={{ fontWeight: 400 }}>{selected ? selected.id : '未选'}</span></h4>
            {selected ? (
              <div className="hf-detail-grid">
                <div className="hf-kv"><b>画幅</b><span>{selected.aspect === 'portrait' ? '竖屏 9:16' : '横屏 16:9'}</span></div>
                <div className="hf-kv"><b>缩放</b><span className="hf-mono">scale {selected.modelInputTransform.scale}</span></div>
                <div className="hf-kv"><b>偏移</b><span className="hf-mono">({selected.modelInputTransform.offsetX}, {selected.modelInputTransform.offsetY})</span></div>
                <div className="hf-kv"><b>填充</b><span>{selected.modelInputTransform.pad}</span></div>
                <div className="hf-kv"><b>人物区域</b><span>{regionCount} 处</span></div>
                <div className="hf-kv"><b>提供方</b><span>{selected.imageApiConfigRef ?? '—'}</span></div>
                <div className="hf-kv"><b>主图资产</b><span className="hf-mono" style={{ wordBreak: 'break-all' }}>{selected.masterImage.artifactId}</span></div>
              </div>
            ) : (
              <p className="wu-caption">点击变体卡片查看构图变换与人物区域；生成后自动选中最新变体。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>确认点</h4>
            <ul className="hf-ins-list">
              <li><Badge tone={voiceApproved ? 'success' : 'muted'}>配音</Badge> {voiceApproved ? '已确认' : '未确认'}</li>
              <li><Badge tone={sampleApproved ? 'success' : 'warning'}>样片</Badge> {sampleApproved ? '已确认' : '待确认'}</li>
              <li><Badge tone="muted">构图 / 镜头 / 角色</Badge> 可选确认点，可跳过</li>
            </ul>
            <div style={{ marginTop: 10 }}>
              <Button variant="brand" size="sm" icon="check" disabled={!canConfirm} onClick={handleConfirm}>
                {sampleApproved ? '样片已确认' : '确认样片'}
              </Button>
            </div>
            <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
              样片确认以当前草稿脚本为输入；若脚本变更，样片标记为过期（stale）并需重新确认。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>生成记录</h4>
            {(jobs.data ?? []).filter((j) => j.kind === 'visual.generate').slice(0, 3).map((j) => (
              <div className="hf-kv" key={j.id} style={{ marginBottom: 4 }}>
                <b className="hf-mono" style={{ minWidth: 60 }}>{j.id}</b>
                <span>{j.status}{j.result?.meta ? ` · ${String((j.result.meta as { variantId?: string }).variantId ?? '')}` : ''}</span>
              </div>
            ))}
            {(jobs.data ?? []).filter((j) => j.kind === 'visual.generate').length === 0 && (
              <p className="wu-caption">暂无生成记录。</p>
            )}
          </div>
        </aside>
      </div>
    </AppShell>
  );
}
