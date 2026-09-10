/* ============================================================================
   HF-08 · 任务中心（真实数据流，02 §7.1 状态机 / 05 §4.3）
   左列：运行中 / 排队中 / 已结束 三类任务列表；右侧：所选任务详情 + 暂停/取消
   ========================================================================== */
import { useMemo, useState } from 'react';
import { useJobPause, useJobCancel, useJobs } from '../lib/api';
import { JOB_LABEL, type Job, type JobStatus } from '../lib/types';
import { Badge, Button, Progress, type Tone } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const RUNNING: JobStatus[] = ['running', 'pause_requested', 'paused', 'recovering', 'waiting_confirmation'];
const QUEUED: JobStatus[] = ['queued'];
const FINISHED: JobStatus[] = ['succeeded', 'failed', 'cancelled', 'cancel_requested'];

const TAB_META = [
  { id: 'running', label: '运行中' },
  { id: 'queued', label: '排队中' },
  { id: 'finished', label: '已结束' },
] as const;
type TabId = (typeof TAB_META)[number]['id'];

function jobTone(j: Job): Tone {
  if (j.status === 'succeeded') return 'success';
  if (j.status === 'failed' || j.status === 'cancel_requested' || j.status === 'cancelled') return 'danger';
  if (j.status === 'queued' || j.status === 'waiting_confirmation') return 'warning';
  return 'info';
}

function kindLabel(kind: string): string {
  const map: Record<string, string> = {
    'script.generate': '脚本生成', 'script.rewrite': '脚本改写',
    'tts.synthesize': '配音合成', 'visual.generate': '画面生成', renders: '视频渲染',
  };
  return map[kind] ?? kind;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

const STAGE_PROGRESS: Record<Job['stage'], number> = {
  prepare: 20, infer: 50, download: 70, verify: 85, post: 95,
};

export function TasksPage() {
  const jobs = useJobs();
  const pause = useJobPause();
  const cancel = useJobCancel();
  const setView = useProjectStore((s) => s.setView);
  const [tab, setTab] = useState<TabId>('running');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const all = jobs.data ?? [];
  const groups = useMemo(() => ({
    running: all.filter((j) => RUNNING.includes(j.status)),
    queued: all.filter((j) => QUEUED.includes(j.status)),
    finished: all.filter((j) => FINISHED.includes(j.status)),
  }), [all]);

  const selected = all.find((j) => j.id === selectedId) ?? groups[tab][0] ?? null;

  const canPause = selected != null && ['running', 'recovering', 'waiting_confirmation'].includes(selected.status);
  const canCancel = selected != null && ['queued', 'running', 'pause_requested', 'paused', 'recovering', 'waiting_confirmation'].includes(selected.status);

  const handlePause = () => { if (selected) pause.mutate(selected.id); };
  const handleCancel = () => { if (selected) cancel.mutate(selected.id); };

  return (
    <AppShell
      project={null}
      onNavigate={setView}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Badge tone="info">任务状态机</Badge>
            <span className="wu-caption">queued → running → succeeded / failed / cancelled；暂停为协作式</span>
            <span className="hf-spacer" />
            <span className="wu-caption">共 {all.length} 项 · SSE 实时推送</span>
          </span>
        }
      />
      }
    >
      <StageHead title="任务中心">
        <span className="hf-seg" role="group" aria-label="任务分类" style={{ height: 30 }}>
          {TAB_META.map((t) => (
            <button
              key={t.id} type="button" aria-pressed={tab === t.id}
              style={{ width: 'auto', padding: '0 14px' }}
              onClick={() => { setTab(t.id); setSelectedId(null); }}
            >{t.label} · {groups[t.id].length}</button>
          ))}
        </span>
        <span className="hf-spacer" />
        <span className="wu-caption">GPU / API / CPU 三队列；长任务可暂停与取消</span>
      </StageHead>

      <div className="hf-body" style={{ minHeight: 0 }}>
        <div className="hf-task-layout">
          {/* ---- 左列：任务列表 ---- */}
          <div className="hf-task-col">
            {groups[tab].length === 0 && (
              <div className="hf-task-row" style={{ cursor: 'default' }}>暂无{tab === 'running' ? '运行中' : tab === 'queued' ? '排队中' : '已结束'}任务</div>
            )}
            {groups[tab].map((j) => (
              <div
                key={j.id}
                className={`hf-task-row ${selected?.id === j.id ? 'active' : ''}`}
                onClick={() => setSelectedId(j.id)}
              >
                <Badge tone={jobTone(j)}>{JOB_LABEL[j.status]}</Badge>
                <span className="kind">{kindLabel(j.kind)}</span>
                <span className="hf-spacer" />
                <span className="hf-mono" style={{ fontSize: 10.5 }}>{j.queueClass}</span>
              </div>
            ))}
          </div>

          {/* ---- 右列：任务详情 ---- */}
          <div className="hf-panel">
            {selected ? (
              <div className="hf-svc-card">
                <div className="head">
                  <span className="hf-mono" style={{ fontWeight: 700 }}>{selected.id}</span>
                  <Badge tone={jobTone(selected)}>{JOB_LABEL[selected.status]}</Badge>
                  <span className="hf-spacer" />
                  <span className="hf-mono" style={{ fontSize: 11 }}>{selected.clientToken}</span>
                </div>
                <div className="hf-detail-grid">
                  <div className="hf-kv"><b>类型</b><span>{kindLabel(selected.kind)} <span className="hf-mono">({selected.kind})</span></span></div>
                  <div className="hf-kv"><b>队列</b><Badge tone="muted">{selected.queueClass}</Badge></div>
                  <div className="hf-kv"><b>工程</b><span className="hf-mono">{selected.projectId}</span></div>
                  <div className="hf-kv"><b>阶段</b><span>{selected.stage} · 第 {selected.attempt + 1} 次尝试</span></div>
                  <div className="hf-kv"><b>创建时间</b><span>{fmtTime(selected.createdAt)}</span></div>
                </div>

                {['queued', 'running', 'pause_requested', 'paused', 'recovering', 'waiting_confirmation'].includes(selected.status) && (
                  <div style={{ margin: '8px 0' }}>
                    <Progress value={STAGE_PROGRESS[selected.stage]} />
                  </div>
                )}

                {selected.error && (
                  <div className="wu-alert" data-tone="danger">
                    {selected.error}{selected.retryable ? '（可重试）' : ''}
                  </div>
                )}
                {selected.result && (
                  <div className="hf-detail-grid" style={{ marginTop: 4 }}>
                    <div className="hf-kv" style={{ alignItems: 'flex-start' }}><b>结果</b><span style={{ wordBreak: 'break-all' }}>{JSON.stringify(selected.result)}</span></div>
                  </div>
                )}
                {selected.inputSnapshot && (
                  <details style={{ marginTop: 4, fontSize: 12 }}>
                    <summary className="wu-caption" style={{ cursor: 'pointer' }}>输入快照（冻结）</summary>
                    <pre className="hf-mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', background: 'var(--wu-semantic-muted)', padding: 8, borderRadius: 8, marginTop: 6, fontSize: 10.5 }}>
                      {JSON.stringify(selected.inputSnapshot, null, 2)}
                    </pre>
                  </details>
                )}

                <div className="wu-row" style={{ gap: 8, marginTop: 12 }}>
                  <Button variant="secondary" size="sm" icon="pause" disabled={!canPause} busy={pause.isPending} onClick={handlePause}>暂停</Button>
                  <Button variant="ghost" size="sm" icon="close" disabled={!canCancel} busy={cancel.isPending} onClick={handleCancel}>取消</Button>
                  <span className="hf-spacer" />
                  <Button variant="ghost" size="sm" icon="refresh" onClick={() => setSelectedId(null)}>清空选择</Button>
                </div>
              </div>
            ) : (
              <div className="wu-empty">
                <div style={{ fontWeight: 700 }}>选择任务查看详情</div>
                <p className="wu-caption" style={{ margin: '6px auto 14px', maxWidth: 420 }}>任务详情展示状态机、阶段进度与冻结的输入快照。</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}
