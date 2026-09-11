/* ============================================================================
   HF-02 · 阶段② 编辑对话（真实数据流，01 §4 权威）
   主视图：单列话轮列表 + 底部统计条；检查器：内容结构 / 所选发言属性 / 质量提示
   编辑 → 草稿版本 upsert（已用版本不覆盖）；确认脚本 → approvals 只追加
   ========================================================================== */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  upsertDraftTurns, useConfirm, useGenerateScript, useJobs, useProject, useSaveDraft,
  useScriptRewrite,
} from '../lib/api';
import { type Job, type ScriptRevision, type Turn } from '../lib/types';
import { Alert, Badge, Button, Icon, Input, Modal, Textarea } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const INTENTS: { id: Turn['intent']; label: string }[] = [
  { id: 'explain', label: '解释' }, { id: 'probe', label: '追问' },
  { id: 'example', label: '举例' }, { id: 'challenge', label: '质疑' },
  { id: 'summarize', label: '总结' }, { id: 'other', label: '其他' },
];

const TOOLBAR_ACTIONS: { id: 'rewrite' | 'casual' | 'probe' | 'dedupe' | 'trim'; label: string }[] = [
  { id: 'rewrite', label: '改写' },
  { id: 'casual', label: '更口语化' },
  { id: 'probe', label: '增加追问' },
  { id: 'dedupe', label: '减少重复' },
  { id: 'trim', label: '精简至目标时长' },
];

interface QaHint { kind: 'local' | 'ai'; level: 'warn' | 'info'; title: string; detail: string; }

function turnText(t: Turn): string {
  return t.lines.map((l) => l.displayText).join('');
}

function countHints(turns: Turn[]): { hints: QaHint[]; pronCount: number } {
  const hints: QaHint[] = [];
  let pronCount = 0;
  const openings = new Map<string, Turn[]>();
  turns.forEach((t) => {
    const text = turnText(t);
    if (!text) return;
    const prons = text.match(/[A-Z]{2,}|[\d]+(?=[%％:]|(?:个|分|秒|亿|万|元|张|倍))/g);
    if (prons) pronCount += prons.length;
    const open = text.slice(0, 6);
    const arr = openings.get(`${t.speaker}:${open}`) ?? [];
    arr.push(t);
    openings.set(`${t.speaker}:${open}`, arr);
    if (text.length > 90) {
      hints.push({
        kind: 'local', level: 'warn', title: '连续独白较长',
        detail: `本地计算：${t.id} 单条 ${text.length} 字，建议拆分或插问`,
      });
    }
    if (/[\d]/.test(text) && !(t.sourceAnchors?.length)) {
      hints.push({
        kind: 'local', level: 'info', title: '这一段缺少来源',
        detail: `本地计算：${t.id} 断言无来源锚点`,
      });
    }
  });
  openings.forEach((arr) => {
    if (arr.length >= 2) {
      const ids = arr.map((t) => t.id).join(' / ');
      hints.push({
        kind: 'ai', level: 'warn', title: `${arr[0].speaker} 连续重复`,
        detail: `${ids} 均以「${turnText(arr[0]).slice(0, 6)}」开头`,
      });
    }
  });
  return { hints: hints.slice(0, 6), pronCount };
}

function estimateSecs(turns: Turn[]): number {
  const chars = turns.reduce((n, t) => n + turnText(t).length, 0);
  return Math.round(chars * 0.22 + turns.length * 0.32);
}

function fmtMs(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function EditScriptPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const saveDraft = useSaveDraft();
  const confirm = useConfirm();
  const regenerate = useGenerateScript();
  const rewrite = useScriptRewrite();
  const setView = useProjectStore((s) => s.setView);

  const proj = project.data;
  const [viewRevId, setViewRevId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [candidate, setCandidate] = useState<{ turnIds: string[]; turns: Turn[]; jobId: string } | null>(null);
  const [dictOpen, setDictOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saveTimer = useRef<number | null>(null);

  // 当前查看版本：默认当前草稿
  const revision: ScriptRevision | undefined = useMemo(() => {
    const revs = proj?.scriptRevisions ?? [];
    return revs.find((r) => r.id === (viewRevId ?? proj?.currentDraftRevision)) ?? revs[revs.length - 1];
  }, [proj, viewRevId]);

  // 版本切换 → 载入话轮
  useEffect(() => {
    if (revision) {
      setTurns(revision.turns.map((t) => ({ ...t, lines: t.lines.map((l) => ({ ...l })) })));
      setSelected([]);
      setCandidate(null);
    }
  }, [revision?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // 选区改写结果：只为「页面打开期间完成」的任务弹候选，吸收挂载前已完成的旧任务
  // （11 报告 P2-15：历史会话的旧候选不再反复弹出）
  const rewriteSeen = useRef<Map<string, string>>(new Map());
  useEffect(() => {
    (jobs.data ?? []).forEach((x) => {
      if (x.kind !== 'script.rewrite') return;
      const prev = rewriteSeen.current.get(x.id);
      rewriteSeen.current.set(x.id, x.status);
      if (prev === undefined) return;
      if (prev !== 'succeeded' && x.status === 'succeeded' && x.result?.candidate && !candidate) {
        const cand = x.result.candidate as { turns: Turn[]; changedTurnIds: string[] };
        const ct = cand.turns ?? [];
        if (ct.length) {
          setCandidate({ turnIds: cand.changedTurnIds, turns: ct, jobId: x.id });
        }
      }
    });
  }, [jobs.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // 自动保存（2s 防抖，01 §4.2）；baseRevId 是正在编辑的版本（11 报告 P0-1：
  // 查看历史版本时编辑 → 新建草稿，绝不覆盖当前草稿内容）
  const scheduleSave = (nextTurns: Turn[]) => {
    if (!proj) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const { patch, revId } = upsertDraftTurns(proj, nextTurns, viewRevId ?? undefined);
      saveDraft.mutate({ projectId, patch, expectedRevision: proj.revision }, {
        onError: (e) => setError((e as Error).message),
      });
      setViewRevId(revId);
    }, 2000);
  };

  const applyTurns = (next: Turn[]) => {
    setTurns(next);
    scheduleSave(next);
  };

  // ---- 话轮操作 ----
  const selectTurn = (id: string, additive: boolean) => {
    setSelected((s) => (additive
      ? (s.includes(id) ? s.filter((x) => x !== id) : [...s, id])
      : (s.length === 1 && s[0] === id ? [] : [id])));
  };

  const updateTurn = (id: string, patch: Partial<Turn>) => {
    applyTurns(turns.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  };

  const moveTurn = (id: string, dir: -1 | 1) => {
    const i = turns.findIndex((t) => t.id === id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= turns.length) return;
    const next = [...turns];
    [next[i], next[j]] = [next[j], next[i]];
    applyTurns(next);
  };

  const addTurn = () => {
    const n = turns.length + 1;
    const id = `T${String(Math.max(1, ...turns.map((t) => Number(t.id.replace(/\D/g, '')) || 0)) + 1).padStart(2, '0')}`;
    applyTurns([...turns, {
      id, speaker: 'A', intent: 'other', tone: '自然', speedRatio: 1.0,
      lines: [{ id: `L${String(n * 10).padStart(3, '0')}`, displayText: '', spokenText: '' }],
    }]);
  };

  const removeTurns = (ids: string[]) => {
    applyTurns(turns.filter((t) => !ids.includes(t.id)));
    setSelected((s) => s.filter((x) => !ids.includes(x)));
  };

  const handleRewrite = (mode: 'rewrite' | 'casual' | 'probe' | 'dedupe' | 'trim') => {
    if (!proj || !revision || selected.length === 0) return;
    setCandidate(null);
    rewrite.mutate({
      projectId, clientToken: crypto.randomUUID(), revisionId: revision.id,
      turnIds: selected, mode,
    }, {
      onError: (e) => setError((e as Error).message),
    });
  };

  const acceptCandidate = () => {
    if (!candidate) return;
    const byId = new Map(candidate.turns.map((t) => [t.id, t]));
    applyTurns(turns.map((t) => byId.get(t.id) ?? t));
    setCandidate(null);
  };

  const handleConfirm = () => {
    if (!proj || !revision) return;
    // 确认只对当前草稿开放（11 报告 P1-6：确认历史版本不会推进阶段，两条口径会分裂）
    if (!viewIsDraft) return;
    confirm.mutate({ projectId, expectedRevision: proj.revision, kind: 'script', inputRevisionId: revision.id }, {
      onError: (e) => setError((e as Error).message),
    });
  };

  const handleRegenerate = () => {
    if (!proj) return;
    const src = revision?.sourceInput ?? { kind: 'article', content: '' };
    regenerate.mutate({
      projectId, clientToken: crypto.randomUUID(),
      sourceInput: { kind: src.kind, content: src.content ?? '' },
      brief: { aspect: proj.aspect },
    }, { onError: (e) => setError((e as Error).message) });
  };

  // ---- 派生 ----
  const scriptApproved = proj?.approvals.some(
    (a) => a.kind === 'script' && a.decision === 'accepted' && revision && a.inputRevisionId === revision.id,
  ) ?? false;
  const { hints, pronCount } = useMemo(() => countHints(turns), [turns]);
  const charCount = useMemo(() => turns.reduce((n, t) => n + turnText(t).length, 0), [turns]);
  const speakerCount = useMemo(() => ({
    A: turns.filter((t) => t.speaker === 'A').length,
    B: turns.filter((t) => t.speaker === 'B').length,
  }), [turns]);
  const genJob: Job | undefined = useMemo(() => (jobs.data ?? []).find(
    (j) => j.kind === 'script.generate' && (j.status === 'queued' || j.status === 'running'),
  ), [jobs.data]);
  const generating = !!genJob;
  const selectedTurn = turns.find((t) => t.id === selected[selected.length - 1]);
  const viewIsDraft = !!revision && !!proj && revision.id === proj.currentDraftRevision;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>内容结构 <span className="wu-caption" style={{ fontWeight: 400 }}>{revision?.id}</span></h4>
            <ul className="hf-ins-list">
              <li>核心问题：{revision?.contentBrief?.coreQuestion || '—'}</li>
              <li>主要观点：{revision?.contentBrief?.keyPoints?.join(' / ') || '—'}</li>
              <li>必要事实：{revision?.contentBrief?.necessaryFacts?.join(' / ') || '—'}</li>
              <li>双方职责：{revision?.contentBrief?.speakerDuties || '—'}</li>
            </ul>
          </div>

          <div className="hf-ins-sec">
            <h4>所选发言 <span className="wu-caption" style={{ fontWeight: 400 }}>{selectedTurn ? `#${selectedTurn.id}` : '未选'}</span></h4>
            {selectedTurn ? (
              <>
                <div className="hf-fld">
                  <label>说话人</label>
                  <div className="wu-row" style={{ gap: 6 }}>
                    {(['A', 'B'] as const).map((spk) => (
                      <Button
                        key={spk} variant={selectedTurn.speaker === spk ? 'secondary' : 'ghost'} size="sm"
                        onClick={() => updateTurn(selectedTurn.id, { speaker: spk })}
                      >{spk} · {spk === 'A' ? '主持' : '嘉宾'}</Button>
                    ))}
                  </div>
                </div>
                <div className="hf-fld">
                  <label>意图</label>
                  <select
                    className="wu-input" aria-label="意图" value={selectedTurn.intent}
                    onChange={(e) => updateTurn(selectedTurn.id, { intent: e.target.value as Turn['intent'] })}
                  >
                    {INTENTS.map((it) => <option key={it.id} value={it.id}>{it.label}</option>)}
                  </select>
                </div>
                <div className="hf-fld">
                  <label>语气</label>
                  <Input
                    aria-label="语气" value={selectedTurn.tone}
                    onChange={(e) => updateTurn(selectedTurn.id, { tone: e.target.value })}
                  />
                </div>
                <div className="hf-fld">
                  <label>语速</label>
                  <span className="hf-seg" aria-label="语速调节">
                    <button type="button" aria-label="减慢" onClick={() => updateTurn(selectedTurn.id, { speedRatio: Math.max(0.5, +(selectedTurn.speedRatio - 0.05).toFixed(2)) })}>-</button>
                    <span style={{ fontSize: 12, padding: '0 10px', display: 'inline-flex', alignItems: 'center' }}>{selectedTurn.speedRatio.toFixed(2)}</span>
                    <button type="button" aria-label="加快" onClick={() => updateTurn(selectedTurn.id, { speedRatio: Math.min(1.5, +(selectedTurn.speedRatio + 0.05).toFixed(2)) })}>+</button>
                  </span>
                </div>
                <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
                  显示文本与朗读文本分别保存（displayText / spokenText）；读法见
                  <a className="hf-link" onClick={() => setDictOpen(true)}>读音词典</a>。
                </p>
              </>
            ) : (
              <p className="wu-caption">点击话轮卡片查看其发音属性；Ctrl/⌘ 多选后可用选区工具条。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>质量提示</h4>
            {hints.length === 0 ? (
              <p className="wu-caption">本地计算与语义建议均无异常。</p>
            ) : hints.map((h, i) => (
              <div className="hf-qa" key={i} style={{ marginBottom: 8 }}>
                <span className="q">
                  <b>{h.title}</b>
                  <br />
                  {h.kind === 'ai' && <span className="hf-ai" style={{ margin: '4px 0', display: 'inline-flex' }}>AI 建议</span>}
                  {' '}{h.detail}
                </span>
                {h.title === '连续独白较长' && (
                  <Button variant="ghost" size="sm" onClick={() => setView('script')}>拆分</Button>
                )}
              </div>
            ))}
            {pronCount > 0 && (
              <div className="hf-qa" style={{ marginTop: 8 }}>
                <span className="q"><b>读音：{pronCount} 处</b><br />本地计算：专名 / 缩写 / 数字，可在读音词典中维护</span>
              </div>
            )}
          </div>
        </aside>
      }
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('create')}>上一步</Button>
            <Badge tone="warning" icon="alert">确认点 · 脚本确认</Badge>
            <span className="wu-caption">确认后可选自动启动配音</span>
            <span className="hf-spacer" />
            <Button size="sm" onClick={() => setView('voice')}>下一步<Icon name="arrowRight" size={14} /></Button>
          </span>
        }
      />
      }
    >
      <StageHead title="② 编辑对话">
        {revision && (
          <Badge tone={scriptApproved ? 'success' : 'warning'}>
            {scriptApproved ? `脚本 ${revision.id} · 已确认` : `草稿 ${revision.id} · 未确认`}
          </Badge>
        )}
        <span className="hf-spacer" />
        <Button variant="secondary" size="sm" icon="refresh" disabled={generating} onClick={handleRegenerate}>重新生成</Button>
        <label className="wu-input" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '0 10px', height: 30, width: 'auto', cursor: 'pointer' }}>
          <Icon name="edit" size={14} />版本
          <select
            className="hf-mini-select"
            aria-label="选择版本"
            style={{ border: 'none', background: 'none' }}
            value={revision?.id ?? ''}
            onChange={(e) => { setViewRevId(e.target.value); setCandidate(null); }}
          >
            {(proj?.scriptRevisions ?? []).map((r) => (
              <option key={r.id} value={r.id}>{r.id}{r.id === proj?.currentDraftRevision ? ' · 草稿' : ''}</option>
            ))}
          </select>
        </label>
        <Button
          variant="brand" size="sm" icon="check"
          disabled={!revision || scriptApproved || generating || !viewIsDraft}
          title={viewIsDraft ? undefined : '仅当前草稿可确认；请在版本选择器切回草稿'}
          onClick={handleConfirm}
        >
          {scriptApproved ? '已确认' : '确认脚本'}
        </Button>
      </StageHead>

      <div className="hf-body">
        {/* ---- 主视图：话轮列表 ---- */}
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
          {scriptApproved && (
            <Alert tone="info">
              已绑定脚本 {revision?.id}。编辑将创建新草稿版本，不覆盖已用版本；确认后可选自动启动配音。
              <a className="hf-link" onClick={() => setView('voice')}>前往阶段③ 试听配音</a>
            </Alert>
          )}
          {error && <Alert tone="danger">{error}</Alert>}
          {!viewIsDraft && revision && (
            <Alert tone="warning">正在查看历史版本 {revision.id}（非当前草稿）；编辑将基于该版本创建新草稿。</Alert>
          )}

          {candidate && (
            <div className="hf-cand" style={{ marginBottom: 12 }}>
              <div className="t">
                候选改写 · 差异：共改 {candidate.turnIds.length} 处
                <span className="hf-ai" style={{ marginLeft: 8 }}>AI 建议</span>
              </div>
              {candidate.turns.filter((t) => candidate.turnIds.includes(t.id)).map((t) => (
                <p key={t.id}><b style={{ fontFamily: 'var(--wu-global-mono)' }}>{t.id}</b> {turnText(t)}</p>
              ))}
              <div className="wu-row" style={{ gap: 8 }}>
                <Button variant="brand" size="sm" icon="check" onClick={acceptCandidate}>接受</Button>
                <Button variant="ghost" size="sm" onClick={() => setCandidate(null)}>拒绝</Button>
                <span className="wu-caption">其他发言不自动重写</span>
              </div>
            </div>
          )}

          <div className="hf-list">
            {selected.length > 0 && (
              <div className="hf-toolbar">
                <span className="cap">已选 {selected.length > 1 ? `T… 共 ${selected.length} 条` : selected[0]}</span>
                {TOOLBAR_ACTIONS.map((a) => (
                  <Button key={a.id} variant="secondary" size="sm" disabled={rewrite.isPending} onClick={() => handleRewrite(a.id)}>
                    {a.label}
                  </Button>
                ))}
                <span className="hf-spacer" />
                <span className="hf-ai">AI 建议</span>
              </div>
            )}

            {turns.length === 0 && (
              <div className="wu-empty">
                <div style={{ fontWeight: 700 }}>还没有话轮</div>
                <p className="wu-caption" style={{ margin: '6px auto 14px', maxWidth: 420 }}>
                  在阶段①生成对话，或点下方「发言」手动添加。
                </p>
              </div>
            )}

            {turns.map((t, idx) => {
              const text = turnText(t);
              const isSel = selected.includes(t.id);
              return (
                <div
                  key={t.id}
                  className={`hf-turn ${isSel ? 'selected' : ''}`}
                  onClick={(e) => selectTurn(t.id, e.ctrlKey || e.metaKey)}
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData('text/plain', t.id)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    const srcId = e.dataTransfer.getData('text/plain');
                    const src = turns.findIndex((x) => x.id === srcId);
                    const dst = turns.findIndex((x) => x.id === t.id);
                    if (src >= 0 && dst >= 0 && src !== dst) {
                      const next = [...turns];
                      const [moved] = next.splice(src, 1);
                      next.splice(dst, 0, moved);
                      applyTurns(next);
                    }
                  }}
                >
                  <div className="hf-turn-head">
                    <span className="hf-grip" title="拖动排序"><Icon name="grip" size={12} /></span>
                    <span className="hf-turn-id">#{t.id}</span>
                    <Badge tone={t.speaker === 'A' ? 'info' : 'speaker-b'}>{t.speaker} · {t.speaker === 'A' ? '主持' : '嘉宾'}</Badge>
                    <select
                      className="hf-mini-select" aria-label={`${t.id} 意图`}
                      value={t.intent}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => updateTurn(t.id, { intent: e.target.value as Turn['intent'] })}
                    >
                      {INTENTS.map((it) => <option key={it.id} value={it.id}>{it.label}</option>)}
                    </select>
                    <span className="hf-spacer" />
                    {/* 用设计系统的 .wu-icon-btn[data-size="sm"]（32px 方形），替换此前自造的 .hf-icon-btn（26px）。
                        权威基线里 .wu-icon-btn 只给了尺寸、没给外观重置，故在 duo-shell.css 补齐幽灵态。 */}
                    <button
                      type="button" className="wu-icon-btn" data-size="sm" aria-label={`上移 ${t.id}`}
                      disabled={idx === 0} onClick={(e) => { e.stopPropagation(); moveTurn(t.id, -1); }}
                    ><Icon name="chevronUp" size={16} /></button>
                    <button
                      type="button" className="wu-icon-btn" data-size="sm" aria-label={`下移 ${t.id}`}
                      disabled={idx === turns.length - 1} onClick={(e) => { e.stopPropagation(); moveTurn(t.id, 1); }}
                    ><Icon name="chevronDown" size={16} /></button>
                    <button
                      type="button" className="wu-icon-btn" data-size="sm" aria-label={`删除 ${t.id}`}
                      onClick={(e) => { e.stopPropagation(); removeTurns([t.id]); }}
                    ><Icon name="trash" size={16} /></button>
                  </div>
                  {editingId === t.id ? (
                    <Textarea
                      className="wu-input"
                      rows={3}
                      value={editText}
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => setEditText(e.target.value)}
                      onBlur={() => {
                        updateTurn(t.id, { lines: t.lines.map((l, i) => (i === 0 ? { ...l, displayText: editText, spokenText: editText } : l)) });
                        setEditingId(null);
                      }}
                      onKeyDown={(e) => { if (e.key === 'Escape') { setEditingId(null); } }}
                    />
                  ) : (
                    <p
                      className="hf-turn-text"
                      style={{ cursor: 'text' }}
                      onClick={(e) => { e.stopPropagation(); setEditingId(t.id); setEditText(text); }}
                      title="点击编辑台词"
                    >{text || <span className="wu-caption">（空话轮 · 点击输入台词）</span>}</p>
                  )}
                  <div className="hf-turn-meta">
                    <span>语气：{t.tone}</span>
                    <span>语速：{t.speedRatio.toFixed(2)}</span>
                    {t.sourceAnchors?.length ? <span>来源锚点：{t.sourceAnchors.length} 处</span> : null}
                    {pronCount > 0 && t.id === turns.find((x) => x.id === selected[selected.length - 1])?.id
                      ? <span className="hf-meta-warn"><Icon name="alert" size={12} />读音：{pronCount} 处</span> : null}
                  </div>
                </div>
              );
            })}

            <div className="wu-row" style={{ gap: 12, margin: '2px 0 8px' }}>
              <Button variant="secondary" size="sm" icon="plus" onClick={addTurn}>发言</Button>
              <span className="wu-caption">拖动发言左侧手柄排序 · 稳定 ID（{turns[0]?.id}）不随显示序号改变</span>
            </div>
            <div className="wu-row" style={{ gap: 12, marginBottom: 6 }}>
              <Button variant="secondary" size="sm" icon="edit" onClick={() => setDictOpen(true)}>读音词典</Button>
              <span className="wu-caption">专名 · 数字 · 缩写；显示文本与朗读文本分别保存</span>
            </div>
          </div>
        </div>

      </div>

      {/* ---- 底部统计条（01 §4.1） ---- */}
      <div className="hf-stats">
        <span className="hf-pos">话轮 {turns.length}</span>
        <span>A {speakerCount.A} / B {speakerCount.B}</span>
        <span>字数 {charCount.toLocaleString()}</span>
        <span>预估 {fmtMs(estimateSecs(turns))}（基于历史语速）</span>
        <span className="hf-spacer" />
        <span className="wu-caption">自动保存 2s 防抖 · 拖动手柄可排序</span>
      </div>

      {/* ---- 读音词典（01 §4.2 全局面板） ---- */}
      <DictModal
        open={dictOpen}
        onClose={() => setDictOpen(false)}
        hintPron={pronCount}
      />
    </AppShell>
  );
}

function DictModal({ open, onClose, hintPron }: { open: boolean; onClose: () => void; hintPron: number }) {
  const [rows, setRows] = useState<{ term: string; read: string; scope: '全局' | '本工程' }[]>([
    { term: 'KV 缓存', read: '读作「K-V 缓存」', scope: '全局' },
    { term: 'MiniMax', read: '英文原音', scope: '全局' },
  ]);
  const [term, setTerm] = useState('');
  const [read, setRead] = useState('');
  return (
    <Modal
      open={open} onClose={onClose} title="读音词典" width={520}
      footer={
        <div className="wu-row" style={{ gap: 8 }}>
          <Button variant="secondary" size="sm" onClick={() => { if (term && read) { setRows([...rows, { term, read, scope: '本工程' }]); setTerm(''); setRead(''); } }}>新增</Button>
          <span className="hf-spacer" />
          <Button size="sm" onClick={onClose}>完成</Button>
        </div>
      }
    >
      <div className="hf-dict-row" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 8 }}>
        <Input placeholder="词条（如 3:12）" value={term} onChange={(e) => setTerm(e.target.value)} aria-label="词条" />
        <Input placeholder="读法（如 三分十二秒）" value={read} onChange={(e) => setRead(e.target.value)} aria-label="读法" />
        <span className="wu-caption" style={{ alignSelf: 'center' }}>本工程</span>
      </div>
      {hintPron > 0 && <p className="wu-caption">当前草稿检出读音提示 {hintPron} 处，可在下方维护后由合成阶段读取。</p>}
      {rows.map((r) => (
        <div className="hf-dict-row" key={r.term + r.read}>
          <span className="term">{r.term}</span>
          <span className="read">{r.read}</span>
          <span className="hf-spacer" />
          <span className="wu-caption">{r.scope}</span>
        </div>
      ))}
    </Modal>
  );
}
