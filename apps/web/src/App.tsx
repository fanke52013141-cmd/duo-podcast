/* ============================================================================
   DuoCast 前端 · 应用路由（视图切换）+ SSE 事件接线 + 能力快照同步
   视图：home（无工程全屏）｜五阶段（01 §3）｜资产库 / 服务设置 / 任务中心
   ========================================================================== */
import { Component, useEffect, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useCapabilities, useProject } from './lib/api';
import { startSse } from './lib/sse';
import { useProjectStore, useServiceStore, saveSession } from './stores';
import { AppShell } from './components/AppShell';
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

/* 页面渲染崩溃兜底（第五轮实测 A6：打开工程偶发整页白屏）。
   捕获渲染期异常后给出可操作的恢复界面，而不是无提示白屏。 */
class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="wu-empty" style={{ minHeight: '60vh' }}>
          <h3>页面出现了一个错误</h3>
          <p className="wu-muted" style={{ maxWidth: 520, margin: '8px auto 16px' }}>
            {this.state.error.message || '未知渲染错误'}。你的工程数据已保存；重试或返回工程列表均可继续。
          </p>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
            <Button variant="brand" onClick={() => this.setState({ error: null })}>重试</Button>
            <Button variant="secondary" onClick={() => { useProjectStore.getState().leaveProject(); this.setState({ error: null }); }}>
              返回工程列表
            </Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

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
        </AppShell>
      );
  }
}

export default function App() {
  const qc = useQueryClient();
  const { currentProjectId, view } = useProjectStore();
  const setCapabilities = useServiceStore((s) => s.setCapabilities);

  // 能力快照 → serviceStore（UI 门控）
  const caps = useCapabilities();
  useEffect(() => {
    if (caps.data) setCapabilities(caps.data);
  }, [caps.data, setCapabilities]);

  // SSE：job.* 事件 → 失效任务与工程查询；system.resync → 全量回读
  useEffect(() => {
    const handle = startSse(
      (ev) => {
        if (typeof ev.type === 'string' && ev.type.startsWith('job.')) {
          // 任务事件只触发查询失效；活动任务集合由各页从 /api/jobs 派生，避免第三份状态源。
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
  }, [qc, currentProjectId]);

  // 会话持久化：写回。恢复由 stores 初始化时同步完成（避免首帧先渲染首页再跳转）。
  useEffect(() => {
    saveSession(currentProjectId, view);
  }, [currentProjectId, view]);

  if (!currentProjectId || view === 'home') {
    return <HomePage />;
  }
  return (
    <PageErrorBoundary>
      <WorkspacePage projectId={currentProjectId} view={view} />
    </PageErrorBoundary>
  );
}
