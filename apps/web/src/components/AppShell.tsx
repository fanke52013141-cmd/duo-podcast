/* ============================================================================
   DuoCast 前端 · 应用壳（TopBar + 阶段轨 + 主区 + FootBar）
   对齐 HF-00/HF-01 结构；阶段状态徽章由工程 stageProgress 推导（01 §13.1）
   ========================================================================== */
import React from 'react';
import { STAGE_META, STAGE_ORDER, type AudioTimeline, type Project, type StageState } from '../lib/types';
import { Badge, Icon, type Tone } from './wu';
import { useProjectStore, type View } from '../stores';

const ENTRY_VIEWS: { id: View; label: string; icon: 'folder' | 'gear' | 'task' }[] = [
  { id: 'assets', label: '资产库', icon: 'folder' },
  { id: 'services', label: '服务设置', icon: 'gear' },
  { id: 'tasks', label: '任务中心', icon: 'task' },
];

function stageTone(state: StageState | undefined): Tone {
  if (!state) return 'muted';
  return STAGE_META[state].tone;
}

export function TopBar({ project }: { project: Project | null }) {
  const { setView, view, leaveProject } = useProjectStore();
  return (
    <header className="hf-top">
      <button type="button" className="hf-brand" onClick={leaveProject}>
        <span className="hf-mark"><i /><i /></span>双声播客工坊
      </button>
      {project && <span className="hf-proj">{project.id} · {project.title}</span>}
      <span className="hf-spacer" />
      <span className="hf-vram"><span className="bar"><i /></span>VRAM 17%</span>
      <button
        type="button"
        className="wu-btn"
        data-variant={view === 'tasks' ? 'primary' : 'ghost'}
        data-size="sm"
        aria-current={view === 'tasks' ? 'page' : undefined}
        onClick={() => setView('tasks')}
      >
        <Icon name="task" size={16} />任务
      </button>
    </header>
  );
}

export function StageRail({ project, onNavigate }: { project: Project | null; onNavigate: (v: View) => void }) {
  const { view } = useProjectStore();
  const progress = project?.stageProgress ?? {};
  return (
    <nav className="hf-rail" aria-label="阶段导航">
      {STAGE_ORDER.map((s, i) => {
        const st = progress[s.id];
        const active = view === s.id;
        const done = st === 'done' || st === 'pending_confirm' || st === 'stale';
        return (
          <button
            key={s.id}
            type="button"
            className={`hf-stg ${active ? 'active' : ''} ${done ? 'done' : ''}`}
            onClick={() => onNavigate(s.id)}
            aria-current={active ? 'page' : undefined}
          >
            <span className="hf-stg-no">{i + 1}</span>
            <span className="hf-stg-tt"><b>{s.title}</b><small>{s.sub}</small></span>
            {st && st !== 'ready' && (
              <span className="hf-stg-st"><Badge tone={stageTone(st)}>{STAGE_META[st].label}</Badge></span>
            )}
          </button>
        );
      })}
      <div className="hf-sep" />
      {ENTRY_VIEWS.map((e) => (
        <button
          key={e.id}
          type="button"
          className={`hf-entry ${view === e.id ? 'active' : ''}`}
          onClick={() => onNavigate(e.id)}
          aria-current={view === e.id ? 'page' : undefined}
        >
          <Icon name={e.icon} size={16} />
          {e.label}
        </button>
      ))}
      <div className="hf-note">工程与成片保存在本机；文本 / 图片请求发送到所选 API。</div>
    </nav>
  );
}

export function StageHead({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="hf-stage-head">
      <h2>{title}</h2>
      {children}
    </div>
  );
}

/* ----------------------------------------------------------------------------
   TimelineBar —— 应用壳的常驻底部区域（05 §1.5「唯一深色面」/ §4.4 高 56）
   设计稿在 HF-03 / HF-04 / HF-05 三屏都渲染同一条时间轨，此前实现只在 HF-03 内联，
   导致另外两屏整条缺失（.hf-timeline 0/1）。此处抽成壳级组件，三屏共享同一份标记。
   -------------------------------------------------------------------------- */

/** 样本数 → `MM:SS.d`（设计稿时间区文案格式 `01:23.4 / 02:48.120`）。 */
export function fmtDur(samples: number, sampleRate: number): string {
  const secs = sampleRate > 0 ? samples / sampleRate : 0;
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const ds = Math.floor((secs - Math.floor(secs)) * 10);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${ds}`;
}

export function TimelineBar({
  timeline, speakerOf, playPct = 0, curSecs = '00:00.0', labels,
}: {
  timeline: AudioTimeline | null;
  /** 由 turnId 反查说话人，决定片段落在 A 段色还是 B 段色 */
  speakerOf?: (turnId: string) => 'A' | 'B';
  playPct?: number;
  curSecs?: string;
  labels?: React.ReactNode[];
}) {
  if (!timeline) return null;
  const units = timeline.units ?? [];
  const total = timeline.sampleCount || 0;
  const segs = units.map((u) => {
    if (total <= 0) return null;
    const start = timeline.unitOffsets[u.id] ?? 0;
    return {
      id: u.id,
      spk: (speakerOf?.(u.turnId) ?? 'A') === 'A' ? 'a' : 'b',
      left: (start / total) * 100,
      width: Math.max(0.4, (u.sampleCount / total) * 100),
      mark: (start / total) * 100,
    };
  }).filter(Boolean) as { id: string; spk: string; left: number; width: number; mark: number }[];

  return (
    <div className="hf-timeline" aria-label="时间轨">
      <span className="hf-tl-time">
        <Icon name="play" size={16} />
        {curSecs} / {fmtDur(total, timeline.sampleRate)}
      </span>
      <div className="hf-tl-tracks">
        <div className="hf-tl-track">
          {segs.map((s) => (
            <span key={s.id} className={`hf-tl-seg ${s.spk}`} style={{ left: `${s.left}%`, width: `${s.width}%` }} />
          ))}
          <span className="hf-tl-play" style={{ left: `${playPct}%` }} />
        </div>
        <div className="hf-tl-track" style={{ height: 8 }}>
          {segs.map((s) => (
            <span key={s.id} className="hf-tl-mark" style={{ left: `${s.mark}%` }} />
          ))}
        </div>
      </div>
      {(labels ?? []).map((l, i) => <span key={i} className="hf-tl-label">{l}</span>)}
      <span className="hf-tl-label">{units.length} 单元 · {timeline.sampleRate}Hz</span>
    </div>
  );
}

export function FootBar({ hint }: { hint?: React.ReactNode }) {
  return (
    <footer className="hf-foot">
      <span className="hf-pos">阶段导航</span>
      <span>·</span>
      <span>{hint ?? '选择左侧阶段进入对应制作页'}</span>
      <span className="hf-spacer" />
      <span className="wu-caption">双声播客工坊 v0.1</span>
    </footer>
  );
}

export function AppShell({
  project, onNavigate, children, footer, inspector, timeline,
}: {
  project: Project | null; onNavigate: (v: View) => void;
  children: React.ReactNode;
  /** 传 null 表示该屏没有 FootBar（设计稿 HF-06/07/08 的「独立全屏」形态） */
  footer?: React.ReactNode;
  inspector?: React.ReactNode;
  timeline?: React.ReactNode;
}) {
  return (
    <div className="hf-app">
      <TopBar project={project} />
      <div className="hf-mid">
        <StageRail project={project} onNavigate={onNavigate} />
        <main className="hf-main">
          {children}
        </main>
        {/* 检查器是 .hf-mid 的第三列（与 rail / main 并排），不是 .hf-body 的子元素。
            设计稿 HF-02/03/04/05/06/08 均为该结构；此前实现把它放进 hf-body 并用
            position:sticky 补偿，导致检查器顶部比阶段标题低一档、且底部被 FootBar 切齐。 */}
        {inspector}
      </div>
      {/* TimelineBar 与 FootBar 都是 .hf-app 的直接子元素（全宽），不是 .hf-main 的子元素。
          实测设计稿 .hf-app 子元素顺序 = [hf-top, hf-mid, hf-timeline, hf-foot]；
          此前实现两者都塞进 .hf-main，导致底栏起点右移 208px（阶段轨宽度）。 */}
      {timeline}
      {footer === undefined ? <FootBar /> : footer}
    </div>
  );
}
