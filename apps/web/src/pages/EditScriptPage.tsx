/* ============================================================================
   HF-02 · 阶段② 编辑对话（真实数据流，01 §4 权威）
   主视图：单列话轮列表 + 底部统计条；检查器：内容结构 / 所选发言属性 / 质量提示
   编辑 → 草稿版本 upsert（已用版本不覆盖）；确认脚本 → approvals 只追加
   ========================================================================== */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  upsertDraftTurns, useBranchScript, useConfirm, useGenerateScript, useJobs, useMachine, useMergeScript,
  usePatchProject, useProject, useSaveDraft, useSaveMachine, useScriptDiff, useScriptRewrite,
} from '../lib/api';
import { type Job, type Project, type PronunciationEntry, type ScriptRevision, type Turn } from '../lib/types';
import { Alert, Badge, Button, Choice, Icon, Input, Modal, Textarea } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { formatTimecode } from '../lib/format';
import { useError } from '../lib/useError';
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

interface QaHint { kind: 'local' | 'ai'; level: 'warn' | 'info'; title: string; detail: string; turnId?: string; }

function turnText(t: Turn): string {
  return t.lines.map((l) => l.displayText).join('');
}

/** 最靠近中点的句边界（不含末尾句号）；找不到返回 -1 */
function pickSplitOffset(text: string): number {
  const mid = text.length / 2;
  let best = -1;
  for (const m of text.matchAll(/[。！？；.!?;]/g)) {
    const pos = (m.index ?? 0) + m[0].length;
    if (pos <= 0 || pos >= text.length) continue;
    if (best < 0 || Math.abs(pos - mid) < Math.abs(best - mid)) best = pos;
  }
  return best;
}

function countHints(turns: Turn[]): { hints: QaHint[]; pronCount: number } {
  const hints: QaHint[] = [];
  let pronCount = 0;
  const openings = new Map<string, Turn[]>();
  turns.forEach((t) => {
    const text = turnText(t);
    if (!text) {
      hints.push({
        kind: 'local', level: 'warn', title: '存在空话轮', turnId: t.id,
        detail: `${t.id} 没有台词，确认前请补全或删除（空话轮会导致合成失败）`,
      });
      return;
    }
    const prons = text.match(/[A-Z]{2,}|[\d]+(?=[%％:]|(?:个|分|秒|亿|万|元|张|倍))/g);
    if (prons) pronCount += prons.length;
    const open = text.slice(0, 6);
    const arr = openings.get(`${t.speaker}:${open}`) ?? [];
    arr.push(t);
    openings.set(`${t.speaker}:${open}`, arr);
    if (text.length > 90) {
      hints.push({
        kind: 'local', level: 'warn', title: '连续独白较长', turnId: t.id,
        detail: `${t.id} 单条 ${text.length} 字，建议拆分或插问`,
      });
    }
    if (/[\d]/.test(text) && !(t.sourceAnchors?.length)) {
      hints.push({
        kind: 'local', level: 'info', title: '这一段缺少来源',
        detail: `${t.id} 断言无来源锚点`,
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
  // 提前暴露阶段④的样片硬约束（此前到生成样片时才发现，需回本阶段返工）
  if (turns.length > 0) {
    const seq = turns.map((t) => t.speaker);
    const hasAlternating = seq.some((s, i) => i + 2 < seq.length && s === 'A' && seq[i + 1] === 'B' && seq[i + 2] === 'A');
    if (!hasAlternating) {
      hints.push({
        kind: 'local', level: 'warn', title: '缺少 A→B→A 交替区间',
        detail: '阶段④样片需要双方连续交替的 ≥3 条话轮；当前脚本将无法生成样片，请调整说话人或补一条发言。',
      });
    }
  }
  return { hints: hints.slice(0, 6), pronCount };
}

function estimateSecs(turns: Turn[]): number {
  const chars = turns.reduce((n, t) => n + turnText(t).length, 0);
  return Math.round(chars * 0.22 + turns.length * 0.32);
}

export function EditScriptPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const saveDraft = useSaveDraft();
  const confirm = useConfirm();
  const regenerate = useGenerateScript();
  const rewrite = useScriptRewrite();
  const setView = useProjectStore((s) => s.setView);
  const qc = useQueryClient();

  const proj = project.data;
  const [viewRevId, setViewRevId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  // 最新话轮的同步镜像：同一批次内连续多次增删改必须基于最新状态计算，
  // 否则各操作都基于旧渲染闭包互相覆盖（实测：快速连删 4 条只生效 1 条）。
  const turnsRef = useRef<Turn[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [candidate, setCandidate] = useState<{ turnIds: string[]; turns: Turn[]; jobId: string } | null>(null);
  const [dictOpen, setDictOpen] = useState(false);
  // F5 分支：从任一版本派生新草稿
  const [branchOpen, setBranchOpen] = useState(false);
  const [branchLabel, setBranchLabel] = useState('');
  // F5 对比/合并
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffBase, setDiffBase] = useState('');
  const [diffSource, setDiffSource] = useState('');
  const [diffSelected, setDiffSelected] = useState<string[]>([]);
  const { error, setError, dismiss: dismissError } = useError();
  const saveTimer = useRef<number | null>(null);
  // 保存流水线状态：未保存 / 保存中 / 已保存 / 保存失败。确认与改写必须先过保存屏障。
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  // 串行保存链：同一时刻只允许一个 PATCH 在飞，后续保存排在其后执行。
  // 并发保存会让先发的请求带着旧文本重放 409，把后一次编辑反向覆盖（P0-1）。
  const saveChain = useRef<Promise<boolean>>(Promise.resolve(true));
  const savingRef = useRef(false);
  // 有未落盘编辑：由 applyTurns 置位，保存成功且期间无新编辑时清除。
  const dirtyRef = useRef(false);

  // 当前查看版本：默认当前草稿
  const revision: ScriptRevision | undefined = useMemo(() => {
    const revs = proj?.scriptRevisions ?? [];
    return revs.find((r) => r.id === (viewRevId ?? proj?.currentDraftRevision)) ?? revs[revs.length - 1];
  }, [proj, viewRevId]);

  // 版本切换 → 载入话轮
  useEffect(() => {
    if (revision) {
      const loaded = revision.turns.map((t) => ({ ...t, lines: t.lines.map((l) => ({ ...l })) }));
      turnsRef.current = loaded;
      setTurns(loaded);
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

  // 自动保存（2s 防抖，01 §4.2）。
  // 关键 1：防抖回调读最新工程快照（qc.getQueryData），不用闭包捕获的旧 proj.revision，
  // 否则「编辑 → 立刻确认」时保存会用过期 revision 发 PATCH，触发 409 后确认再以另一个旧
  // revision 提交，双写竞态导致丢编辑或锁死确认按钮。
  // 关键 2：baseRevId 是正在编辑的版本（11 报告 P0-1：查看历史版本时编辑 → 新建草稿，
  // 绝不覆盖当前草稿内容）。
  const doSave = (): Promise<boolean> => {
    const run = (): Promise<boolean> => {
      const latest = qc.getQueryData<Project>(['project', projectId]);
      if (!latest) return Promise.resolve(false);
      // 关键：快照取保存“执行时”的最新话轮，而不是调度时捕获的旧数组。
      // 串行链里排队的保存因此永远写最新内容，不会再出现旧文本覆盖新编辑。
      const snapshot = turnsRef.current;
      const { patch, revId } = upsertDraftTurns(latest, snapshot, viewRevId ?? undefined);
      savingRef.current = true;
      setSaveState('saving');
      return saveDraft
        .mutateAsync({ projectId, patch, expectedRevision: latest.revision })
        .then(() => {
          setSaveState('saved');
          // 保存期间无新编辑才算干净；有的话保持 dirty，等下一次保存。
          if (turnsRef.current === snapshot) dirtyRef.current = false;
          // 查看历史版本时编辑会派生新草稿：保存成功后再切视图，避免新版本尚未进缓存时回退旧稿。
          if (turnsRef.current === snapshot) setViewRevId(revId);
          return true;
        })
        .catch((e: unknown) => {
          setSaveState('error');
          setError(`自动保存失败：${(e as Error).message}（可再次编辑重试，或刷新页面核对）`);
          return false;
        })
        .finally(() => { savingRef.current = false; });
    };
    const next = saveChain.current.then(run, run);
    saveChain.current = next.catch(() => false);
    return next;
  };

  const scheduleSave = () => {
    if (!proj) return;
    dirtyRef.current = true;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { saveTimer.current = null; void doSave(); }, 2000);
  };

  /** 保存屏障：确认 / 改写 / 重新生成前把挂起编辑落盘，并等待真实结果。 */
  const flushDraft = (): Promise<boolean> => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    if (!dirtyRef.current && !savingRef.current) return Promise.resolve(true);
    return doSave();
  };

  /** 一律基于 turnsRef.current（最新状态）计算下一状态；同一批次内连续操作不再互相覆盖。 */
  const applyTurns = (updater: (prev: Turn[]) => Turn[]) => {
    const next = updater(turnsRef.current);
    turnsRef.current = next;
    setTurns(next);
    scheduleSave();
  };

  // ---- 话轮操作 ----
  const selectTurn = (id: string, additive: boolean) => {
    setSelected((s) => (additive
      ? (s.includes(id) ? s.filter((x) => x !== id) : [...s, id])
      : (s.length === 1 && s[0] === id ? [] : [id])));
  };

  const updateTurn = (id: string, patch: Partial<Turn>) => {
    applyTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  };

  const moveTurn = (id: string, dir: -1 | 1) => {
    applyTurns((prev) => {
      const i = prev.findIndex((t) => t.id === id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  };

  const addTurn = () => {
    applyTurns((prev) => {
      const n = prev.length + 1;
      const id = `T${String(Math.max(1, ...prev.map((t) => Number(t.id.replace(/\D/g, '')) || 0)) + 1).padStart(2, '0')}`;
      // 双人对话默认交替：新话轮接在最后一条的对方（此前永远默认 A，双人要手动切换）
      const speaker: Turn['speaker'] = prev[prev.length - 1]?.speaker === 'A' ? 'B' : 'A';
      return [...prev, {
        id, speaker, intent: 'other', tone: '自然', speedRatio: 1.0,
        lines: [{ id: `L${String(n * 10).padStart(3, '0')}`, displayText: '', spokenText: '' }],
      }];
    });
  };

  /** 拆分长独白：优先在句子边界把单行话轮切成两条；多行话轮按行分界切。 */
  const splitTurn = (id: string) => {
    applyTurns((prev) => {
      const i = prev.findIndex((t) => t.id === id);
      if (i < 0) return prev;
      const src = prev[i];
      const full = turnText(src);
      if (!full) return prev;
      let headLines: Turn['lines'];
      let tailLines: Turn['lines'];
      if (src.lines.length === 1) {
        const l = src.lines[0];
        const cut = pickSplitOffset(l.displayText);
        if (cut <= 0 || cut >= l.displayText.length) return prev;
        const maxLine = Math.max(0, ...prev.flatMap((t) => t.lines.map((x) => Number(x.id.replace(/\D/g, '')) || 0)));
        const newLineId = `L${String(maxLine + 1).padStart(3, '0')}`;
        const spokenCut = l.spokenText.length === l.displayText.length ? cut : Math.round(cut * (l.spokenText.length / l.displayText.length));
        headLines = [{ ...l, displayText: l.displayText.slice(0, cut), spokenText: l.spokenText.slice(0, spokenCut) }];
        tailLines = [{ ...l, id: newLineId, displayText: l.displayText.slice(cut), spokenText: l.spokenText.slice(spokenCut) }];
      } else {
        const half = full.length / 2;
        let k = 1;
        let acc = 0;
        for (; k < src.lines.length; k++) {
          acc += src.lines[k - 1].displayText.length;
          if (acc >= half) break;
        }
        if (k >= src.lines.length) return prev;
        headLines = src.lines.slice(0, k);
        tailLines = src.lines.slice(k);
      }
      const newId = `T${String(Math.max(1, ...prev.map((t) => Number(t.id.replace(/\D/g, '')) || 0)) + 1).padStart(2, '0')}`;
      const next = [...prev];
      next.splice(i + 1, 0, { ...src, id: newId, lines: tailLines });
      next[i] = { ...src, lines: headLines };
      return next;
    });
  };

  const removeTurns = (ids: string[]) => {
    applyTurns((prev) => prev.filter((t) => !ids.includes(t.id)));
    setSelected((s) => s.filter((x) => !ids.includes(x)));
  };

  const handleRewrite = (mode: 'rewrite' | 'casual' | 'probe' | 'dedupe' | 'trim') => {
    if (!proj || !revision || selected.length === 0) return;
    setCandidate(null);
    // 改写基于 revisionId 冻结输入，先把挂起编辑保存掉，避免改写结果落后于所见。
    flushDraft().then((ok) => {
      if (!ok) return;
      rewrite.mutate({
        projectId, clientToken: crypto.randomUUID(), revisionId: revision.id,
        turnIds: selected, mode,
      }, {
        onError: (e) => setError((e as Error).message),
      });
    });
  };

  const acceptCandidate = () => {
    if (!candidate) return;
    const byId = new Map(candidate.turns.map((t) => [t.id, t]));
    applyTurns((prev) => prev.map((t) => byId.get(t.id) ?? t));
    setCandidate(null);
  };

  const handleConfirm = () => {
    if (!proj || !revision) return;
    // 确认只对当前草稿开放（11 报告 P1-6：确认历史版本不会推进阶段，两条口径会分裂）。
    // 确认屏障：先把挂起的编辑保存进草稿版本，再以最新 revision 发确认（避免 expectedRevision 落后必 409）。
    if (!viewIsDraft) return;
    flushDraft().then((ok) => {
      // 保存失败时不得用旧文本确认：确认会绑定服务端当前草稿，与界面所见不一致。
      if (!ok) { setError('保存未完成，已取消确认：请检查上方保存状态后重试。'); return; }
      const latest = qc.getQueryData<Project>(['project', projectId]) ?? proj;
      confirm.mutate({
        projectId, expectedRevision: latest.revision, kind: 'script', inputRevisionId: revision.id,
      }, {
        onError: (e) => setError((e as Error).message),
      });
    });
  };

  const branchScript = useBranchScript();
  const patchProject = usePatchProject();
  const mergeScript = useMergeScript();
  const diff = useScriptDiff(projectId, diffBase, diffSource, diffOpen && !!diffBase && !!diffSource);

  // 打开对比面板时：基准=当前草稿，对比=正在查看的版本；两者一致则自动挑一个真正有差异的版本。
  const openDiff = () => {
    const latest = proj?.scriptRevisions ?? [];
    const level = (r: ScriptRevision) =>
      r.turns.map((t) => `${t.id}:${t.speaker}:${t.lines.map((l) => l.displayText).join("\n")}`).join('|');
    const base = proj?.currentDraftRevision ?? latest[latest.length - 1]?.id ?? '';
    const baseRev = latest.find((r) => r.id === base);
    const viewed = revision?.id && revision.id !== base ? revision.id : '';
    const viewedRev = latest.find((r) => r.id === viewed);
    const differs = (r?: ScriptRevision) => !!r && !!baseRev && level(r) !== level(baseRev);
    const differing = latest.filter((r) => r.id !== base && differs(r));
    const source = differs(viewedRev) ? viewed : (differing.slice(-1)[0]?.id ?? base);
    setDiffBase(base);
    setDiffSource(source);
    setDiffSelected([]);
    setDiffOpen(true);
  };

  useEffect(() => {
    if (!diff.data) return;
    // 默认勾选「改写 + 新增」（行级用 turn:line，整段用 turn:id）；删除类需手动勾选，避免误删
    setDiffSelected([
      ...diff.data.turns.filter((t) => t.status === 'added').map((t) => `turn:${t.turnId}`),
      ...diff.data.lines.filter((l) => l.status !== 'removed').map((l) => `${l.turnId}:${l.lineId}`),
    ]);
  }, [diff.data]);

  const handleMerge = () => {
    if (!diffBase || !diffSource) return;
    const turnIds = diffSelected.filter((t) => t.startsWith('turn:')).map((t) => t.slice(5));
    const lineIds = diffSelected.filter((t) => !t.startsWith('turn:'));
    mergeScript.mutate(
      { projectId, baseRevisionId: diffBase, sourceRevisionId: diffSource, turnIds, lineIds,
        label: `合并自 ${diffSource}` },
      {
        onSuccess: () => { setDiffOpen(false); setViewRevId(null); },
        onError: (e) => setError(`合并失败：${(e as Error).message}`),
      },
    );
  };

  /** F5：从当前查看的版本建分支；先落盘挂起编辑，避免复制到旧内容。 */
  const handleBranch = () => {
    if (!revision) return;
    flushDraft().then((ok) => {
      if (!ok) { setError('保存未完成，已取消建分支：请检查上方保存状态后重试。'); return; }
      branchScript.mutate(
        { projectId, fromRevisionId: revision.id, label: branchLabel.trim() },
        {
          onSuccess: () => { setBranchOpen(false); setBranchLabel(''); setViewRevId(null); },
          onError: (e) => setError(`建分支失败：${(e as Error).message}`),
        },
      );
    });
  };

  /** 把正在查看的历史版本设为当前草稿（分支之间切换）。 */
  const handleMakeDraft = () => {
    if (!proj || !revision || viewIsDraft) return;
    patchProject.mutate(
      { projectId, patch: { currentDraftRevision: revision.id }, expectedRevision: proj.revision },
      {
        onSuccess: () => setViewRevId(null),
        onError: (e) => setError(`切换草稿失败：${(e as Error).message}`),
      },
    );
  };

  const handleRegenerate = () => {
    if (!proj) return;
    if (regenerate.isPending) return;
    // 先把挂起编辑落盘并停掉 2s 定时器，再发起重生成：
    // 否则旧定时器会在新脚本落库后把旧 turns 覆盖回去（P0-3）。
    flushDraft().then((ok) => {
      if (!ok) { setError('保存未完成，已取消重新生成：请检查上方保存状态后重试。'); return; }
      const src = revision?.sourceInput ?? { kind: 'article', content: '' };
      regenerate.mutate({
        projectId, clientToken: crypto.randomUUID(),
        sourceInput: { kind: src.kind, content: src.content ?? '' },
        brief: { aspect: proj.aspect },
      }, { onError: (e) => setError((e as Error).message) });
    });
  };

  // ---- 派生 ----
  const saving = saveState === 'saving';
  const scriptApproved = proj?.approvals.some(
    (a) => a.kind === 'script' && a.decision === 'accepted' && !a.revokedAt && revision && a.inputRevisionId === revision.id,
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

  /** 底部导航先过保存屏障：2s 防抖窗口内离开会让下游读到旧草稿（甚至合成旧文案）。 */
  const navigateAfterFlush = (view: 'create' | 'voice') => {
    flushDraft().then((ok) => {
      if (ok || saveState !== 'error') setView(view);
      // 保存失败时停留本页，错误提示已由 error Alert 承担
    });
  };

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
                <p className="wu-caption" style={{ marginTop: 8 }}>
                  读法见<a className="hf-link" onClick={() => setDictOpen(true)}>读音词典</a>。
                </p>
              </>
            ) : (
              <p className="wu-caption">点击话轮查看属性；Ctrl/⌘ 多选。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>质量提示</h4>
            {hints.length === 0 ? (
              <p className="wu-caption">无异常。</p>
            ) : hints.map((h, i) => (
              <div className="hf-qa" key={i} style={{ marginBottom: 8 }}>
                <span className="q">
                  <b>{h.title}</b>
                  <br />
                  {h.kind === 'ai' && <span className="hf-ai" style={{ margin: '4px 0', display: 'inline-flex' }}>AI 建议</span>}
                  {' '}{h.detail}
                </span>
                {h.title === '连续独白较长' && h.turnId && (
                  <Button variant="ghost" size="sm" onClick={() => splitTurn(h.turnId!)}>拆分</Button>
                )}
              </div>
            ))}
            {pronCount > 0 && (
              <div className="hf-qa" style={{ marginTop: 8 }}>
                <span className="q"><b>读音：{pronCount} 处</b><br />可在读音词典中维护</span>
              </div>
            )}
          </div>
        </aside>
      }
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => navigateAfterFlush('create')}>上一步</Button>
            <Badge tone="warning" icon="alert">确认点 · 脚本确认</Badge>
            <span className="hf-spacer" />
            <Button size="sm" disabled={!revision || saving} onClick={() => navigateAfterFlush('voice')}>下一步<Icon name="arrowRight" size={14} /></Button>
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
        <Button variant="secondary" size="sm" icon="refresh" disabled={generating || regenerate.isPending} onClick={handleRegenerate}>重新生成</Button>
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
              <option key={r.id} value={r.id}>
                {r.id}{r.label ? ` · ${r.label}` : ''}{r.id === proj?.currentDraftRevision ? ' · 草稿' : ''}
              </option>
            ))}
          </select>
        </label>
        <Button variant="secondary" size="sm" icon="plus" disabled={!revision || saving} onClick={() => { setBranchLabel(''); setBranchOpen(true); }}>建分支</Button>
        <Button variant="secondary" size="sm" disabled={!proj || (proj.scriptRevisions?.length ?? 0) < 2} onClick={openDiff}>版本对比</Button>
        {!viewIsDraft && (
          <Button variant="secondary" size="sm" disabled={patchProject.isPending} onClick={handleMakeDraft}>设为当前草稿</Button>
        )}
        <Button
          variant="brand" size="sm" icon="check"
          disabled={!revision || scriptApproved || generating || saving || !viewIsDraft}
          busy={confirm.isPending || saving}
          title={viewIsDraft ? undefined : '仅当前草稿可确认；请在版本选择器切回草稿'}
          onClick={handleConfirm}
        >
          {scriptApproved ? '已确认' : saving ? '保存中…' : '确认脚本'}
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
          {error && <Alert tone="danger" onClose={dismissError}>{error}</Alert>}
          {!viewIsDraft && revision && (
            <Alert tone="warning">正在查看历史版本 {revision.id}（非当前草稿）；编辑将基于该版本创建新草稿。</Alert>
          )}

          {candidate && (
            <div className="hf-cand" style={{ marginBottom: 12 }}>
              <div className="t">
                {candidate.turnIds.length === 0
                  ? '候选改写 · 未产生差异'
                  : `候选改写 · 差异：共改 ${candidate.turnIds.length} 处`}
                <span className="hf-ai" style={{ marginLeft: 8 }}>AI 建议</span>
              </div>
              {candidate.turns.filter((t) => candidate.turnIds.includes(t.id)).map((t) => (
                <p key={t.id}><b style={{ fontFamily: 'var(--wu-global-mono)' }}>{t.id}</b> {turnText(t)}</p>
              ))}
              <div className="wu-row" style={{ gap: 8 }}>
              {/* 空差异没有可接受的内容，只保留「拒绝」关闭候选（此前 0 处也弹「接受」） */}
                <Button
                  variant="brand" size="sm" icon="check" disabled={candidate.turnIds.length === 0}
                  title={candidate.turnIds.length === 0 ? '本次改写没有产生任何文本变化' : undefined}
                  onClick={acceptCandidate}
                >接受</Button>
                <Button variant="ghost" size="sm" onClick={() => setCandidate(null)}>拒绝</Button>
                <span className="wu-caption">
                  {candidate.turnIds.length === 0 ? '可换一个改写意图，或手动编辑话轮' : '其他发言不自动重写'}
                </span>
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
                    applyTurns((prev) => {
                      const src = prev.findIndex((x) => x.id === srcId);
                      const dst = prev.findIndex((x) => x.id === t.id);
                      if (src < 0 || dst < 0 || src === dst) return prev;
                      const next = [...prev];
                      const [moved] = next.splice(src, 1);
                      next.splice(dst, 0, moved);
                      return next;
                    });
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
                        setEditingId(null);
                        // 编辑器展示的是整条话轮的合并文本；写回时必须整组替换，
                        // 否则 lines[1..] 会原样保留，保存后变成「新整段 + 旧第二行」重复台词（P0-2）。
                        if (editText === text) return;
                        const first = t.lines[0] ?? { id: `L${t.id.replace(/\D/g, '')}0`, displayText: '', spokenText: '' };
                        updateTurn(t.id, { lines: [{ ...first, displayText: editText, spokenText: editText }] });
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
              <Button variant="secondary" size="sm" icon="edit" onClick={() => setDictOpen(true)}>读音词典</Button>
            </div>
          </div>
        </div>

      </div>

      {/* ---- 底部统计条（01 §4.1） ---- */}
      <div className="hf-stats">
        <span className="hf-pos">话轮 {turns.length}</span>
        <span>A {speakerCount.A} / B {speakerCount.B}</span>
        <span>字数 {charCount.toLocaleString()}</span>
        <span>预估 {formatTimecode(estimateSecs(turns))}（按默认语速估算）</span>
        <span className="hf-spacer" />
        <span className="wu-caption">
          {saveState === 'saving' ? '保存中…'
            : saveState === 'error' ? <span style={{ color: 'var(--wu-semantic-danger)' }}>保存失败 · 再编辑可重试</span>
            : saveState === 'saved' ? '已保存'
            : '自动保存 · 2s 防抖'}
        </span>
      </div>

      {/* ---- 读音词典（01 §4.2 全局面板；工程级/全局级均已持久化） ---- */}
      <DictModal
        open={dictOpen}
        onClose={() => setDictOpen(false)}
        hintPron={pronCount}
        project={proj ?? null}
      />

      {/* ---- 分支（F5）：从当前查看的版本派生新草稿，原版本与确认记录不动 ---- */}
      <Modal
        open={branchOpen}
        onClose={() => setBranchOpen(false)}
        title={revision ? `从 ${revision.id} 新建分支` : '新建分支'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setBranchOpen(false)}>取消</Button>
            <Button variant="brand" busy={branchScript.isPending} onClick={handleBranch}>建立分支</Button>
          </>
        }
      >
        <div style={{ display: 'grid', gap: 10 }}>
          <p className="wu-caption" style={{ margin: 0 }}>
            会复制当前查看版本的全部话轮，生成一个新草稿并立即切换过去；原版本、已用版本与确认记录都保持不变。
          </p>
          <Input
            aria-label="分支名称"
            placeholder="如：开场更活泼版"
            value={branchLabel}
            maxLength={40}
            onChange={(e) => setBranchLabel(e.target.value)}
          />
          {branchScript.error && (
            <Alert tone="danger">建分支失败：{(branchScript.error as Error).message}</Alert>
          )}
        </div>
      </Modal>

      {/* ---- 版本对比与合并（F5）：两种方案之间按话轮取舍 ---- */}
      <Modal
        open={diffOpen}
        onClose={() => setDiffOpen(false)}
        title="版本对比与合并"
        footer={
          <>
            <span className="wu-caption">已选 {diffSelected.length} 项</span>
            <span className="hf-spacer" />
            <Button variant="ghost" onClick={() => setDiffOpen(false)}>取消</Button>
            <Button variant="brand" busy={mergeScript.isPending} disabled={!diffSelected.length} onClick={handleMerge}>
              合并为新草稿
            </Button>
          </>
        }
      >
        <div style={{ display: 'grid', gap: 10 }}>
          <div className="wu-row" style={{ gap: 8 }}>
            <label className="wu-label" htmlFor="diff-base">基准</label>
            <select id="diff-base" className="wu-input" value={diffBase} onChange={(e) => setDiffBase(e.target.value)}>
              {(proj?.scriptRevisions ?? []).map((r) => (
                <option key={r.id} value={r.id}>{r.id}{r.label ? ` · ${r.label}` : ''}</option>
              ))}
            </select>
            <label className="wu-label" htmlFor="diff-source">对比</label>
            <select id="diff-source" className="wu-input" value={diffSource} onChange={(e) => setDiffSource(e.target.value)}>
              {(proj?.scriptRevisions ?? []).map((r) => (
                <option key={r.id} value={r.id}>{r.id}{r.label ? ` · ${r.label}` : ''}</option>
              ))}
            </select>
          </div>
          {diff.isError && <Alert tone="danger">读取差异失败：{(diff.error as Error).message}</Alert>}
          {diff.data && diff.data.turns.length === 0 && diff.data.lines.length === 0 && (
            <p className="wu-caption">两个版本内容一致。</p>
          )}
          <div style={{ display: 'grid', gap: 6, maxHeight: 300, overflow: 'auto' }}>
            {diff.data?.turns.map((t) => {
              const token = `turn:${t.turnId}`;
              return (
                <label key={token} className="wu-row" style={{ alignItems: 'flex-start', gap: 8, padding: '6px 8px', border: '1px solid var(--wu-semantic-border)', borderRadius: 8 }}>
                  <input
                    type="checkbox"
                    checked={diffSelected.includes(token)}
                    onChange={(e) => setDiffSelected((prev) => e.target.checked ? [...prev, token] : prev.filter((x) => x !== token))}
                  />
                  <span style={{ display: 'grid', gap: 2 }}>
                    <span>
                      <Badge tone={t.status === 'added' ? 'success' : t.status === 'removed' ? 'danger' : 'warning'}>
                        {t.status === 'added' ? '整段新增' : t.status === 'removed' ? '整段删除' : '说话人变化'}
                      </Badge>{' '}
                      <span className="hf-mono">{t.turnId}</span> · {t.speaker}
                    </span>
                    {t.text && <span className="wu-caption">{t.text.slice(0, 80)}</span>}
                  </span>
                </label>
              );
            })}
            {diff.data?.lines.length ? (
              <span className="wu-caption" style={{ marginTop: 4 }}>行级差异（同一段里改不同句子可分别勾选）</span>
            ) : null}
            {diff.data?.lines.map((c) => {
              const token = `${c.turnId}:${c.lineId}`;
              return (
                <label key={token} className="wu-row" style={{ alignItems: 'flex-start', gap: 8, padding: '6px 8px', border: '1px solid var(--wu-semantic-border)', borderRadius: 8 }}>
                  <input
                    type="checkbox"
                    checked={diffSelected.includes(token)}
                    onChange={(e) => setDiffSelected((prev) => e.target.checked ? [...prev, token] : prev.filter((x) => x !== token))}
                  />
                  <span style={{ display: 'grid', gap: 2 }}>
                    <span>
                      <Badge tone={c.status === 'added' ? 'success' : c.status === 'removed' ? 'danger' : 'warning'}>
                        {c.status === 'added' ? '新增' : c.status === 'removed' ? '删除' : '改写'}
                      </Badge>{' '}
                      <span className="hf-mono">{c.turnId} / {c.lineId}</span> · {c.speaker}
                    </span>
                    {c.before && <span className="wu-caption">原：{c.before.slice(0, 60)}</span>}
                    {c.after && <span className="wu-caption">新：{c.after.slice(0, 60)}</span>}
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      </Modal>
    </AppShell>
  );
}

function DictModal({ open, onClose, hintPron, project }: {
  open: boolean; onClose: () => void; hintPron: number; project: Project | null;
}) {
  const machine = useMachine();
  const saveMachine = useSaveMachine();
  const saveDraft = useSaveDraft();
  const [term, setTerm] = useState('');
  const [read, setRead] = useState('');
  const [scope, setScope] = useState<'全局' | '本工程'>('本工程');
  const [error, setError] = useState<string | null>(null);

  const globalEntries: PronunciationEntry[] = machine.data?.pronunciationDict ?? [];
  const projectEntries: PronunciationEntry[] = project?.pronunciationDict ?? [];

  const saveProjectEntries = (next: PronunciationEntry[]) => {
    if (!project) return;
    saveDraft.mutate(
      { projectId: project.id, patch: { pronunciationDict: next }, expectedRevision: project.revision },
      { onError: (e) => setError(`保存本工程词典失败：${(e as Error).message}`) },
    );
  };
  const saveGlobalEntries = (next: PronunciationEntry[]) => {
    saveMachine.mutate(
      { pronunciationDict: next },
      { onError: (e) => setError(`保存全局词典失败：${(e as Error).message}`) },
    );
  };

  const addEntry = () => {
    setError(null);
    if (!term.trim() || !read.trim()) return;
    if ([...globalEntries, ...projectEntries].some((e) => e.term === term.trim())) {
      setError(`词条「${term.trim()}」已存在，请先删除原条目再修改。`);
      return;
    }
    const entry = { term: term.trim(), read: read.trim() };
    if (scope === '全局') saveGlobalEntries([...globalEntries, entry]);
    else saveProjectEntries([...projectEntries, entry]);
    setTerm('');
    setRead('');
  };
  const removeEntry = (entryScope: '全局' | '本工程', entryTerm: string) => {
    setError(null);
    if (entryScope === '全局') saveGlobalEntries(globalEntries.filter((e) => e.term !== entryTerm));
    else saveProjectEntries(projectEntries.filter((e) => e.term !== entryTerm));
  };

  return (
    <Modal
      open={open} onClose={onClose} title="读音词典" width={560}
      footer={
        <div className="wu-row" style={{ gap: 8 }}>
          <span className="wu-caption">词条在提交配音时冻结进任务快照，合成前按顺序替换</span>
          <span className="hf-spacer" />
          <Button size="sm" onClick={onClose}>完成</Button>
        </div>
      }
    >
      <div className="hf-dict-row" style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr auto', gap: 8 }}>
        <Input placeholder="词条（如 3:12 / KV）" value={term} onChange={(e) => setTerm(e.target.value)} aria-label="词条" />
        <Input placeholder="读法（如 三分十二秒）" value={read} onChange={(e) => setRead(e.target.value)} aria-label="读法" />
        <Button variant="secondary" size="sm" disabled={!term.trim() || !read.trim()} onClick={addEntry}>新增</Button>
      </div>
      <div className="wu-row" style={{ gap: 14, margin: '8px 0 4px' }}>
        <Choice type="radio" name="dict-scope" checked={scope === '本工程'} onChange={() => setScope('本工程')}
          label={<span>存入本工程{project ? `（${project.id}）` : ''}</span>} />
        <Choice type="radio" name="dict-scope" checked={scope === '全局'} onChange={() => setScope('全局')}
          label="存入全局（所有工程生效）" />
      </div>
      {error && <Alert tone="danger">{error}</Alert>}
      {hintPron > 0 && <p className="wu-caption">当前草稿检出读音提示 {hintPron} 处，可在上方维护后由合成阶段读取。</p>}

      <div style={{ marginTop: 8 }}>
        <div className="hf-dict-row" style={{ fontWeight: 600 }}>
          <span className="term">词条</span>
          <span className="read">读法</span>
          <span className="hf-spacer" />
          <span className="wu-caption">范围</span>
        </div>
        {[...projectEntries.map((e) => ({ ...e, scope: '本工程' as const })),
          ...globalEntries.map((e) => ({ ...e, scope: '全局' as const }))].map((r) => (
          <div className="hf-dict-row" key={`${r.scope}-${r.term}`}>
            <span className="term">{r.term}</span>
            <span className="read">{r.read}</span>
            <span className="hf-spacer" />
            <span className="wu-caption">{r.scope}</span>
            <Button variant="ghost" size="sm" aria-label={`删除 ${r.term}`} onClick={() => removeEntry(r.scope, r.term)}>
              删除
            </Button>
          </div>
        ))}
        {projectEntries.length === 0 && globalEntries.length === 0 && (
          <p className="wu-caption">还没有词条。新增后立即保存：本工程词条随工程一起保存，全局词条对所有工程生效。</p>
        )}
      </div>
    </Modal>
  );
}
