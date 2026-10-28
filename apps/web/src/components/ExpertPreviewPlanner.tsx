import { useEffect, useId, useRef, useState } from 'react';
import { getJson, sendJson } from '../lib/api';
import type { Project } from '../lib/types';
import { Alert, Button } from './wu';
import { ExpertCandidates } from './ExpertCandidates';

interface PreviewPlan {
  planId: string;
  selectedTurnIds: string[];
  reviewSampleRange?: [number, number];
  sampleRate?: number;
  displayFrameRange: [number, number];
  windows: { windowId: string; reason: string }[];
  turnWindowBindings: Record<string, string[]>;
  executionAvailable: boolean;
  executionStatus?: string;
  stale?: boolean;
  operation: 'preview' | 'regenerate';
}

/** Planning only: never substitutes the legacy whole-episode renderer for a local retry. */
export function ExpertPreviewPlanner({ project }: { project: Project }) {
  const controlId = useId();
  const turns = project.scriptRevisions.find(r => r.id === project.currentDraftRevision)?.turns ?? [];
  const [start, setStart] = useState(0);
  const [operation, setOperation] = useState<'preview' | 'regenerate'>('preview');
  const [result, setResult] = useState<PreviewPlan | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<PreviewPlan[]>([]);
  const [submittedJob, setSubmittedJob] = useState('');
  const requestVersion = useRef(0);
  const identity = JSON.stringify(project);
  useEffect(() => {
    requestVersion.current++;
    setResult(null); setError(''); setBusy(false); setStart(0);
    setHistory([]); setSubmittedJob('');
    const version = requestVersion.current;
    void getJson<PreviewPlan[]>(`/projects/${encodeURIComponent(project.id)}/expert-video/plans`)
      .then(plans => { if (requestVersion.current === version) setHistory(plans); })
      .catch(() => { if (requestVersion.current === version) setError('历史计划加载失败，可重新检查范围。'); });
    return () => { requestVersion.current++; };
  }, [identity]);
  const selected = turns.slice(start, start + (operation === 'preview' ? 4 : 1));
  const ready = !!project.motionPack && !!project.audioTimeline
    && project.audioTimeline.revisionId === project.currentDraftRevision && selected.length > 0;
  const invalidate = () => { requestVersion.current++; setResult(null); setError(''); setBusy(false); };
  async function plan() {
    const version = ++requestVersion.current;
    setBusy(true); setResult(null); setError('');
    try {
      const next = await sendJson<PreviewPlan>(`/projects/${encodeURIComponent(project.id)}/expert-video/${operation === 'preview' ? 'preview-plan' : 'regeneration-plan'}`, 'POST', {
        turnIds: selected.map(t => t.id),
        ...(operation === 'regenerate' ? { candidateToken: crypto.randomUUID() } : {}),
      });
      if (version === requestVersion.current) {
        setResult(next);
        setHistory(previous => [...previous.filter(p => p.planId !== next.planId), next]);
      }
    } catch (e) {
      if (version === requestVersion.current) setError(e instanceof Error ? e.message : '规划失败，请重试');
    } finally { if (version === requestVersion.current) setBusy(false); }
  }
  async function generate() {
    if (!result || busy) return;
    const version = ++requestVersion.current;
    setBusy(true); setError('');
    try {
      const response = await sendJson<{jobId: string}>(`/projects/${encodeURIComponent(project.id)}/expert-video/generate`, 'POST', { planId: result.planId, clientToken: result.planId });
      if (version === requestVersion.current) setSubmittedJob(response.jobId);
    } catch (e) { if (version === requestVersion.current) setError(e instanceof Error ? e.message : '任务提交失败，可重试查询同一任务'); }
    finally { if (version === requestVersion.current) setBusy(false); }
  }
  return <section className="hf-zone" aria-label="专家两轮预览与局部重做规划">
    <div className="hf-zone-t">专家模式 · 两轮预览与单轮重做</div>
    <p className="wu-caption">先检查范围，再提交生成。新候选不会自动替换当前版本；观看并确认后才能采用。</p>
    <div style={{ display: 'grid', gap: 12 }}>
      <label htmlFor={controlId+'-operation'}>操作</label>
        <select id={controlId+'-operation'} className="wu-input" value={operation} onChange={e => { invalidate(); setSubmittedJob(''); setOperation(e.target.value as 'preview' | 'regenerate'); }}>
          <option value="preview">两轮预览（连续四个话轮）</option>
          <option value="regenerate">单轮画面重做（保持配音与时长）</option>
        </select>
      <label htmlFor={controlId+'-turn'}>{operation === 'preview' ? '从哪个话轮开始预览' : '选择需要重做的话轮'}</label>
        <select id={controlId+'-turn'} className="wu-input" disabled={!turns.length} value={start} onChange={e => { invalidate(); setSubmittedJob(''); setStart(Number(e.target.value)); }}>
          {turns.map((t, i) => <option key={t.id} value={i}>{i + 1} · {t.speaker} · {t.lines.map(l => l.displayText).join(' ').slice(0, 60)}</option>)}
        </select>
      <ol>{selected.map(t => <li key={t.id}><strong>{t.speaker}</strong>：{t.lines.map(l => l.displayText).join(' ')}</li>)}</ol>
      {operation === 'preview' && selected.length < 4 && <p>剩余不足四个话轮，将检查以上 {selected.length} 个完整话轮。</p>}
      {!ready && <Alert tone="muted">请先采用动作素材，并准备与当前脚本一致的配音。</Alert>}
      <Button variant="secondary" disabled={!ready || busy} busy={busy} onClick={() => void plan()}>检查生成范围（不生成）</Button>
      {error && <div role="alert"><Alert tone="warning">{error}</Alert></div>}
      {result && <div role="status">
        <p>播放范围：{(result.displayFrameRange[0] / 25).toFixed(2)}–{(result.displayFrameRange[1] / 25).toFixed(2)} 秒；涉及 {result.windows.length} 个模型窗口。</p>
        <p>涉及话轮：{[...new Set(Object.values(result.turnWindowBindings).flat())].map(id => {
          const index = turns.findIndex(t => t.id === id);
          return index < 0 ? id : `${index + 1}（${turns[index].speaker}）`;
        }).join('、')}。{operation === 'regenerate' ? '交接上下文可能要求相邻话轮一并更新。' : '生成时需保留前后上下文。'}</p>
        {result.reviewSampleRange && result.sampleRate && <p>审看范围（含需更新的交接窗口）：{(result.reviewSampleRange[0]/result.sampleRate).toFixed(2)}–{(result.reviewSampleRange[1]/result.sampleRate).toFixed(2)} 秒。</p>}
        <Button disabled={!result.executionAvailable || busy || !!submittedJob} busy={busy} onClick={() => void generate()}>生成此范围的候选</Button>
        {!result.executionAvailable && <p>专家执行器尚未配置，当前只能检查范围。</p>}
        {result.executionStatus && <p className="wu-caption">{result.executionStatus}</p>}
        {submittedJob && <p role="status">任务已提交：{submittedJob}。可在下方查看结果，重复提交不会创建第二个任务。</p>}
      </div>}
      {!!history.length && <details>
        <summary>最近保存的范围计划（最多50条）</summary>
        <p className="wu-caption">范围计划不是生成候选。刷新页面后保留；工程变化后须重新检查。</p>
        <ul>{history.map(p => <li key={p.planId}>
          {p.operation === 'preview' ? '预览' : '重做'} · {(p.displayFrameRange[0] / 25).toFixed(2)}–{(p.displayFrameRange[1] / 25).toFixed(2)} 秒 · {p.stale ? '已过期，请重新检查' : '已保存，尚未生成'}
        </li>)}</ul>
      </details>}
      <ExpertCandidates key={project.id} projectId={project.id} projectRevision={project.revision} />
    </div>
  </section>;
}
