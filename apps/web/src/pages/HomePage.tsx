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
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { MaintenancePanel } from '../components/MaintenancePanel';
import { filterProjects } from '../lib/workflowTools';
import { getJson } from '../lib/api';
import {
  useCapabilities, useCreateProject, useCreateTemplate, useDeleteProject, useJobs, usePatchProject, useProjects, useTemplates,
} from '../lib/api';
import {
  currentStageOf, formatEditedAt, outputVersionLabel, SPEAKERS, type Job, type ProgramTemplate,
  type Project,
} from '../lib/types';
import { Badge, Button, Card, Choice, Empty, Icon, Input, Modal } from '../components/wu';
import { useProjectStore } from '../stores';

/** 工程的后台任务口径：排队/执行中/暂停都算「还有活没干完」，渲染类另给进度文案。 */
function jobSummary(jobs: Job[] | undefined, projectId: string) {
  const PENDING = ['queued', 'running', 'recovering', 'pause_requested', 'paused', 'waiting_confirmation'];
  const active = (jobs ?? []).filter(
    (j) => j.projectId === projectId && PENDING.includes(j.status),
  );
  if (active.length === 0) return null;
  const rendering = active.find((j) => j.kind.startsWith('render'));
  const seg = rendering?.result as { done?: number; total?: number } | null | undefined;
  if (rendering && seg && typeof seg.done === 'number' && typeof seg.total === 'number') {
    return { tone: 'info' as const, text: `渲染中 ${seg.done}/${seg.total} 段` };
  }
  // 暂停中的任务以前从首页消失，用户以为已经结束
  if (active.every((j) => j.status === 'paused' || j.status === 'pause_requested')) {
    return { tone: 'warning' as const, text: `暂停中 ${active.length}` };
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

      {/* 直达工程当前阶段（阶段由服务端 stageProgress 推导），而不是每次都落回阶段① */}
      <Button size="sm" onClick={() => openProject(p.id, currentStageOf(p).id)}>继续制作</Button>
      <a className="hf-link" href={`/api/projects/${p.id}/backup`} download>备份工程（含必要媒体）</a>
    </Card>
  );
}

export function HomePage() {
  const importFile = useRef<HTMLInputElement>(null);
  const [importMessage,setImportMessage] = useState('');
  const importing=useRef(false);
  const [importBusy,setImportBusy]=useState(false);
  const [importResult,setImportResult]=useState<{projectId:string;missing:string[];summary:{sourceProjectId:string;mediaCount:number;mediaBytes:number;scriptVersions:number;recheck:string[];external:string[]}}|null>(null);
  const qc = useQueryClient();
  const setView = useProjectStore(s => s.setView);
  const { data: projects = [], isLoading } = useProjects();
  const { data: jobs } = useJobs();
  const createProject = useCreateProject();
  const capabilities = useCapabilities();
  const availableModes = capabilities.data?.video.capabilities.generationModes;
  const fastAvailable = Array.isArray(availableModes) && availableModes.includes('fast');
  const patchProject = usePatchProject();
  const deleteProject = useDeleteProject();
  const createTemplate = useCreateTemplate();
  const templates = useTemplates();
  const tplList: ProgramTemplate[] = templates.data ?? [];
  const openProject = useProjectStore((s) => s.openProject);
  const [modalOpen, setModalOpen] = useState(false);
  const [templateId, setTemplateId] = useState<string>('blank');
  const [generationMode, setGenerationMode] = useState<'fast' | 'standard'>('fast');
  const [fastTemplates,setFastTemplates] = useState<{id:string;label:string;ready:boolean;version:string}[]>([]);
  const [fastTemplateId,setFastTemplateId] = useState('');
  useEffect(()=>{void getJson<typeof fastTemplates>('/diagnostics/fast-templates').then(list=>{setFastTemplates(list);setFastTemplateId(list.find(t=>t.ready)?.id??'');}).catch(()=>setFastTemplates([]));},[]);
  useEffect(() => { if (capabilities.data && !fastAvailable) setGenerationMode('standard'); }, [capabilities.data, fastAvailable]);
  const [opsTarget, setOpsTarget] = useState<Project | null>(null);
  const [renameTo, setRenameTo] = useState('');
  // 工程管理（第五轮实测 A8）：搜索 + 阶段筛选 + 软删除
  const [searchText, setSearchText] = useState('');
  const [stageFilter, setStageFilter] = useState('all');
  const [sortOrder,setSortOrder]=useState('recent');
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const filteredProjects = useMemo(() => filterProjects(projects,searchText,stageFilter,sortOrder,p=>currentStageOf(p).id),[projects,searchText,stageFilter,sortOrder]);

  // 保存为模板：从既有工程提取配置（画幅 + A/B 声线引用）
  const [saveTplOpen, setSaveTplOpen] = useState(false);
  const [saveTplProjectId, setSaveTplProjectId] = useState<string>('');
  const [saveTplName, setSaveTplName] = useState('');
  const [saveTplDesc, setSaveTplDesc] = useState('');
  // 新建空模板
  const [newTplOpen, setNewTplOpen] = useState(false);
  const [newTplName, setNewTplName] = useState('');
  const [newTplDesc, setNewTplDesc] = useState('');
  const [newTplAspect, setNewTplAspect] = useState<'landscape' | 'portrait'>('landscape');

  const handleCreate = async () => {
    // 失败时 Modal 内已有 createProject.isError 告警（见下方渲染），
    // 这里捕获以避免未处理 rejection（11 报告 P2-14）
    try {
      // 模板在服务端创建时套用（画幅 + A/B 声线引用）；标题带模板名前缀便于区分。
      const tpl = tplList.find((t) => t.id === templateId);
      const proj = await createProject.mutateAsync({
        title: tpl ? `${tpl.name} · 未命名节目` : '未命名节目',
        templateId: tpl ? tpl.id : undefined,
        generationMode,
        fastTemplateId: generationMode==='fast' ? fastTemplateId||undefined : undefined,
      });
      setModalOpen(false);
      openProject(proj.id);
    } catch {
      /* 提示由 Modal 内的 isError 告警承担 */
    }
  };

  const handleSaveTemplate = () => {
    const name = saveTplName.trim();
    if (!saveTplProjectId || !name) return;
    createTemplate.mutate(
      { projectId: saveTplProjectId, name, desc: saveTplDesc.trim() },
      { onSuccess: () => setSaveTplOpen(false) },
    );
  };

  const handleNewTemplate = () => {
    const name = newTplName.trim();
    if (!name) return;
    createTemplate.mutate(
      { name, desc: newTplDesc.trim(), config: { aspect: newTplAspect } },
      { onSuccess: () => setNewTplOpen(false) },
    );
  };

  const openOps = (p: Project) => {
    setOpsTarget(p);
    setRenameTo(p.title);
    setDeleteError(null);
    setConfirmDelete(false);
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

  // 删除采用应用内两步确认（第五轮实测 A8）：第一次点变「确认删除」，再点才执行；
  // 不用 window.confirm——阻塞式原生框点击一次后按钮无变化，用户容易困惑。
  const handleDelete = () => {
    if (!opsTarget) return;
    if (!confirmDelete) { setConfirmDelete(true); return; }
    deleteProject.mutate(opsTarget.id, {
      onSuccess: () => { setOpsTarget(null); setDeleteError(null); setConfirmDelete(false); },
      onError: (e) => { setDeleteError((e as Error).message); setConfirmDelete(false); },
    });
  };

  return (
    <div className="hf-home">
      <header className="hf-home-top">
        <span className="hf-brand"><span className="hf-mark"><i /><i /></span>双声播客工坊</span>
        <span className="hf-spacer" />
        <Button variant="secondary" icon="gear" onClick={() => setView('services')}>环境检查与服务设置</Button>
        <Button variant="secondary" icon="folder" onClick={() => setView('motionPacks')}>动作资产库与审看</Button>
        <Button
          variant="secondary" icon="upload" disabled={importBusy}
          onClick={() => importFile.current?.click()}
        >
          导入工程
        </Button>
        <Button variant="brand" icon="plus" onClick={() => setModalOpen(true)}>新建本期</Button>
      </header>

      <main className="hf-home-main">
        <input ref={importFile} aria-label="选择工程备份" type="file" accept=".zip" hidden onChange={async e=>{
          const file=e.target.files?.[0];e.target.value='';if(!file||importing.current)return;
          importing.current=true;setImportBusy(true);setImportResult(null);
          try {setImportMessage('正在校验并另存工程…');const form=new FormData();form.append('file',file);const response=await fetch('/api/projects/import-backup',{method:'POST',body:form});const result=await response.json();if(!response.ok)throw new Error(result.detail??'导入失败');setImportResult(result);setImportMessage(`${result.projectId}：${result.notice}`);await qc.invalidateQueries({queryKey:['projects']});}catch(error){setImportMessage(`导入未完成：${(error as Error).message}。请确认选择的是工程备份 ZIP；损坏媒体需从原机器重新备份。`);}finally{importing.current=false;setImportBusy(false);}
        }}/>
        {importMessage&&<p role="status">{importMessage}</p>}
        {importResult&&<section aria-label="工程导入结果" className="wu-card"><h3>工程已另存：{importResult.projectId}</h3><p>原工程 {importResult.summary.sourceProjectId} 保持完整；导入 {importResult.summary.scriptVersions} 个脚本版本、{importResult.summary.mediaCount} 个媒体文件（{(importResult.summary.mediaBytes/1024**2).toFixed(1)} MB）。</p>
          <p>需重新确认：{importResult.summary.recheck.join('、')}。需在本机配置：{importResult.summary.external.join('、')}。</p>
          <p>缺失媒体 {importResult.missing.length} 项</p>{importResult.missing.length>0&&<ul>{importResult.missing.map((id,i)=><li key={i}>{id}</li>)}</ul>}
          <Button onClick={()=>openProject(importResult.projectId)}>检查导入工程</Button></section>}
        <MaintenancePanel/>
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
                <span className="wu-caption">{filteredProjects.length}{searchText || stageFilter !== 'all' ? ` / ${projects.length}` : ''} 个</span>
                <span className="hf-spacer" />
                <Input
                  aria-label="搜索工程"
                  placeholder="搜索工程 ID 或名称"
                  value={searchText}
                  onChange={(e) => setSearchText(e.target.value)}
                  style={{ width: 180, height: 30 }}
                />
                <select
                  className="wu-input" aria-label="按阶段筛选"
                  value={stageFilter} onChange={(e) => setStageFilter(e.target.value)}
                  style={{ width: 'auto', height: 30 }}
                >
                  <option value="all">全部阶段</option>
                  <option value="create">① 创建本期</option>
                  <option value="script">② 编辑对话</option>
                  <option value="voice">③ 试听配音</option>
                  <option value="visual">④ 预览画面</option>
                  <option value="render">⑤ 生成导出</option>
                </select>
                <select className="wu-input" style={{width:'auto'}} aria-label="工程排序" value={sortOrder} onChange={e=>setSortOrder(e.target.value)}><option value="recent">最近编辑优先</option><option value="oldest">最早编辑优先</option><option value="title">名称排序</option><option value="id">工程 ID 排序</option></select>
                <Button variant="ghost" size="sm" onClick={()=>{setSearchText('');setStageFilter('all');setSortOrder('recent');}}>重置筛选</Button>
                <Button
                  variant="secondary" size="sm" icon="folder"
                  disabled title="浏览器无法直接打开资源管理器；可下载工程备份或手动打开本机存储目录"
                >
                  打开工程目录
                </Button>
              </div>
              {filteredProjects.length === 0 && projects.length > 0 ? (
                <Empty title="没有匹配的工程" desc="换个关键词，或把阶段筛选切回「全部阶段」。" />
              ) : (
                <div className="hf-grid3">
                  {filteredProjects.map((p) => <ProjectCard key={p.id} p={p} jobs={jobs} onOps={openOps} />)}
                </div>
              )}
            </section>

            <section>
              <div className="hf-sec-head">
                <h2>节目模板</h2>
                <span className="wu-caption">{tplList.length} 个</span>
                <span className="hf-spacer" />
                <Button
                  variant="secondary" size="sm" icon="save"
                  disabled={projects.length === 0}
                  title={projects.length === 0 ? '先创建工程后，可将它的画幅与声线配置存为模板' : '从既有工程提取画幅与 A/B 声线配置'}
                  onClick={() => {
                    setSaveTplProjectId(projects[0]?.id ?? '');
                    setSaveTplName('');
                    setSaveTplDesc('');
                    setSaveTplOpen(true);
                  }}
                >
                  保存为模板
                </Button>
              </div>
              <div className="hf-grid3">
                {tplList.map((t) => (
                  <Card key={t.id} className="hf-tcard">
                    <h3>{t.name}{t.builtin && <span className="wu-caption">（内置）</span>}</h3>
                    <p>{t.desc || '（无简介）'}</p>
                    <div className="wu-row">
                      {(t.tags ?? []).map((tag) => <span key={tag} className="wu-badge">{tag}</span>)}
                      {t.config?.aspect && <span className="wu-badge">{t.config.aspect === 'portrait' ? '竖屏' : '横屏 16:9'}</span>}
                    </div>
                    <Button size="sm" variant="secondary" onClick={() => { setTemplateId(t.id); setModalOpen(true); }}>
                      用此模板新建
                    </Button>
                  </Card>
                ))}
                <button
                  type="button" className="wu-card hf-new-tpl" aria-label="新建模板"
                  onClick={() => { setNewTplName(''); setNewTplDesc(''); setNewTplAspect('landscape'); setNewTplOpen(true); }}
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
        <p className="wu-muted hf-hint">从模板开始，或创建空白工程——选择模板会带入模板名前缀、画幅与 A/B 声线引用，内容从零开始。</p>
        <fieldset className="hf-fieldset">
          <legend>制作模式</legend>
          <label className="wu-choice">
            <input type="radio" name="generation-mode" disabled={!fastAvailable} checked={generationMode === 'fast'} onChange={() => setGenerationMode('fast')} />
            <span><b>快速模式</b> <span className="wu-caption">{fastAvailable ? '使用已验证的双人演播室动作模板，按新对白更新口型' : '本机快速引擎未就绪，请选择原有模式'}</span></span>
          </label>
          <label className="wu-choice">
            <input type="radio" name="generation-mode" checked={generationMode === 'standard'} onChange={() => setGenerationMode('standard')} />
            <span><b>原有模式</b> <span className="wu-caption">沿用当前的完整画面与动作生成流程</span></span>
          </label>
        </fieldset>

        <fieldset className="hf-fieldset">
          <legend>选择起点</legend>
          {generationMode==='fast'&&fastTemplates.length>0&&<label>演播室素材版本<select className="wu-input" value={fastTemplateId} onChange={e=>setFastTemplateId(e.target.value)}>{fastTemplates.map(t=><option key={t.id} value={t.id} disabled={!t.ready}>{t.label} · {t.version.slice(0,8)}{!t.ready?'（未就绪）':''}</option>)}</select></label>}
          <label className="wu-choice">
            <input type="radio" name="tpl" checked={templateId === 'blank'} onChange={() => setTemplateId('blank')} />
            <span><b>空白工程</b> <span className="wu-caption">从话题 / 文章 / 已有脚本开始</span></span>
          </label>
          {tplList.map((t) => (
            <label key={t.id} className="wu-choice">
              <input type="radio" name="tpl" checked={templateId === t.id} onChange={() => setTemplateId(t.id)} />
              <span><b>{t.name}</b> <span className="wu-caption">{t.desc || '模板将带入画幅与声线配置'}</span></span>
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
        open={saveTplOpen}
        onClose={() => setSaveTplOpen(false)}
        title="保存为模板"
        footer={
          <>
            <Button variant="ghost" onClick={() => setSaveTplOpen(false)}>取消</Button>
            <Button
              variant="brand" busy={createTemplate.isPending}
              disabled={!saveTplProjectId || !saveTplName.trim()} onClick={handleSaveTemplate}
            >保存</Button>
          </>
        }
      >
        <p className="wu-muted hf-hint">从所选工程提取可复用配置：画幅与 A/B 声线引用；脚本与产物不进入模板。</p>
        <label className="wu-field">
          <span className="wu-label">来源工程</span>
          <select className="wu-input" aria-label="来源工程" value={saveTplProjectId} onChange={(e) => setSaveTplProjectId(e.target.value)}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.id} · {p.title}</option>)}
          </select>
        </label>
        <label className="wu-field">
          <span className="wu-label">模板名称</span>
          <Input value={saveTplName} onChange={(e) => setSaveTplName(e.target.value)} aria-label="模板名称" placeholder="如：科技周报 · 竖屏版" />
        </label>
        <label className="wu-field">
          <span className="wu-label">简介（可选）</span>
          <Input value={saveTplDesc} onChange={(e) => setSaveTplDesc(e.target.value)} aria-label="模板简介" />
        </label>
        {createTemplate.isError && (
          <div className="wu-alert" data-tone="danger">保存失败：{(createTemplate.error as Error)?.message}</div>
        )}
      </Modal>

      <Modal
        open={newTplOpen}
        onClose={() => setNewTplOpen(false)}
        title="新建模板"
        footer={
          <>
            <Button variant="ghost" onClick={() => setNewTplOpen(false)}>取消</Button>
            <Button
              variant="brand" busy={createTemplate.isPending}
              disabled={!newTplName.trim()} onClick={handleNewTemplate}
            >创建</Button>
          </>
        }
      >
        <p className="wu-muted hf-hint">先建一个只含名称与画幅的空模板；声线引用可稍后从其他工程「保存为模板」补充。</p>
        <label className="wu-field">
          <span className="wu-label">模板名称</span>
          <Input value={newTplName} onChange={(e) => setNewTplName(e.target.value)} aria-label="新模板名称" placeholder="如：读书圈精读" />
        </label>
        <label className="wu-field">
          <span className="wu-label">简介（可选）</span>
          <Input value={newTplDesc} onChange={(e) => setNewTplDesc(e.target.value)} aria-label="新模板简介" />
        </label>
        <div className="wu-field">
          <span className="wu-label">画幅</span>
          <div className="wu-row">
            <Choice type="radio" name="new-tpl-aspect" checked={newTplAspect === 'landscape'} onChange={() => setNewTplAspect('landscape')} label="横屏 16:9" />
            <Choice type="radio" name="new-tpl-aspect" checked={newTplAspect === 'portrait'} onChange={() => setNewTplAspect('portrait')} label="竖屏" />
          </div>
        </div>
        {createTemplate.isError && (
          <div className="wu-alert" data-tone="danger">创建失败：{(createTemplate.error as Error)?.message}</div>
        )}
      </Modal>

      <Modal
        open={!!opsTarget}
        onClose={() => setOpsTarget(null)}
        title={opsTarget ? `${opsTarget.id} · 工程操作` : '工程操作'}
        footer={
          <>
            <Button
              variant="danger" size="sm" icon="trash" busy={deleteProject.isPending}
              disabled={patchProject.isPending} onClick={handleDelete}
              title={confirmDelete ? '再次点击确认删除（软删除，可人工恢复）' : undefined}
            >{confirmDelete ? '确认删除？' : '删除工程'}</Button>
            <span className="hf-spacer" />
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
        {deleteError && (
          <div className="wu-alert" data-tone="danger">删除失败：{deleteError}</div>
        )}
        <div className="wu-alert" data-tone="muted">
          删除为软删除：工程目录移入 storage/trash/，可人工恢复。「打开工程目录」尚未开放。
        </div>
      </Modal>
    </div>
  );
}
