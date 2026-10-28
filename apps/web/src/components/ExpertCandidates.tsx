import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getJson, sendJson } from '../lib/api';
import { Alert, Button } from './wu';
import { ExpertAssembly } from './ExpertAssembly';

interface Candidate {
  jobId: string; status: string; stage: string; error?: string; stale: boolean;
  plan: { planId: string; selectedTurnIds: string[]; operation: string; displayFrameRange: [number, number]; reviewSampleRange?: [number, number]; sampleRate?: number; reusedWindowCount?:number;generateWindowCount?:number };
  result?: { artifactIds: string[]; simulated?: boolean };
}
interface Selection { revision: number; selected: Record<string, string>; versions: { version: number; selected: Record<string, string> }[] }
interface CandidateData { selection: Selection; candidates: Candidate[] }
const labels: Record<string, string> = { queued: '排队中', running: '生成中', succeeded: '待审看', failed: '失败', cancelled: '已取消', unknown: '状态待核实', cancel_requested: '取消中' };
const reviewChecks = [['sync', '口型与发言同步'], ['stable', '嘴唇与面部稳定'], ['blend', '脸部边缘融合自然'], ['boundary', '前后交接连续']];

export function ExpertCandidates({ projectId, projectRevision }: { projectId: string; projectRevision:number }) {
  const base = `/projects/${encodeURIComponent(projectId)}/expert-video`;
  const query = useQuery({ queryKey: ['expert-candidates', projectId], queryFn: () => getJson<CandidateData>(base+'/candidates'), refetchInterval: 2000 });
  const [checks, setChecks] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [mediaReady, setMediaReady] = useState<Record<string, boolean>>({});
  const [mediaError, setMediaError] = useState<Record<string, boolean>>({});
  const retryTokens = useRef<Record<string, string>>({});
  const players = useRef<Record<string, HTMLVideoElement>>({});
  const retryToken = (jobId: string) => retryTokens.current[jobId] ??= crypto.randomUUID();
  async function action(path: string, body: unknown, message: string, absolute = false) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await sendJson(absolute ? path : base+path, 'POST', body); await query.refetch(); setNotice(message); }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败，请刷新后重试'); }
    finally { setBusy(false); }
  }
  if (query.isError) return <Alert tone="warning">候选状态加载失败。<button onClick={() => void query.refetch()}>重新加载</button></Alert>;
  const data = query.data;
  if (!data?.candidates.length) return <p className="wu-caption">还没有生成候选。检查范围后提交任务，结果将在这里显示。</p>;
  return <section aria-label="专家候选与版本">
    <h3>候选审看与版本恢复</h3>
    <p>采用只更新所涉及窗口；其他已采用窗口保留。当前选择版本：{data.selection.revision}。完整成片仍需单独合成导出。</p>
    {error && <div role="alert"><Alert tone="warning">{error}</Alert></div>}
    {notice && <p role="status">{notice}</p>}
    {data.candidates.map(candidate => {
      const selected = Object.values(data.selection.selected).includes(candidate.jobId);
      const completed = candidate.status === 'succeeded' && candidate.result?.artifactIds.length === 1 && !candidate.result.simulated;
      return <article key={candidate.jobId} style={{ borderTop: '1px solid var(--wu-border, #ddd)', padding: '16px 0' }}>
        <h4>{candidate.plan.operation === 'preview' ? '预览' : candidate.plan.operation==='continue'?'整期续算':'重做'} · {(candidate.plan.displayFrameRange[0]/25).toFixed(2)}–{(candidate.plan.displayFrameRange[1]/25).toFixed(2)} 秒 · {selected ? '包含已采用窗口' : labels[candidate.status] ?? candidate.status}</h4>
        {candidate.stale && <Alert tone="warning">工程已改变，此候选不能采用或恢复。</Alert>}
        {candidate.plan.operation==='continue'&&<p>复用已采用片段 {candidate.plan.reusedWindowCount} 个，补齐 {candidate.plan.generateWindowCount} 个；审看通过后再采用整期。</p>}
        {candidate.error && <Alert tone="warning">{candidate.error}</Alert>}
        {!completed && <p>{candidate.stage}</p>}
        {['queued', 'running'].includes(candidate.status) && <Button variant="secondary" disabled={busy} onClick={() => void action(`/jobs/${candidate.jobId}/cancel`, undefined, '已请求取消，当前采用版保留。', true)}>取消此任务</Button>}
        {['failed', 'cancelled'].includes(candidate.status) && <Button variant="secondary" disabled={busy || candidate.stale} onClick={() => void action('/generate', {planId: candidate.plan.planId, clientToken: retryToken(candidate.jobId)}, '已提交新的候选任务，原记录保留。')}>新建重试任务</Button>}
        {completed && <>
          {candidate.plan.reviewSampleRange && candidate.plan.sampleRate && <p>候选播放含交接窗口：{(candidate.plan.reviewSampleRange[0]/candidate.plan.sampleRate).toFixed(2)}–{(candidate.plan.reviewSampleRange[1]/candidate.plan.sampleRate).toFixed(2)} 秒。</p>}
          <video controls preload="metadata" style={{ width: '100%', maxWidth: 720 }} src={`/api/artifacts/${encodeURIComponent(candidate.result!.artifactIds[0])}/file`} aria-label={`${candidate.jobId}候选播放`}
            ref={player => { if (player) players.current[candidate.jobId] = player; else delete players.current[candidate.jobId]; }}
            onPlay={() => Object.entries(players.current).forEach(([id, player]) => { if (id !== candidate.jobId) player.pause(); })}
            onLoadedMetadata={() => setMediaReady(previous => ({...previous,[candidate.jobId]:true}))}
            onError={() => { setMediaReady(previous => ({...previous,[candidate.jobId]:false})); setMediaError(previous => ({...previous,[candidate.jobId]:true})); }} />
          {mediaError[candidate.jobId] && <Alert tone="warning">候选视频无法加载，请刷新后重试；当前采用版不受影响。</Alert>}
          <fieldset disabled={busy || candidate.stale}>
            <legend>观看后逐项确认</legend>
            {reviewChecks.map(([key, text]) => <label key={key} style={{ display: 'block' }}>
              <input type="checkbox" checked={(checks[candidate.jobId] ?? []).includes(key)} onChange={e => setChecks(previous => ({ ...previous,
                [candidate.jobId]: e.target.checked ? [...(previous[candidate.jobId] ?? []), key] : (previous[candidate.jobId] ?? []).filter(v => v !== key) }))} /> {text}
            </label>)}
          </fieldset>
          <Button disabled={busy || candidate.stale || !mediaReady[candidate.jobId] || (checks[candidate.jobId]?.length ?? 0) !== 4} onClick={() => void action(`/candidates/${candidate.jobId}/adopt`, { expectedRevision: data.selection.revision, checks: checks[candidate.jobId] }, '已采用此候选涉及的窗口，旧版本已保留。')}>采用此候选</Button>
        </>}
      </article>;
    })}
    <label>恢复已采用的历史版本
      <select aria-label="恢复已采用的历史版本" value="" disabled={busy} onChange={e => { if (e.target.value) void action('/restore', { version: Number(e.target.value), expectedRevision: data.selection.revision }, '已恢复历史窗口选择。'); }}>
        <option value="">请选择历史版本</option>
        {data.selection.versions.filter(v => v.version !== data.selection.revision).map(v => <option key={v.version} value={v.version}>版本 {v.version}</option>)}
      </select>
    </label>
    <ExpertAssembly key={data.selection.revision} projectId={projectId} projectRevision={projectRevision}/>
  </section>;
}
