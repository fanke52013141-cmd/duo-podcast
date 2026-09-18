/* ============================================================================
   HF-01 · 阶段① 创建本期（真实数据流）
   左 内容输入 55% / 右 内容要求 45%；无 Inspector / 无 TimelineBar（01 §3）
   ========================================================================== */
import { useEffect, useMemo, useState } from 'react';
import {
  useCapabilities, useGenerateScript, useJobs, useJobCancel, usePatchProject, useProject,
} from '../lib/api';
import { STAGE_META, type SourceKind } from '../lib/types';
import { Alert, Badge, Button, Choice, Field, Icon, Input, SegControl, Tabs, Textarea } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

type DurationMode = 'auto' | 'approx';

const SOURCE_TABS: { id: SourceKind; label: string }[] = [
  { id: 'topic', label: '从话题开始' },
  { id: 'article', label: '粘贴文章' },
  { id: 'script', label: '已有脚本' },
];

const SOURCE_HINTS: Record<SourceKind, string> = {
  topic: '一句话或一段话即可，建议 ≤200 字',
  article: '粘贴文章正文，≤8,000 字',
  script: '粘贴已有双人对话脚本，只做结构解析与校验',
};

export function CreateEpisodePage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const caps = useCapabilities();
  const patchProject = usePatchProject();
  const generateScript = useGenerateScript();
  const cancelJob = useJobCancel();
  const setView = useProjectStore((s) => s.setView);

  const [sourceKind, setSourceKind] = useState<SourceKind>('article');
  const [content, setContent] = useState('');
  const [durationMode, setDurationMode] = useState<DurationMode>('auto');
  const [approxMinutes, setApproxMinutes] = useState(3);
  const [aspect, setAspect] = useState<'landscape' | 'portrait'>('landscape');
  const [title, setTitle] = useState('');
  const [error, setError] = useState<string | null>(null);

  const proj = project.data;
  useEffect(() => {
    if (proj) setAspect(proj.aspect);
  }, [proj?.aspect]); // eslint-disable-line react-hooks/exhaustive-deps

  const textReady = caps.data?.textApi.ready ?? false;
  const canGenerate = content.trim().length > 0 && textReady && !generateScript.isPending;

  // 当前工程的脚本生成任务（幂等 clientToken 定位）
  const genJob = useMemo(() => {
    const js = jobs.data ?? [];
    const j = js.find((x) => x.kind === 'script.generate');
    return j && (j.status === 'queued' || j.status === 'running' || j.status === 'succeeded' || j.status === 'failed') ? j : undefined;
  }, [jobs.data]);
  const generating = !!genJob && (genJob.status === 'queued' || genJob.status === 'running');
  const genDone = !!genJob && genJob.status === 'succeeded';
  const genFailed = !!genJob && genJob.status === 'failed';

  const handleGenerate = async () => {
    setError(null);
    try {
      await generateScript.mutateAsync({
        projectId,
        clientToken: crypto.randomUUID(),
        sourceInput: { kind: sourceKind, content: content.trim() },
        brief: {
          durationMode,
          approxMinutes: durationMode === 'approx' ? approxMinutes : undefined,
          aspect,
        },
      });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleAspect = (v: 'landscape' | 'portrait') => {
    setAspect(v);
    if (proj && v !== proj.aspect) {
      patchProject.mutate({ projectId, patch: { aspect: v }, expectedRevision: proj.revision });
    }
  };

  const handleRename = () => {
    const t = title.trim();
    if (proj && t && t !== proj.title) {
      patchProject.mutate({ projectId, patch: { title: t }, expectedRevision: proj.revision });
    }
  };

  const stageState = proj?.stageProgress.create;
  const draftRev = proj?.currentDraftRevision;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      footer={<FootBar hint={proj ? `${proj.id} · ${proj.title} · 阶段① 无确认点，脚本在阶段②收口` : undefined} />}
    >
      <StageHead title="创建本期">
        {stageState && stageState !== 'ready' && <Badge tone={STAGE_META[stageState].tone}>{STAGE_META[stageState].label}</Badge>}
        <span className="hf-spacer" />
        {proj && <span className="hf-mono wu-caption">revision {proj.revision}</span>}
      </StageHead>

      <div className="hf-cols">
        {/* ---- 左：内容输入（55%） ---- */}
        <section className="wu-card hf-panel in">
          <h3 className="hf-panel-t">内容输入</h3>
          <Tabs
            items={SOURCE_TABS.map((t) => ({ id: t.id, label: t.label }))}
            active={sourceKind}
            onSelect={(id) => setSourceKind(id as SourceKind)}
            idPrefix="src"
          />
          <div style={{ marginTop: 14, display: 'grid', gap: 10 }}>
            <Field labelFor="src-content" label={SOURCE_TABS.find((t) => t.id === sourceKind)?.label ?? '内容'}>
              <Textarea
                id="src-content"
                rows={12}
                placeholder={SOURCE_HINTS[sourceKind]}
                value={content}
                disabled={generating}
                onChange={(e) => setContent(e.target.value)}
              />
            </Field>
            {sourceKind === 'article' && content.trim().length > 8000 && (
              <Alert tone="warning">文章超过 8,000 字，生成质量可能下降；建议拆分为多期，或截取核心段落。</Alert>
            )}
            {sourceKind === 'topic' && content.trim().length > 200 && (
              <Alert tone="warning">话题建议 ≤200 字，过长内容建议改为「粘贴文章」入口。</Alert>
            )}

            {/* 生成区：就绪 → 执行中两步进度 → 结果 */}
            {generating ? (
              <div className="wu-alert" data-tone="info" style={{ display: 'grid', gap: 10 }}>
                <div className="wu-row">
                  <Badge tone="success" icon="check">内容结构</Badge>
                  <Icon name="arrowRight" size={14} />
                  <Badge tone="info">结构化话轮</Badge>
                  <span className="hf-spacer" />
                  <Button variant="ghost" size="sm" busy={cancelJob.isPending}
                    onClick={() => genJob && cancelJob.mutate(genJob.id)}>取消</Button>
                </div>
                <div className="hf-prog"><i style={{ width: '46%', animation: 'wu-prog 1.2s ease-in-out infinite alternate' }} /></div>
                <span className="wu-caption">两次生成：先内容结构，再直接生成双人对话；完成后进入阶段②为候选脚本。</span>
              </div>
            ) : genDone ? (
              <div className="wu-alert" data-tone="success" style={{ display: 'grid', gap: 8 }}>
                <div className="wu-row">
                  <Badge tone="success" icon="check">候选脚本已生成</Badge>
                  {draftRev && <span className="hf-mono">{draftRev}</span>}
                </div>
                <div className="wu-row">
                  <span className="wu-caption">结果进入阶段②为候选脚本，不自动覆盖已用版本。</span>
                  <span className="hf-spacer" />
                  <Button variant="secondary" size="sm" onClick={() => setView('script')}>
                    前往编辑对话<Icon name="arrowRight" size={14} />
                  </Button>
                </div>
              </div>
            ) : genFailed ? (
              <div className="wu-alert" data-tone="danger" style={{ display: 'grid', gap: 8 }}>
                <div className="wu-row">
                  <Badge tone="danger" icon="alert">生成失败</Badge>
                  <span className="wu-caption">{genJob?.error || '未知错误'}</span>
                </div>
                <div className="wu-row">
                  <span className="wu-caption">任务已记入任务中心，可直接重试；详情可到任务中心查看。</span>
                  <span className="hf-spacer" />
                  <Button variant="secondary" size="sm" onClick={() => setView('tasks')}>查看任务中心</Button>
                  <Button variant="brand" size="sm" disabled={!canGenerate} onClick={handleGenerate}>重试生成</Button>
                </div>
              </div>
            ) : (
              <>
                {!textReady && (
                  <Alert tone="warning">
                    文本 API 不可用，无法生成对话。
                    <a className="hf-link" onClick={() => setView('services')}>去服务设置</a>
                  </Alert>
                )}
                {error && <Alert tone="danger">生成失败：{error}</Alert>}
                <Button variant="brand" icon="sparkle" disabled={!canGenerate} onClick={handleGenerate}>
                  生成对话
                </Button>
              </>
            )}
          </div>
        </section>

        {/* ---- 右：内容要求（45%） ---- */}
        <section className="wu-card hf-panel req">
          <h3 className="hf-panel-t">
            内容要求
            <span className="wu-caption">模板「科技周报」提供默认值</span>
          </h3>

          <div style={{ display: 'grid', gap: 6 }}>
          <Field label="节目名">
            <Input
              placeholder="未命名节目"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={handleRename}
              onKeyDown={(e) => e.key === 'Enter' && handleRename()}
            />
          </Field>

          <Field label="时长">
            <div className="wu-stack" style={{ gap: 8 }}>
              <Choice
                checked={durationMode === 'auto'}
                onChange={() => setDurationMode('auto')}
                label={<span>根据内容，最多 5 分钟 <span className="wu-caption">（上限 300 秒，含片头片尾与首尾停顿）</span></span>}
              />
              <div className="wu-row" style={{ gap: 8 }}>
                <Choice checked={durationMode === 'approx'} onChange={() => setDurationMode('approx')} label="大约" />
                <SegControl
                  options={[1, 2, 3, 4].map((n) => ({ value: String(n), label: `${n} 分钟` }))}
                  value={String(approxMinutes)}
                  onChange={(v) => setApproxMinutes(Number(v))}
                />
                <span className="wu-caption">偏好不是承诺</span>
              </div>
            </div>
          </Field>

          <Field label="比例">
            <div className="wu-row">
              <Choice checked={aspect === 'landscape'} onChange={() => handleAspect('landscape')} label="横屏 16:9" />
              <Choice checked={aspect === 'portrait'} onChange={() => handleAspect('portrait')} label="竖屏" />
            </div>
          </Field>
          {aspect === 'portrait' && (
            <Alert tone="warning">竖屏建议采用上下分栏双人构图；构图需独立检查，不承诺横屏素材无损裁成竖屏。</Alert>
          )}

          <Field label="角色">
            <div className="hf-upload" onClick={() => setView('assets')}>
              <div className="t"><Icon name="image" size={16} /> 角色图</div>
              <div className="s">资产库选择或 API 生成</div>
            </div>
            <span className="wu-caption">形象与声音未就绪不阻塞生成对话。</span>
          </Field>
          </div>
        </section>
      </div>
    </AppShell>
  );
}
