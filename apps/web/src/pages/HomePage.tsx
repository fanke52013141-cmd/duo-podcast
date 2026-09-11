/* ============================================================================
   HF-00 · 工程首页（真实数据流）
   最近工程（POST/GET /projects）+ 节目模板 + 空态 + 新建本期模板弹层
   版式取值全部来自 duo-shell.css 的 .hf-home* / .hf-sec-head / .hf-grid3 /
   .hf-pcard / .hf-tcard / .hf-new-tpl / .hf-fieldset（对齐 HF-00 内联样式），
   页面内不再自定义跨屏布局（此前 31 处内联 style 是版式漂移的来源）。

   卡片内容契约（对齐 HF-00 主稿的 .wu-card.hf-pcard）：
     .hf-pcard-h  → h3 标题 + 「更多操作」图标按钮
     .hf-roles    → A/B 角色徽章 + 姓名 · 职能
     .hf-badges   → 第一行「阶段X · 阶段名」「成片 vN」；第二行后台任务状态
     .wu-caption  → 「最后编辑 MM-DD」
     .wu-btn      → 默认(primary) sm 实心「继续制作」，撑满卡片宽度
   ========================================================================== */
import { useState } from 'react';
import { useCreateProject, useJobs, usePatchProject, useProjects } from '../lib/api';
import {
  currentStageOf, formatEditedAt, outputVersionLabel, SPEAKERS, type Job, type Project,
} from '../lib/types';
import { Badge, Button, Card, Empty, Icon, Input, Modal } from '../components/wu';
import { useProjectStore } from '../stores';

const TEMPLATES = [
  {
    id: 'tech-weekly',
    name: '科技周报',
    desc: 'A·李雷（主持）/ B·韩梅梅（嘉宾）· 演播室同框 · 每周更新节奏',
    tags: ['轻松对谈', '横屏 16:9', '本地引擎', '3 确认点'],
  },
  {
    id: 'interview',
    name: '人物访谈',
    desc: 'A·主持（追问）/ B·嘉宾（观点输出）· 深度问答结构 · 可绑定 MiniMax 音色',
    tags: ['深度问答', '横屏 16:9', '本地 + MiniMax', '3 确认点'],
  },
];

/** 工程的后台任务口径：排队/执行中计为「后台任务 N」，渲染类另给「渲染中」文案。 */
function jobSummary(jobs: Job[] | undefined, projectId: string) {
  const active = (jobs ?? []).filter(
    (j) => j.projectId === projectId && (j.status === 'queued' || j.status === 'running'),
  );
  if (active.length === 0) return null;
  const rendering = active.find((j) => j.kind.startsWith('render'));
  const seg = rendering?.result as { done?: number; total?: number } | null | undefined;
  if (rendering && seg && typeof seg.done === 'number' && typeof seg.total === 'number') {
    return { tone: 'info' as const, text: `渲染中 ${seg.done}/${seg.total} 段` };
  }
  return { tone: 'warning' as const, text: `后台任务 ${active.length}` };
}

/* 工程卡：结构对齐 HF-00 —— .wu-card.hf-pcard > .hf-pcard-h + .hf-roles + 两行徽章 + 说明 + 按钮
   注意：不能加 Card 的 interactive（会写入 data-variant="interactive"），
   基线 `.wu-card[data-variant="interactive"]{display:block}` 特异性 (0,2,0)
   高于 `.hf-pcard{display:grid}` (0,1,0)，会把卡片内部 10px 栅格间距挤掉。 */
function ProjectCard({ p, jobs, onOps }: { p: Project; jobs: Job[] | undefined; onOps: (p: Project) => void }) {
  const openProject = useProjectStore((s) => s.openProject);
  const stage = currentStageOf(p);
  const film = outputVersionLabel(p.outputVersion);
  const edited = formatEditedAt(p.updatedAt);
  const task = jobSummary(jobs, p.id);

  return (
    <Card className="hf-pcard">
      <div className="hf-pcard-h">
        <h3>{p.id} · {p.title}</h3>
        <button
          type="button" className="wu-btn wu-icon-btn" data-size="sm"
          aria-label={`${p.id} 更多操作`}
          onClick={() => onOps(p)}
        >
          <Icon name="more" size={16} />
        </button>
      </div>

      <p className="hf-roles">
        <Badge tone="info">A</Badge>{SPEAKERS.A.name} · {SPEAKERS.A.role}
        <Badge tone="speaker-b">B</Badge>{SPEAKERS.B.name} · {SPEAKERS.B.role}
      </p>

      <div className="wu-row hf-badges">
        <span className="wu-badge">阶段{stage.cn} · {stage.title}</span>
        {film && <span className="wu-badge">{film}</span>}
      </div>

      {task && (
        <div className="wu-row hf-badges">
          <span className="wu-badge" data-tone={task.tone}>{task.text}</span>
        </div>
      )}

      {edited && <p className="wu-caption">{edited}</p>}

      <Button size="sm" onClick={() => openProject(p.id)}>继续制作</Button>
    </Card>
  );
}

export function HomePage() {
  const { data: projects = [], isLoading } = useProjects();
  const { data: jobs } = useJobs();
  const createProject = useCreateProject();
  const patchProject = usePatchProject();
  const openProject = useProjectStore((s) => s.openProject);
  const [modalOpen, setModalOpen] = useState(false);
  const [templateId, setTemplateId] = useState<string>('blank');
  const [opsTarget, setOpsTarget] = useState<Project | null>(null);
  const [renameTo, setRenameTo] = useState('');

  const handleCreate = async () => {
    // 失败时 Modal 内已有 createProject.isError 告警（见下方渲染），
    // 这里捕获以避免未处理 rejection（11 报告 P2-14）
    try {
      const proj = await createProject.mutateAsync({ title: '未命名节目' });
      setModalOpen(false);
      openProject(proj.id);
    } catch {
      /* 提示由 Modal 内的 isError 告警承担 */
    }
  };

  const openOps = (p: Project) => {
    setOpsTarget(p);
    setRenameTo(p.title);
  };

  const handleRename = () => {
    if (!opsTarget) return;
    const title = renameTo.trim();
    if (!title || title === opsTarget.title) { setOpsTarget(null); return; }
    patchProject.mutate(
      { projectId: opsTarget.id, patch: { title }, expectedRevision: opsTarget.revision },
      {
        onSuccess: () => setOpsTarget(null),
        onError: () => { /* 冲突/失败保留弹层，错误提示见下方 alert */ },
      },
    );
  };

  return (
    <div className="hf-home">
      <header className="hf-home-top">
        <span className="hf-brand"><span className="hf-mark"><i /><i /></span>双声播客工坊</span>
        <span className="hf-spacer" />
        <Button
          variant="secondary" icon="upload"
          onClick={() => alert('导入工程：暂未开放（设计稿为调起系统文件管理器选择导出的工程 zip）')}
        >
          导入工程
        </Button>
        <Button variant="brand" icon="plus" onClick={() => setModalOpen(true)}>新建本期</Button>
      </header>

      <main className="hf-home-main">
        {isLoading ? (
          <Empty title="读取工程中…" />
        ) : projects.length === 0 ? (
          <div className="wu-empty">
            <svg width="160" viewBox="0 0 256 192" aria-hidden="true">
              <ellipse cx="128" cy="163" rx="93" ry="9" fill="var(--wu-semantic-muted)" />
              <g stroke="var(--wu-global-ink)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none">
                <rect x="66" y="40" width="140" height="104" rx="10" fill="var(--wu-semantic-surface)" />
                <path d="M46 75h68l12 14h82v62H46Z" fill="var(--wu-semantic-brand)" />
                <path d="M84 62h80M84 74h45" />
                <path d="M55 112h144" opacity=".3" />
              </g>
            </svg>
            <h3>还没有工程</h3>
            <p className="wu-muted">新建一期，从话题、文章或已有脚本开始；也可以导入此前导出的工程 zip。</p>
            <Button variant="brand" onClick={() => setModalOpen(true)}>新建本期</Button>
          </div>
        ) : (
          <>
            <section>
              <div className="hf-sec-head">
                <h2>最近工程</h2>
                <span className="wu-caption">{projects.length} 个</span>
                <span className="hf-spacer" />
                <Button
                  variant="secondary" size="sm" icon="folder"
                  onClick={() => alert('打开工程目录：暂未开放（设计稿为调起系统资源管理器定位工程目录）')}
                >
                  打开工程目录
                </Button>
              </div>
              <div className="hf-grid3">
                {projects.map((p) => <ProjectCard key={p.id} p={p} jobs={jobs} onOps={openOps} />)}
              </div>
            </section>

            <section>
              <div className="hf-sec-head">
                <h2>节目模板</h2>
                <span className="wu-caption">{TEMPLATES.length + 1} 个</span>
                <span className="hf-spacer" />
                <Button
                  variant="secondary" size="sm"
                  onClick={() => alert('保存为模板：暂未开放（需后端提供模板持久化 API）')}
                >
                  保存为模板
                </Button>
              </div>
              <div className="hf-grid3">
                {TEMPLATES.map((t) => (
                  <Card key={t.id} className="hf-tcard">
                    <h3>{t.name}</h3>
                    <p>{t.desc}</p>
                    <div className="wu-row">
                      {t.tags.map((tag) => <span key={tag} className="wu-badge">{tag}</span>)}
                    </div>
                    <Button size="sm" variant="secondary" onClick={() => { setTemplateId(t.id); setModalOpen(true); }}>
                      用此模板新建
                    </Button>
                  </Card>
                ))}
                <button
                  type="button" className="wu-card hf-new-tpl" aria-label="新建模板"
                  onClick={() => { setTemplateId('blank'); setModalOpen(true); }}
                >
                  <Icon name="plus" size={20} />
                  <span className="t">新建模板</span>
                </button>
              </div>
            </section>
          </>
        )}
      </main>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="新建本期"
        footer={
          <>
            <Button variant="ghost" onClick={() => setModalOpen(false)}>取消</Button>
            <Button variant="brand" busy={createProject.isPending} onClick={handleCreate}>创建</Button>
          </>
        }
      >
        <p className="wu-muted hf-hint">从模板开始，或创建空白工程——模板只带入可复用配置与素材引用。</p>
        <fieldset className="hf-fieldset">
          <legend>选择起点</legend>
          <label className="wu-choice">
            <input type="radio" name="tpl" checked={templateId === 'blank'} onChange={() => setTemplateId('blank')} />
            <span><b>空白工程</b> <span className="wu-caption">从话题 / 文章 / 已有脚本开始</span></span>
          </label>
          {TEMPLATES.map((t) => (
            <label key={t.id} className="wu-choice">
              <input type="radio" name="tpl" checked={templateId === t.id} onChange={() => setTemplateId(t.id)} />
              <span><b>{t.name}</b> <span className="wu-caption">{t.desc}</span></span>
            </label>
          ))}
        </fieldset>
        {createProject.isError && (
          <div className="wu-alert" data-tone="danger">
            创建失败：{(createProject.error as Error)?.message}
          </div>
        )}
      </Modal>

      <Modal
        open={!!opsTarget}
        onClose={() => setOpsTarget(null)}
        title={opsTarget ? `${opsTarget.id} · 工程操作` : '工程操作'}
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpsTarget(null)}>取消</Button>
            <Button variant="brand" busy={patchProject.isPending} onClick={handleRename}>保存名称</Button>
          </>
        }
      >
        <label className="wu-field">
          <span className="wu-label">节目名</span>
          <Input value={renameTo} onChange={(e) => setRenameTo(e.target.value)} aria-label="节目名" />
        </label>
        {patchProject.isError && (
          <div className="wu-alert" data-tone="danger">
            重命名失败：{(patchProject.error as Error)?.message}
          </div>
        )}
        <div className="wu-alert" data-tone="muted">
          「打开工程目录」「删除工程」尚未接入后端（后者属危险操作，需先有软删除与回收站语义）。
        </div>
      </Modal>
    </div>
  );
}
