/* ============================================================================
   DuoCast 前端 · 应用壳（TopBar + 阶段轨 + 主区 + FootBar）
   对齐 HF-00/HF-01 结构；阶段状态徽章由工程 stageProgress 推导（01 §13.1）
   ========================================================================== */
import React from 'react';
import { STAGE_META, STAGE_ORDER, type Project, type StageState } from '../lib/types';
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
  project, onNavigate, children, footer,
}: { project: Project | null; onNavigate: (v: View) => void; children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <div className="hf-app">
      <TopBar project={project} />
      <div className="hf-mid">
        <StageRail project={project} onNavigate={onNavigate} />
        <main className="hf-main">
          {children}
          {footer ?? <FootBar />}
        </main>
      </div>
    </div>
  );
}
