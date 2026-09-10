/* ============================================================================
   DuoCast 前端 · 应用路由（视图切换）+ SSE 事件接线 + 能力快照同步
   视图：home（无工程全屏）｜五阶段（01 §3）｜资产库 / 服务设置 / 任务中心
   ========================================================================== */
import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useCapabilities, useProject } from './lib/api';
import { startSse } from './lib/sse';
import { useJobStore, useProjectStore, useServiceStore } from './stores';
import { AppShell, FootBar } from './components/AppShell';
import { Empty, Button } from './components/wu';
import { HomePage } from './pages/HomePage';
import { CreateEpisodePage } from './pages/CreateEpisodePage';
import { EditScriptPage } from './pages/EditScriptPage';
import { VoicePage } from './pages/VoicePage';
import { VisualPage } from './pages/VisualPage';
import { RenderPage } from './pages/RenderPage';
import { AssetsPage } from './pages/AssetsPage';
import { ServicesPage } from './pages/ServicesPage';
import { TasksPage } from './pages/TasksPage';

function WorkspacePage({ projectId, view }: { projectId: string; view: string }) {
  const project = useProject(projectId);
  const setView = useProjectStore((s) => s.setView);
  switch (view) {
    case 'create':
      return <CreateEpisodePage projectId={projectId} />;
    case 'script':
      return <EditScriptPage projectId={projectId} />;
    case 'voice':
      return <VoicePage projectId={projectId} />;
    case 'visual':
      return <VisualPage projectId={projectId} />;
    case 'render':
      return <RenderPage projectId={projectId} />;
    case 'assets':
      return <AssetsPage project={project.data ?? null} />;
    case 'services':
      return <ServicesPage />;
    case 'tasks':
      return <TasksPage />;
    default:
      return (
        <AppShell project={project.data ?? null} onNavigate={setView}>
          <div className="hf-body">
            <Empty
              title={`「${view}」页面实现中`}
              desc="该视图尚未接入真实页面，请在左侧阶段导航中选择已实现的制作页。"
              action={<Button variant="secondary" onClick={() => setView('create')}>返回创建本期</Button>}
            />
          </div>
          <FootBar />
        </AppShell>
      );
  }
}

export default function App() {
  const qc = useQueryClient();
  const { currentProjectId, view } = useProjectStore();
  const setCapabilities = useServiceStore((s) => s.setCapabilities);
  const noteSeq = useJobStore((s) => s.noteSeq);
  const touch = useJobStore((s) => s.touch);

  // 能力快照 → serviceStore（UI 门控）
  const caps = useCapabilities();
  useEffect(() => {
    if (caps.data) setCapabilities(caps.data);
  }, [caps.data, setCapabilities]);

  // SSE：job.* 事件 → 失效任务与工程查询；system.resync → 全量回读
  useEffect(() => {
    const handle = startSse(
      (ev) => {
        noteSeq(ev.seq);
        if (typeof ev.type === 'string' && ev.type.startsWith('job.')) {
          const jobId = typeof ev.jobId === 'string' ? ev.jobId : '';
          if (jobId) touch(jobId, String(ev.status ?? 'running') as never);
          qc.invalidateQueries({ queryKey: ['jobs'] });
          if (currentProjectId) qc.invalidateQueries({ queryKey: ['project', currentProjectId] });
        }
      },
      () => {
        qc.invalidateQueries();
        qc.refetchQueries();
      },
    );
    return handle.stop;
  }, [qc, currentProjectId, noteSeq, touch]);

  if (!currentProjectId || view === 'home') {
    return <HomePage />;
  }
  return <WorkspacePage projectId={currentProjectId} view={view} />;
}
