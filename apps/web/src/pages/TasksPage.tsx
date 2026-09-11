/* ============================================================================
   HF-08 · 任务中心（真实数据流，02 §7.1 状态机 / 01 §10.1）
   三个分组同屏（运行中 / 排队 / 已结束），任务卡含状态圆点与五阶段语义进度
   检查器：所选任务 / 五阶段进度 / 取消语义 / 连接与一致性 / 输入快照
   ========================================================================== */
import { useMemo, useState } from 'react';
import { useJobPause, useJobResume, useJobCancel, useJobs } from '../lib/api';
import { JOB_LABEL, type Job, type JobStatus } from '../lib/types';
import { Badge, Button } from '../components/wu';
import { AppShell } from '../components/AppShell';
import { useProjectStore } from '../stores';

const RUNNING: JobStatus[] = ['running', 'pause_requested', 'paused', 'recovering', 'waiting_confirmation', 'cancel_requested', 'unknown'];
const QUEUED: JobStatus[] = ['queued'];
const FINISHED: JobStatus[] = ['succeeded', 'failed', 'cancelled'];

const PHASES: { key: Job['stage']; label: string }[] = [
  { key: 'prepare', label: '准备' },
  { key: 'infer', label: '推理' },
  { key: 'download', label: '下载落盘' },
  { key: 'verify', label: '校验' },
  { key: 'post', label: '后处理' },
];

const PHASE_HINT: Record<Job['stage'], string> = {
  prepare: '输入校验 · 资源预留',
  infer: '引擎内推理',
  download: '产物下载落盘',
  verify: '时长 / 读音一致性',
  post: '对齐 / 打包',
};

function kindLabel(kind: string): string {
  const map: Record<string, string> = {
    'script.generate': '脚本生成', 'script.rewrite': '脚本改写',
    'tts.synthesize': '整期配音', 'visual.generate': '画面生成', renders: '视频渲染',
  };
  return map[kind] ?? kind;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDur(sec: number): string {
  if (sec < 60) return `${Math.max(0, Math.round(sec))}s`;
  const m = Math.floor(sec / 60);
  return `${m}:${String(Math.round(sec % 60)).padStart(2, '0')}`;
}

function elapsedOf(j: Job): number {
  const t = new Date(j.createdAt).getTime();
  return Number.isNaN(t) ? 0 : (Date.now() - t) / 1000;
}

/** 状态圆点语义（01 §10.1） */
function icClass(j: Job): 'run' | 'que' | 'ok' | 'fail' | 'unk' {
  if (j.status === 'succeeded') return 'ok';
  if (j.status === 'failed' || j.status === 'cancelled' || j.status === 'cancel_requested') return 'fail';
  if (j.status === 'queued') return 'que';
  if (j.status === 'pause_requested' || j.status === 'paused' || j.status === 'waiting_confirmation' || j.status === 'recovering') return 'unk';
  return 'run';
}

const IC_GLYPH = { run: '●', que: '○', ok: '✓', fail: '!', unk: '⏸' } as const;

/** 五阶段语义进度：已完成阶段满格，当前阶段给引擎内相对进度，后续空 */
function phaseState(j: Job, idx: number): { cls: string; pct: number } {
  const cur = PHASES.findIndex((p) => p.key === j.stage);
  if (j.status === 'succeeded') return { cls: 'done', pct: 100 };
  if (j.status === 'queued' || j.status === 'cancelled' || j.status === 'cancel_requested') return { cls: '', pct: 0 };
  if (idx < cur) return { cls: 'done', pct: 100 };
  if (idx > cur) return { cls: '', pct: 0 };
  if (j.status === 'failed') return { cls: 'bad', pct: 100 };
  if (j.status === 'paused' || j.status === 'pause_requested') return { cls: 'slow', pct: 55 };
  return { cls: 'run', pct: 42 };
}

export function TasksPage() {
  const jobs = useJobs();
  const pause = useJobPause();
  const resume = useJobResume();
  const cancel = useJobCancel();
  const setView = useProjectStore((s) => s.setView);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState(() => Date.now());

  const all = jobs.data ?? [];
  const groups = useMemo(() => {
    const today = new Date().toDateString();
    return {
      running: all.filter((j) => RUNNING.includes(j.status)),
      queued: all.filter((j) => QUEUED.includes(j.status)),
      finished: all.filter((j) => FINISHED.includes(j.status) && new Date(j.createdAt).toDateString() === today),
    };
  }, [all]);

  const selected = all.find((j) => j.id === selectedId) ?? groups.running[0] ?? groups.queued[0] ?? groups.finished[0] ?? null;

  const canPause = selected != null && ['queued', 'running', 'paused'].includes(selected.status);
  const canCancel = selected != null && ['queued', 'running', 'pause_requested', 'paused', 'waiting_confirmation'].includes(selected.status);

  const handlePause = () => { if (selected) (selected.status === 'paused' ? resume : pause).mutate(selected.id); };
  const handleCancel = () => { if (selected) cancel.mutate(selected.id); };

  const renderTask = (j: Job, inQueue: boolean) => {
    const ic = icClass(j);
    const active = selected?.id === j.id;
    const showPhases = !inQueue && (RUNNING.includes(j.status) || FINISHED.includes(j.status));
    const ok = j.status === 'succeeded';
    return (
      <div
        key={j.id}
        className={`hf-task ${active ? 'selected' : ''}`}
        role="button" tabIndex={0} aria-pressed={active}
        onClick={() => setSelectedId(j.id)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedId(j.id); } }}
      >
        <span className={`hf-task-ic ${ic}`}>{IC_GLYPH[ic]}</span>
        <div className="hf-task-bd">
          <div className="hf-task-t">
            <b>{kindLabel(j.kind)}</b>
            <span className="hf-tag">{j.id}</span>
            <span className="hf-tag">{j.queueClass}</span>
            <span className="hf-task-meta" style={{ margin: 0, marginLeft: 'auto' }}>
              <span>{JOB_LABEL[j.status]}</span>
              {ok && <span>{fmtDur(elapsedOf(j))} · {fmtTime(j.createdAt)} 完成</span>}
              {!ok && RUNNING.includes(j.status) && <span>已 {fmtDur(elapsedOf(j))}</span>}
              {j.error && <span className="err">{j.error}</span>}
            </span>
          </div>

          {showPhases && (
            <>
              <div className="hf-phases">
                {PHASES.map((p, i) => {
                  const st = phaseState(j, i);
                  return <span key={p.key} className={`ph ${st.cls}`}><i style={{ width: `${st.pct}%` }} /></span>;
                })}
              </div>
              <div className="hf-phases-lb">
                {PHASES.map((p) => <span key={p.key}>{p.label}</span>)}
              </div>
            </>
          )}

          {inQueue && (
            <div className="hf-que-note">
              <span className="ph-wait" />
              等待 {j.queueClass} · 第 {j.attempt + 1} 次尝试
            </div>
          )}

          <div className="hf-task-ops">
            {RUNNING.includes(j.status) && (
              <Button variant="secondary" size="sm" disabled={!canPause || !active} onClick={(e) => { e.stopPropagation(); handlePause(); }}>
                {j.status === 'paused' || j.status === 'pause_requested' ? '继续' : '当前段完成后暂停'}
              </Button>
            )}
            {canCancel && (RUNNING.includes(j.status) || inQueue) && (
              <Button variant="ghost" size="sm" disabled={!active} onClick={(e) => { e.stopPropagation(); handleCancel(); }}>
                {inQueue ? '取消排队' : '取消'}
              </Button>
            )}
            {ok && (
              <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); setSelectedId(j.id); }}>查看产物</Button>
            )}
          </div>
        </div>
      </div>
    );
  };

  return (
    <AppShell
      project={null}
      onNavigate={setView}
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>所选任务 <span className="wu-caption" style={{ fontWeight: 400 }}>
              {selected ? `${kindLabel(selected.kind)} ${selected.id}` : '未选'}
            </span></h4>
            {selected ? (
              <div className="hf-ins-rows">
                <div className="hf-fld"><label>类型</label><span>{kindLabel(selected.kind)} · <span className="hf-mono">{selected.kind}</span></span></div>
                <div className="hf-fld"><label>队列</label><span><Badge tone="muted">{selected.queueClass}</Badge></span></div>
                <div className="hf-fld"><label>提交</label><span>{fmtTime(selected.createdAt)} · 第 {selected.attempt + 1} 次尝试</span></div>
                <div className="hf-fld"><label>状态</label><span><Badge tone={selected.status === 'succeeded' ? 'success' : selected.status === 'failed' ? 'danger' : 'info'}>{JOB_LABEL[selected.status]}</Badge></span></div>
                <div className="hf-fld"><label>工程</label><span className="hf-mono">{selected.projectId}</span></div>
              </div>
            ) : (
              <p className="wu-caption">暂无任务。完成任务提交后，此处显示状态机、阶段进度与冻结的输入快照。</p>
            )}
          </div>

          {selected && (
            <div className="hf-ins-sec">
              <h4>五阶段进度 <span className="wu-caption" style={{ fontWeight: 400 }}>节点映射为阶段</span></h4>
              {PHASES.map((p, i) => {
                const st = phaseState(selected, i);
                const cls = st.cls === 'done' ? 'd' : st.cls === 'run' ? 'c' : st.cls === 'slow' || st.cls === 'bad' ? 'w' : 'p';
                const glyph = st.cls === 'done' ? '✓' : st.cls === 'run' ? '●' : st.cls === 'bad' ? '!' : '—';
                return (
                  <div className="hf-ph-row" key={p.key}>
                    <span className={`st ${cls}`}>{glyph}</span>
                    <span className="nm">{p.label}<small>{i === PHASES.findIndex((x) => x.key === selected.stage) ? `${PHASE_HINT[selected.stage]} · ${st.pct}%` : PHASE_HINT[p.key]}</small></span>
                    <span className="val">{st.pct ? `${st.pct}%` : '—'}</span>
                  </div>
                );
              })}
            </div>
          )}

          <div className="hf-ins-sec">
            <h4>取消语义</h4>
            <div className="hf-task-ops" style={{ marginTop: 0 }}>
              <Button variant="secondary" size="sm" disabled={!canPause} busy={pause.isPending || resume.isPending} onClick={handlePause}>{selected?.status === 'paused' ? '继续' : '当前任务完成后暂停'}</Button>
              <Button variant="ghost" size="sm" disabled={!canCancel} busy={cancel.isPending} onClick={handleCancel}>取消</Button>
            </div>
            <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.65 }}>
              「正在取消」与「已取消」分开呈现；没有可验证的定向取消能力时显示「取消待生效」。取消后当前段已落盘内容保留，可续跑。
            </p>
          </div>

          <div className="hf-ins-sec">
            <h4>连接与一致性</h4>
            <p className="wu-caption" style={{ lineHeight: 1.7 }}>
              事件带序号；断线后从服务端<b>读取完整状态</b>（补发或读快照），不依赖浏览器此前收到的进度。任务详情随列表状态实时刷新。
            </p>
          </div>

          {selected?.result && (
            <div className="hf-ins-sec">
              <h4>结果</h4>
              <pre className="hf-mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', background: 'var(--wu-semantic-muted)', padding: 8, borderRadius: 8, margin: 0, fontSize: 10.5 }}>
                {JSON.stringify(selected.result, null, 2)}
              </pre>
            </div>
          )}

          {selected?.inputSnapshot && (
            <div className="hf-ins-sec">
              <details className="hf-adv">
                <summary>输入快照（冻结）</summary>
                <div className="hf-adv-body">
                  <pre className="hf-mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', background: 'var(--wu-semantic-muted)', padding: 8, borderRadius: 8, margin: 0, fontSize: 10.5 }}>
                    {JSON.stringify(selected.inputSnapshot, null, 2)}
                  </pre>
                </div>
              </details>
            </div>
          )}
        </aside>
      }
      /* 独立全屏入口：设计稿 HF-08 底部为正文内 .hf-foot-note，不是 FootBar */
      footer={null}
    >
      <div className="hf-h">
        <h2>任务中心</h2>
        <span className="wu-caption">本地与 API 任务统一视图</span>
        <span className="hf-spacer" />
        <span className="wu-caption">更新于 {new Date(refreshedAt).toLocaleTimeString('zh-CN', { hour12: false })}</span>
        <Button variant="secondary" size="sm" icon="refresh" onClick={() => setRefreshedAt(Date.now())}>刷新</Button>
      </div>

      <div className="hf-body2">
        <section className="hf-grp">
          <div className="hf-grp-t">运行中 <Badge tone="brand">{groups.running.length}</Badge></div>
          {groups.running.map((j) => renderTask(j, false))}
          {groups.running.length === 0 && <p className="wu-caption">暂无运行中任务。</p>}
        </section>

        <section className="hf-grp">
          <div className="hf-grp-t">排队 <Badge tone="muted">{groups.queued.length}</Badge></div>
          {groups.queued.map((j) => renderTask(j, true))}
          {groups.queued.length === 0 && <p className="wu-caption">队列为空。</p>}
        </section>

        <section className="hf-grp">
          <div className="hf-grp-t">已结束（今日） <Badge tone="muted">{groups.finished.length}</Badge></div>
          {groups.finished.map((j) => renderTask(j, false))}
          {groups.finished.length === 0 && <p className="wu-caption">今日暂无已结束任务。</p>}
        </section>

        <div className="hf-foot-note">
          <span>GPU / API / CPU 三队列各自并发上限；暂停为协作式（当前段完成后生效），取消保留已落盘内容。</span>
        </div>
      </div>
    </AppShell>
  );
}
