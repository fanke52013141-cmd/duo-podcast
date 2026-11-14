/* ============================================================================
   HF-01 · 阶段① 创建本期（真实数据流）
   左 内容输入 55% / 右 内容要求 45%；无 Inspector / 无 TimelineBar（01 §3）
   ========================================================================== */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { createSingleFlight, recoveryRead, recoveryWrite, recoveryRemove, registerNavigationGuard } from '../lib/navigation';
import type { Project } from '../lib/types';
import {
  useCapabilities, useGenerateScript, useImportScript, useJobs, useJobCancel, usePatchProject, useProject,
} from '../lib/api';
import { STAGE_META, type SourceKind } from '../lib/types';
import { Alert, Badge, Button, Choice, Field, Icon, Input, SegControl, Tabs, Textarea } from '../components/wu';
import { InfoTip } from '../components/InfoTip';
import { AppShell, StageHead } from '../components/AppShell';
import { useError } from '../lib/useError';
import { useProjectStore } from '../stores';

type DurationMode = 'auto' | 'approx';

const SOURCE_TABS: { id: SourceKind; label: string }[] = [
  { id: 'topic', label: '从话题开始' },
  { id: 'article', label: '粘贴文章' },
  { id: 'script', label: '已有脚本' },
];

const SOURCE_HINTS: Record<SourceKind, string> = {
  topic: '一句话或一段话即可，≤200 字',
  article: '粘贴文章正文，≤8,000 字',
  script: '每行一条发言，用 A: / B: 标记说话人，空行分段。例如：\nA: 大家好，欢迎回到本期节目。\nB: 今天我们聊点具体的。'
};

export function CreateEpisodePage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const caps = useCapabilities();
  const patchProject = usePatchProject();
  const generateScript = useGenerateScript();
  const importScript = useImportScript();
  const scriptFileRef = useRef<HTMLInputElement>(null);
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const generateOnce=useRef(createSingleFlight());
  const cancelJob = useJobCancel();
  const setView = useProjectStore((s) => s.setView);

  const [sourceKind, setSourceKind] = useState<SourceKind>('article');
  const [content, setContent] = useState('');
  const [durationMode, setDurationMode] = useState<DurationMode>('auto');
  const [approxMinutes, setApproxMinutes] = useState(3);
  const [aspect, setAspect] = useState<'landscape' | 'portrait'>('landscape');
  const [title, setTitle] = useState('');
  const { error, setError, dismiss: dismissError } = useError();

  const proj = project.data;
  const qc = useQueryClient();
  const initialized = useRef(false);
  const draftKey = `duocast.creation.${projectId}`;
  const dirty = useRef(false);
  const saveChain = useRef(Promise.resolve(true));
  const [saveLabel, setSaveLabel] = useState('读取草稿…');
  const [recoverable, setRecoverable] = useState<Project['creationDraft'] | null>(null);
  const draft = { sourceKind, content, durationMode, approxMinutes, title };
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const applyDraft = (d: NonNullable<Project['creationDraft']>) => {
    setContent(d.content ?? ''); setSourceKind(d.sourceKind ?? 'article');
    setDurationMode(d.durationMode ?? 'auto'); setApproxMinutes(d.approxMinutes ?? 3);
    setTitle(d.title ?? proj?.title ?? '');
  };
  useEffect(() => {
    if (!proj || initialized.current) return;
    initialized.current = true;
    const saved = proj.creationDraft ?? {};
    applyDraft({ ...saved, title: proj.title });
    const candidate = recoveryRead<NonNullable<Project['creationDraft']>>(draftKey);
    const local = candidate && typeof candidate==='object' && typeof candidate.content==='string' ? candidate : null;
    if (local && JSON.stringify(local) !== JSON.stringify({ ...saved, title: proj.title })) setRecoverable(local);
    setSaveLabel('已保存');
  }, [proj]);
  const saveDraft = () => {
    if (!dirty.current) return saveChain.current;
    saveChain.current = saveChain.current.then(async () => {
      const snapshot = draftRef.current;
      const latest = qc.getQueryData<Project>(['project', projectId]);
      if (!latest) return false;
      try {
        setSaveLabel('保存中…');
        await patchProject.mutateAsync({ projectId, expectedRevision: latest.revision,
          patch: { creationDraft: snapshot, title: snapshot.title.trim() || latest.title } });
        if (JSON.stringify(draftRef.current) === JSON.stringify(snapshot)) {
          dirty.current = false; recoveryRemove(draftKey);
        }
        setSaveLabel(dirty.current ? '仍有新编辑待保存' : '已保存'); return !dirty.current;
      } catch (e) { setSaveLabel('仅本机可恢复 · 服务端保存失败'); setError((e as Error).message); return false; }
    });
    return saveChain.current;
  };
  const lastDraft = useRef('');
  useEffect(() => {
    if (!initialized.current || !proj || recoverable) return;
    const serialized = JSON.stringify(draft);
    if (!lastDraft.current) { lastDraft.current = serialized; return; }
    if (serialized !== lastDraft.current) {
      lastDraft.current = serialized; dirty.current = true;
      recoveryWrite(draftKey, draft); setSaveLabel('本机草稿已保留');
    }
    const timer = window.setTimeout(() => { if (dirty.current) void saveDraft(); }, 800);
    return () => window.clearTimeout(timer);
  }, [content, title, sourceKind, durationMode, approxMinutes, proj?.id]);
  useEffect(() => registerNavigationGuard(saveDraft));
  useEffect(() => {
    const protect = (e: BeforeUnloadEvent) => { if (dirty.current) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', protect);
    return () => window.removeEventListener('beforeunload', protect);
  }, []);
  useEffect(() => {
    if (proj) setAspect(proj.aspect);
  }, [proj?.aspect]); // eslint-disable-line react-hooks/exhaustive-deps

  const textReady = caps.data?.textApi.ready ?? false;
  const canGenerate = !recoverable && content.trim().length > 0 && (sourceKind === 'script' || textReady) && !generateScript.isPending;

  // 当前工程的脚本生成任务（幂等 clientToken 定位）
  const genJob = useMemo(() => {
    const js = jobs.data ?? [];
    const j = js.find((x) => x.kind === 'script.generate');
    return j && (j.status === 'queued' || j.status === 'running' || j.status === 'succeeded' || j.status === 'failed') ? j : undefined;
  }, [jobs.data]);
  const generating = !!genJob && (genJob.status === 'queued' || genJob.status === 'running');
  // 「已生成」必须以工程真实存在脚本为准（第五轮验证：仅看任务成功会被
  // 工程 ID 复用后的残留任务骗成假成功，阶段②实际为空）。
  const hasScript = (proj?.scriptRevisions?.length ?? 0) > 0;
  const genDone = !!genJob && genJob.status === 'succeeded' && hasScript;
  const genFailed = !!genJob && genJob.status === 'failed' && !hasScript;

  const handleGenerate = () => generateOnce.current(async () => {
    setError(null);
    try {
      if (!await saveDraft()) return;
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
  });

  // 批 2（42 号方案）：外部脚本文件导入。.json 走结构化导入；.txt 读入后走现有解析导入。
  const handleScriptFile = async (file: File | undefined) => {
    if (!file || !proj) return;
    setError(null);
    setImportNotice(null);
    const text = await file.text();
    if (file.name.toLowerCase().endsWith('.json')) {
      let parsed: unknown;
      try { parsed = JSON.parse(text); } catch { setError('JSON 解析失败：请检查文件格式'); return; }
      const obj = (parsed && typeof parsed === 'object') ? parsed as Record<string, unknown> : {};
      const payload = (obj.payload && typeof obj.payload === 'object') ? obj.payload : obj;
      importScript.mutate({
        projectId,
        envelope: {
          schemaVersion: '1.0', kind: 'script', clientToken: crypto.randomUUID(),
          expectedRevision: proj.revision,
          provenance: { source: 'external-upload', tool: file.name },
          payload,
        },
      }, {
        onSuccess: (r) => setImportNotice(`已导入 ${r.turnCount} 条话轮 → 草稿 ${r.revisionId}${r.warnings?.length ? '。' + r.warnings.join('；') : ''}。请前往编辑对话检查后确认。`),
        onError: (e) => setError((e as Error).message),
      });
    } else {
      setContent(text);
      setSourceKind('script');
      setImportNotice('已读入脚本文本，点击下方「解析并导入」完成导入。');
    }
  };

  const handleAspect = (v: 'landscape' | 'portrait') => {
    if(recoverable)return;
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
  // P0-A：本次生成任务的真实结果（版本号 + 是否已切为当前草稿）
  const genMeta = genJob?.result?.meta as { revisionId?: string; applied?: boolean } | undefined;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      footer={null}
    >
      <StageHead title="① 创建本期">
        <span role="status" className="wu-caption">{saveLabel}</span>
        {proj && <Badge>{proj.generationMode === 'fast' ? '快速模式 · 双人演播室' : '原有模式'}</Badge>}
        {stageState && stageState !== 'ready' && <Badge tone={STAGE_META[stageState].tone}>{STAGE_META[stageState].label}</Badge>}
        {/* 版本号（revision）是内部概念，第五轮实测 B3：不再在页头展示。 */}
        <span className="hf-spacer" />
      </StageHead>

      <div className="hf-cols">
        {recoverable && <Alert tone="warning">发现本机恢复草稿。
          <Button onClick={() => { applyDraft(recoverable); setRecoverable(null); }}>恢复本机草稿</Button>
          <Button onClick={() => { recoveryWrite(draftKey, draft); setRecoverable(null); }}>使用服务端版本</Button>
        </Alert>}
        {/* ---- 左：内容输入（55%） ---- */}
        <section className="wu-card hf-panel in">
          <h3 className="hf-panel-t">内容输入</h3>
          <Tabs
            items={SOURCE_TABS.map((t) => ({ id: t.id, label: t.label }))}
            active={sourceKind}
            onSelect={(id) => {if(!recoverable)setSourceKind(id as SourceKind);}}
            idPrefix="src"
          />
          <div style={{ marginTop: 12, display: 'grid', gap: 12 }}>
            <Field labelFor="src-content" label={SOURCE_TABS.find((t) => t.id === sourceKind)?.label ?? '内容'}>
              <Textarea
                id="src-content"
                rows={12}
                placeholder={SOURCE_HINTS[sourceKind]}
                value={content}
                disabled={generating||!!recoverable}
                onChange={(e) => setContent(e.target.value)}
              />
            </Field>
            {sourceKind === 'article' && content.trim().length > 8000 && (
              <Alert tone="warning">文章超过 8,000 字，建议拆分为多期或截取核心段落。</Alert>
            )}
            {sourceKind === 'topic' && content.trim().length > 200 && (
              <Alert tone="warning">话题超过 200 字，建议改用「粘贴文章」。</Alert>
            )}
            {sourceKind === 'script' && (
              <div className="wu-row" style={{ gap: 8 }}>
                <Button variant="secondary" size="sm" icon="upload" busy={importScript.isPending}
                  disabled={generating || !!recoverable || importScript.isPending}
                  onClick={() => scriptFileRef.current?.click()}>
                  上传脚本文件（.json / .txt）
                </Button>
                <span className="wu-caption">.json 按结构化契约导入；.txt 用 A:/B: 格式</span>
                <input ref={scriptFileRef} type="file" accept=".json,.txt,application/json,text/plain" hidden
                  aria-label="上传脚本文件"
                  onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; void handleScriptFile(f); }} />
              </div>
            )}
            {importNotice && <Alert tone="success" onClose={() => setImportNotice(null)}>{importNotice}</Alert>}

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
              </div>
            ) : genDone ? (
              <div className="wu-alert" data-tone="success" style={{ display: 'grid', gap: 8 }}>
                <div className="wu-row">
                  <Badge tone="success" icon="check">脚本草稿已生成</Badge>
                  {/* P0-A：显示本次生成的版本号，而不是可能被并发改动的当前草稿指针 */}
                  {genMeta?.revisionId && <span className="hf-mono">{genMeta.revisionId}</span>}
                </div>
                {genMeta?.applied === false && (
                  <Alert tone="warning">
                    生成期间输入被改动：新脚本 {genMeta.revisionId} 已存为历史版本，未替换当前草稿。
                    <Button variant="secondary" size="sm" disabled={!proj || patchProject.isPending}
                      onClick={() => proj && genMeta.revisionId && patchProject.mutate(
                        { projectId, expectedRevision: proj.revision, patch: { currentDraftRevision: genMeta.revisionId } },
                        { onError: (e) => setError((e as Error).message) },
                      )}>
                      设为当前草稿
                    </Button>
                  </Alert>
                )}
                <div className="wu-row">
                  <span className="hf-spacer" />
                  {/* 内容仍在本页输入框中，允许就地重新生成（否则只能绕道阶段②的「重新生成」） */}
                  <Button variant="secondary" size="sm" icon="refresh" disabled={!canGenerate} onClick={handleGenerate}>
                    重新生成（替换当前草稿）
                  </Button>
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
                  <span className="wu-caption">详情见任务中心。</span>
                  <span className="hf-spacer" />
                  <Button variant="secondary" size="sm" onClick={() => setView('tasks')}>查看任务中心</Button>
                  <Button variant="brand" size="sm" disabled={!canGenerate} onClick={handleGenerate}>重新生成</Button>
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
                {error && <Alert tone="danger" onClose={dismissError}>生成失败：{error}</Alert>}
                <Button variant="brand" icon="sparkle" disabled={!canGenerate} onClick={handleGenerate}>
                  {sourceKind === 'script' ? '解析并导入' : '生成对话'}
                </Button>
              </>
            )}
          </div>
        </section>

        {/* ---- 右：内容要求（45%） ---- */}
        <section className="wu-card hf-panel req">
          <h3 className="hf-panel-t">内容要求</h3>

          <div className="wu-stack">
          <Field label="节目名">
            <Input
              placeholder="未命名节目"
              disabled={!!recoverable}
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
                onChange={() => {if(!recoverable)setDurationMode('auto');}}
                label={<span>智能时长 <InfoTip text="根据内容自动决定时长，最多 5 分钟；超出不会自动截断尾句。" /></span>}
              />
              <div className="wu-row" style={{ gap: 8 }}>
                <Choice checked={durationMode === 'approx'} onChange={() => {if(!recoverable)setDurationMode('approx');}} label="大约" />
                <SegControl
                  options={[1, 2, 3, 4].map((n) => ({ value: String(n), label: String(n) }))}
                  value={String(approxMinutes)}
                  onChange={(v) => {if(!recoverable)setApproxMinutes(Number(v));}}
                />
                <span className="wu-caption">分钟</span>
              </div>
            </div>
          </Field>

          <Field label="比例">
            <div className="wu-row">
              <Choice checked={aspect === 'landscape'} onChange={() => handleAspect('landscape')} label="横屏 16:9" />
              {proj?.generationMode !== 'fast' && caps.data?.video.mode === 'mock' && <Choice checked={aspect === 'portrait'} onChange={() => handleAspect('portrait')} label="竖屏（仅演示）" />}
              {caps.data?.video.mode === 'live' && <InfoTip text="当前真实引擎仅支持横屏；竖屏仅在演示模式可用。" />}
            </div>
          </Field>
          {aspect === 'portrait' && (
            <Alert tone="warning">竖屏为上下分栏双人构图，需独立检查。</Alert>
          )}

          <Field label="角色">
            <div className="hf-upload" onClick={() => setView('assets')}>
              <div className="t"><Icon name="image" size={16} /> 角色图</div>
              <div className="s">点击前往资产库</div>
            </div>
          </Field>
          </div>
        </section>
      </div>
    </AppShell>
  );
}
