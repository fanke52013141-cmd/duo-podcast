/* ============================================================================
   HF-00 · 工程首页（真实数据流）
   最近工程（POST/GET /projects）+ 节目模板 + 空态 + 新建本期模板弹层
   ========================================================================== */
import { useState } from 'react';
import { useCreateProject, useProjects } from '../lib/api';
import { type Project } from '../lib/types';
import { Badge, Button, Card, Empty, Icon, Modal, type Tone } from '../components/wu';
import { useProjectStore } from '../stores';

const TEMPLATES = [
  { id: 'tech-weekly', name: '科技周报', desc: '每周科技动态双人对谈' },
  { id: 'interview', name: '人物访谈', desc: '主持人与嘉宾深度访谈' },
];

function projectTone(p: Project): Tone {
  const s = p.stageProgress;
  if (Object.values(s).some((v) => v === 'running')) return 'info';
  if (s.render === 'done') return 'success';
  if (Object.values(s).some((v) => v === 'pending_confirm')) return 'warning';
  return 'muted';
}

function projectStageLabel(p: Project): string {
  const s = p.stageProgress;
  if (Object.values(s).some((v) => v === 'running')) return '制作中';
  if (s.render === 'done') return '已渲染';
  if (s.create === 'done' || s.script === 'done') return '制作中';
  return '草稿';
}

function ProjectCard({ p }: { p: Project }) {
  const openProject = useProjectStore((s) => s.openProject);
  const rev = p.currentDraftRevision;
  return (
    <Card interactive className="hf-pcard" >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div className="wu-row" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {p.id} · {p.title}
          </h3>
          <Badge tone={projectTone(p)}>{projectStageLabel(p)}</Badge>
        </div>
        <p className="wu-caption" style={{ margin: 0, fontSize: 12 }}>
          <Badge tone="info">A</Badge> 主持人 <span style={{ margin: '0 6px' }} /> <Badge tone="speaker-b">B</Badge> 嘉宾
        </p>
        <div className="wu-row" style={{ gap: 8 }}>
          {rev && <span className="hf-mono" style={{ color: 'var(--wu-semantic-caption)' }}>脚本 {rev}</span>}
          <span className="hf-mono" style={{ color: 'var(--wu-semantic-caption)' }}>{p.aspect === 'portrait' ? '竖屏' : '横屏 16:9'}</span>
        </div>
        <div style={{ marginTop: 4 }}>
          <Button size="sm" variant="secondary" onClick={() => openProject(p.id)}>
            继续制作<Icon name="arrowRight" size={14} />
          </Button>
        </div>
      </div>
    </Card>
  );
}

export function HomePage() {
  const { data: projects = [], isLoading } = useProjects();
  const createProject = useCreateProject();
  const openProject = useProjectStore((s) => s.openProject);
  const [modalOpen, setModalOpen] = useState(false);
  const [templateId, setTemplateId] = useState<string>('blank');

  const handleCreate = async () => {
    const proj = await createProject.mutateAsync({ title: '未命名节目' });
    setModalOpen(false);
    openProject(proj.id);
  };

  return (
    <div style={{ minHeight: '100%', display: 'flex', flexDirection: 'column' }}>
      <header className="hf-top" style={{ borderBottom: 'none' }}>
        <span className="hf-brand"><span className="hf-mark"><i /><i /></span>双声播客工坊</span>
        <span className="hf-spacer" />
        <Button variant="brand" icon="plus" onClick={() => setModalOpen(true)}>新建本期</Button>
      </header>

      <div style={{ flex: 1, padding: '10px 20px 24px', overflow: 'auto' }}>
        {isLoading ? (
          <Empty title="读取工程中…" />
        ) : projects.length === 0 ? (
          <div className="wu-empty" style={{ paddingTop: 72 }}>
            <svg width="160" viewBox="0 0 256 192" aria-hidden="true">
              <ellipse cx="128" cy="163" rx="93" ry="9" fill="var(--wu-semantic-muted)" />
              <g stroke="var(--wu-global-ink)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none">
                <rect x="66" y="40" width="140" height="104" rx="10" fill="var(--wu-semantic-surface)" />
                <path d="M46 75h68l12 14h82v62H46Z" fill="var(--wu-semantic-brand)" />
                <path d="M84 62h80M84 74h45" />
                <path d="M55 112h144" opacity=".3" />
              </g>
            </svg>
            <h3 style={{ fontSize: 16, margin: '8px 0 4px' }}>还没有工程</h3>
            <p className="wu-muted" style={{ margin: '0 0 16px' }}>新建一期，从话题、文章或已有脚本开始；也可以导入此前导出的工程 zip。</p>
            <Button variant="brand" onClick={() => setModalOpen(true)}>新建本期</Button>
          </div>
        ) : (
          <>
            <section style={{ marginBottom: 28 }}>
              <div className="hf-sec-head" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <h2 style={{ fontSize: 18, fontWeight: 700 }}>最近工程</h2>
                <span style={{ flex: 1 }} />
                <Button variant="secondary" size="sm" icon="folder" onClick={() => alert('打开工程目录：暂由系统文件管理器导入（开发中）')}>
                  打开工程目录
                </Button>
              </div>
              <div className="hf-grid3" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14, marginTop: 10 }}>
                {projects.map((p) => <ProjectCard key={p.id} p={p} />)}
              </div>
            </section>

            <section>
              <div className="hf-sec-head" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <h2 style={{ fontSize: 18, fontWeight: 700 }}>节目模板</h2>
                <span style={{ flex: 1 }} />
                <Button variant="secondary" size="sm">保存为模板</Button>
              </div>
              <div className="hf-grid3" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14, marginTop: 10 }}>
                {TEMPLATES.map((t) => (
                  <Card key={t.id}>
                    <h3 style={{ fontSize: 14, fontWeight: 600 }}>{t.name}</h3>
                    <p className="wu-caption" style={{ margin: '4px 0 12px' }}>{t.desc}</p>
                    <Button size="sm" variant="secondary" onClick={() => { setTemplateId(t.id); setModalOpen(true); }}>
                      用此模板新建
                    </Button>
                  </Card>
                ))}
                <Card interactive onClick={() => { setTemplateId('blank'); setModalOpen(true); }} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 6, cursor: 'pointer', minHeight: 120 }}>
                  <Icon name="plus" size={22} />
                  <span style={{ fontWeight: 600 }}>新建模板</span>
                  <span className="wu-caption">保存当前配置为模板</span>
                </Card>
              </div>
            </section>
          </>
        )}
      </div>

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
        <p className="wu-muted" style={{ margin: '0 0 14px' }}>从模板开始，或创建空白工程——模板只带入可复用配置与素材引用。</p>
        <fieldset style={{ border: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
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
          <div className="wu-alert" data-tone="danger" style={{ marginTop: 12 }}>
            创建失败：{(createProject.error as Error)?.message}
          </div>
        )}
      </Modal>
    </div>
  );
}
