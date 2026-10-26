/* ============================================================================
   HF-06 · 资产库（01 §7 / 06 §7.2）
   角色已接全局角色库（GET/POST /api/characters + 图片上传）：
   卡片显示真实形象图；支持新建角色与上传/更换形象。
   场景与同框底图区仍为占位（场景库后端未建）；产物登记 = 真实 manifest。
   检查器：所选资产 / 语音绑定 / 产物登记。
   ========================================================================== */
import { useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '../lib/api';
import {
  useArtifacts, useCharacters, useCreateCharacter, useUploadCharacterImage,
} from '../lib/api';
import type { ArtifactEntry, Character, Project, VoiceBinding } from '../lib/types';
import { Badge, Button, Icon, Input, Modal, type IconName, type Tone } from '../components/wu';
import { AppShell } from '../components/AppShell';
import { useProjectStore } from '../stores';

const KIND_META: Record<ArtifactEntry['kind'], { label: string; icon: IconName; tone: Tone }> = {
  audio: { label: '音频', icon: 'mic', tone: 'info' },
  image: { label: '图片', icon: 'image', tone: 'speaker-b' },
  video: { label: '视频', icon: 'film', tone: 'brand' },
  subtitle: { label: '字幕', icon: 'edit', tone: 'muted' },
  mixed: { label: '合成', icon: 'sparkle', tone: 'success' },
};

type Selection = { kind: 'character'; id: string } | { kind: 'scene'; key: string } | { kind: 'artifact'; id: string } | null;

function CharacterThumb({ c, big }: { c: Character; big?: boolean }) {
  if (c.imagePath) {
    return (
      <img
      src={c.imagePath} alt={c.name}
      className={big ? 'hf-bigthumb' : 'hf-thumb'}
      style={{ objectFit: 'cover', padding: 0 }}
      onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
    />
    );
  }
  return <div className={big ? 'hf-bigthumb' : 'hf-thumb'} data-tone={c.speaker === 'A' ? 'a' : 'b'}>{c.name.slice(0, 1)}</div>;
}

export function AssetsPage({ project }: { project: Project | null }) {
  const qc = useQueryClient();
  const scenes = useQuery({queryKey:['scenes'], queryFn:()=>getJson<{id:string;name:string}[]>('/scenes')});
  const sceneFile = useRef<HTMLInputElement>(null);
  const bind = async (characterId: string, speaker: 'A'|'B') => {
    if (!project) return;
    try { await sendJson(`/projects/${project.id}/characters/${speaker}`, 'POST', {characterId,expectedRevision:project.revision}); await qc.invalidateQueries({queryKey:['project',project.id]}); }
    catch(e) { setNewError((e as Error).message); }
  };
  const artifacts = useArtifacts();
  const charactersQ = useCharacters();
  const createCharacter = useCreateCharacter();
  const uploadImage = useUploadCharacterImage();
  const setView = useProjectStore((s) => s.setView);
  const [sel, setSel] = useState<Selection>({ kind: 'character', id: 'char-lilei' });
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newSpeaker, setNewSpeaker] = useState<'A' | 'B'>('A');
  const [newRole, setNewRole] = useState('主持');
  const [newTraits, setNewTraits] = useState('');
  const [newError, setNewError] = useState<string | null>(null);
  const inspectorFileRef = useRef<HTMLInputElement | null>(null);

  const characters: Character[] = charactersQ.data ?? [];
  const list: ArtifactEntry[] = artifacts.data?.artifacts ?? [];
  const bindings: VoiceBinding[] = project?.voiceBindings ?? [];
  const latestVariant = project?.visualVariants?.[project.visualVariants.length - 1] ?? null;

  const selectedCharacter = sel?.kind === 'character'
    ? characters.find((c) => c.id === sel.id) ?? characters[0] ?? null
    : null;
  const selectedArtifact = sel?.kind === 'artifact' ? list.find((a) => a.artifactId === sel.id) ?? null : null;
  const selectedBindings = useMemo(() => (selectedCharacter
    ? bindings.filter((b) => b.characterId === selectedCharacter.speaker)
    : []), [selectedCharacter, bindings]);

  const handleCreate = async () => {
    setNewError(null);
    if (!newName.trim()) { setNewError('角色名称不能为空'); return; }
    try {
      const created = await createCharacter.mutateAsync({
      name: newName.trim(), speaker: newSpeaker, role: newRole.trim(), traits: newTraits.trim(),
      desc: `${newRole.trim()} · ${newSpeaker === 'A' ? '男' : '女'}`,
      });
      setSel({ kind: 'character', id: created.id });
      setNewOpen(false);
      setNewName(''); setNewTraits(''); setNewRole('主持'); setNewSpeaker('A');
    } catch (e) {
      setNewError((e as Error).message);
    }
  };

  const handlePickImage = async (characterId: string, file: File | undefined) => {
    if (!file) return;
    try {
      await uploadImage.mutateAsync({ characterId, file });
    } catch (e) {
      setNewError(`形象上传失败：${(e as Error).message}`);
    }
  };

  return (
    <AppShell
      project={project}
      onNavigate={setView}
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>所选资产 <span className="wu-caption" style={{ fontWeight: 400 }}>
              {selectedCharacter ? selectedCharacter.name : selectedArtifact ? selectedArtifact.artifactId : sel?.kind === 'scene' ? '场景' : '未选'}
            </span></h4>
            {selectedCharacter ? (
              <>
                <CharacterThumb c={selectedCharacter} big />
                <div className="hf-fld">
                  <label>性格描述</label>
                  <input className="wu-input" value={selectedCharacter.traits || '—'} readOnly />
                </div>
                <div className="hf-fld">
                  <label>形象</label>
                  <input className="wu-input" value={selectedCharacter.imagePath || '（未上传形象图）'} readOnly />
                </div>
                <div className="wu-row" style={{ gap: 8, marginTop: 6 }}>
                  <Button
                  variant="secondary" size="sm" icon="upload" busy={uploadImage.isPending}
                  onClick={() => inspectorFileRef.current?.click()}
                  >{selectedCharacter.imagePath ? '更换形象' : '上传形象'}</Button>
                  <input
                  ref={inspectorFileRef} type="file" accept="image/png,image/jpeg" style={{ display: 'none' }}
                  onChange={(e) => { handlePickImage(selectedCharacter.id, e.target.files?.[0]); e.target.value = ''; }}
                  />
                </div>
                {project && <div className="wu-row">{(['A','B'] as const).map(s=><Button key={s} disabled={project.generationMode==='fast'} onClick={()=>void bind(selectedCharacter.id,s)}>用于本期 {s}{project.characterBindings?.[s]?.id===selectedCharacter.id?'（已绑定）':''}</Button>)}</div>}
                <label>参考音频（上传后再次绑定本期以采用新声音）<input type="file" accept="audio/*" onChange={async e=>{
                  const file=e.target.files?.[0]; if(!file)return;
                  try { const form=new FormData();form.append('file',file); const response=await fetch('/api/uploads/audio',{method:'POST',body:form});const result=await response.json();if(!response.ok)throw new Error(result.detail??'上传失败');await sendJson(`/characters/${selectedCharacter.id}/voice`,'POST',{artifactId:result.artifactId});await qc.invalidateQueries({queryKey:['characters']});setNewError('参考音频已保存；点击用于本期以更新本期声音。'); }catch(error){setNewError((error as Error).message);}
                }}/></label>
                {newError && <p role="status" className="wu-caption">{newError}</p>}
              </>
            ) : selectedArtifact ? (
              <>
                <div className="hf-coords"><span>类型</span><b>{KIND_META[selectedArtifact.kind].label}</b></div>
                <div className="hf-coords"><span>版本</span><b>{selectedArtifact.workflowVersion ?? '—'}</b></div>
                <div className="hf-coords"><span>文件哈希</span><b>{selectedArtifact.fileHash || '—'}</b></div>
                <div className="hf-coords"><span>依赖哈希</span><b>{selectedArtifact.dependencyHash || '—'}</b></div>
                <div className="hf-coords"><span>路径</span><b>{selectedArtifact.path || '（占位）'}</b></div>
              </>
            ) : (
              <p className="wu-caption" style={{ lineHeight: 1.7 }}>
                点击卡片查看资产详情。角色保存形象、性格描述与多语音提供方的独立绑定；场景可上传或经图片 API 生成。
              </p>
            )}
          </div>

          {selectedCharacter && (
            <div className="hf-ins-sec">
              <h4>语音绑定 <span className="wu-caption" style={{ fontWeight: 400 }}>独立并存，不保证声音完全相同</span></h4>
              <div className="hf-bindrow">
                <b>本地参考音</b>
                <span>{selectedBindings[0]?.localRefAudio ? '已绑定 · 本地引用' : '未绑定'}</span>
                <span className="hf-spacer" />
                <Button variant="ghost" size="sm" disabled>试听</Button>
              </div>
              <div className="hf-bindrow">
                <b>MiniMax</b>
                <span className="hf-mono">{selectedBindings[0]?.minimaxVoiceId ?? 'voice_id 未配置'}</span>
                <span className="hf-spacer" />
                <Button variant="ghost" size="sm" disabled>试听</Button>
              </div>
              <p className="wu-caption" style={{ marginTop: 8 }}>切换引擎只重做受影响角色的音频。</p>
            </div>
          )}

          <div className="hf-ins-sec">
            <h4>产物登记 <span className="wu-caption" style={{ fontWeight: 400 }}>共 {list.length} 项</span></h4>
            {list.length ? (
              <div className="hf-ins-rows">
                {list.slice(0, 6).map((a) => (
                  <button
                  key={a.artifactId} type="button" className="hf-bindrow"
                  style={{ background: 'none', border: 'none', borderBottom: '1px solid var(--wu-semantic-border)', width: '100%', textAlign: 'left', cursor: 'pointer', padding: '7px 0' }}
                  onClick={() => setSel({ kind: 'artifact', id: a.artifactId })}
                  >
                    <b className="hf-mono">{a.artifactId}</b>
                    <span>{KIND_META[a.kind].label}</span>
                    <span className="hf-spacer" />
                    <span className="wu-caption">{a.workflowVersion ?? 'v0.1'}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="wu-caption">完成阶段任务后，脚本 / 音频 / 主图 / 成片将登记在此处。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>工程引用</h4>
            <p className="wu-caption" style={{ lineHeight: 1.7 }}>
              资产库更新不追溯改变历史工程；双人素材优先清晰正脸、少遮挡。
            </p>
          </div>
        </aside>
      }
      /* 独立全屏入口：设计稿 HF-06 无 FootBar（非阶段流程），传 null 显式关闭 */
      footer={null}
    >
      <div className="hf-h">
        <h2>资产库</h2>
        <span className="wu-caption">角色不再是每期必经步骤 · 独立入口</span>
        <span className="hf-spacer" />
        <Button variant="brand" size="sm" icon="plus" onClick={() => { setNewError(null); setNewOpen(true); }}>新建角色</Button>
        <Button variant="secondary" size="sm" disabled title="接入场景库后端后可新建">新建场景</Button>
        <Button variant="secondary" size="sm" icon="upload" disabled title="接入素材导入后可上传">导入素材</Button>
      </div>

      <div className="hf-body2">
        <div className="hf-sec-t">角色 <span className="wu-caption">保存形象、性格描述、多个语音提供方的独立绑定</span></div>
        <div className="hf-grid">
          {characters.map((c) => {
            const active = sel?.kind === 'character' && sel.id === c.id;
            const b = bindings.find((x) => x.characterId === c.speaker);
            return (
              <div
              key={c.id}
              className={`hf-asset ${active ? 'selected' : ''}`}
              role="button" tabIndex={0} aria-pressed={active}
              onClick={() => setSel({ kind: 'character', id: c.id })}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ kind: 'character', id: c.id }); } }}
              >
                <CharacterThumb c={c} />
                <div className="name">{c.name}</div>
                <div className="desc">{c.desc || `${c.role} · ${c.speaker === 'A' ? '男' : '女'}`}</div>
                <div className="bind">
                  {b?.localRefAudio
                  ? <Badge tone="success" icon="mic">本地</Badge>
                  : <Badge tone="muted" icon="mic">本地</Badge>}
                  {b?.minimaxVoiceId
                  ? <Badge tone="success" icon="mic">MiniMax</Badge>
                  : <Badge tone="muted" icon="mic">MiniMax</Badge>}
                </div>
              </div>
            );
          })}
          <button type="button" className="hf-plus" onClick={() => { setNewError(null); setNewOpen(true); }}>
            <Icon name="plus" size={20} />
            <span>新建角色</span>
          </button>
        </div>

        <div className="hf-sec-t" style={{ marginTop: 22 }}>场景 / 同框底图 / 上传素材 <span className="wu-caption">可上传或经图片 API 生成</span></div>
        <div className="hf-grid">
          {(scenes.data??[]).map((s) => (
            <div
            key={s.id}
            className={`hf-asset ${sel?.kind === 'scene' && sel.key === s.id ? 'selected' : ''}`}
            role="button" tabIndex={0}
            aria-pressed={sel?.kind === 'scene' && sel.key === s.id}
            onClick={() => setSel({ kind: 'scene', key: s.id })}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ kind: 'scene', key: s.id }); } }}
            >
              <img className="hf-thumb" alt={s.name} src={`/api/artifacts/${s.id}/file`}/>
              <div className="name">{s.name}</div>
              <div className="desc">全局场景素材 · 上传双人底图可直接使用</div>
              {project&&<Button disabled={project.generationMode==='fast'} onClick={async e=>{e.stopPropagation();try{const result=await sendJson<{notice:string}>(`/projects/${project.id}/visual/use-scene`,'POST',{sceneId:s.id,expectedRevision:project.revision});setNewError(result.notice);await qc.invalidateQueries({queryKey:['project',project.id]});}catch(error){setNewError((error as Error).message);}}}>用于本期底图</Button>}
            </div>
          ))}
          <div className="hf-asset" role="button" tabIndex={0} onClick={() => latestVariant && setSel({ kind: 'artifact', id: latestVariant.masterImage.artifactId })}>
            <div className="hf-thumb" data-tone="bg" style={{ fontSize: 13, letterSpacing: 1 }}>同框底图</div>
            <div className="name">同框底图{latestVariant ? ` · ${latestVariant.id}` : ' · 尚未生成'}</div>
            <div className="desc">{latestVariant
              ? `${latestVariant.aspect === 'portrait' ? '9:16' : '16:9'} · ${latestVariant.imageApiConfigRef ?? '图片 API'}`
              : '在阶段④「生成同框底图」后在此登记'}</div>
          </div>
          <input ref={sceneFile} type="file" aria-label="上传场景素材" accept="image/png,image/jpeg" hidden onChange={async e=>{const file=e.target.files?.[0];e.target.value='';if(!file)return;try{const form=new FormData();form.append('file',file);const response=await fetch('/api/scenes',{method:'POST',body:form});const result=await response.json();if(!response.ok)throw new Error(result.detail??'上传失败');await qc.invalidateQueries({queryKey:['scenes']});setNewError('场景已保存，含人物的双人底图可直接用于本期。');}catch(error){setNewError((error as Error).message);}}}/>
          <button type="button" className="hf-plus" onClick={()=>sceneFile.current?.click()}>
            <Icon name="upload" size={20} />
            <span>上传场景 / 双人底图</span>
          </button>
        </div>

        {newError && <p role="status">{newError}</p>}
        {list.length === 0 && (
          <div className="hf-sec-t" style={{ marginTop: 22 }}>产物登记 <span className="wu-caption">完成阶段任务后自动登记</span></div>
        )}
      </div>

      <Modal
      open={newOpen} onClose={() => setNewOpen(false)} title="新建角色" width={460}
      footer={
        <div className="wu-row" style={{ gap: 8 }}>
          <span className="wu-caption">角色入全局资产库，所有工程可用</span>
          <span className="hf-spacer" />
          <Button variant="ghost" onClick={() => setNewOpen(false)}>取消</Button>
          <Button variant="brand" busy={createCharacter.isPending} onClick={handleCreate}>创建</Button>
        </div>
      }
      >
        <div className="hf-fld">
          <label>名称</label>
          <Input placeholder="如：王小雅" value={newName} onChange={(e) => setNewName(e.target.value)} />
        </div>
        <div className="hf-fld">
          <label>声道 / 定位</label>
          <div className="wu-row" style={{ gap: 8 }}>
            <Button variant={newSpeaker === 'A' ? 'secondary' : 'ghost'} size="sm" onClick={() => { setNewSpeaker('A'); setNewRole('主持'); }}>A · 主持</Button>
            <Button variant={newSpeaker === 'B' ? 'secondary' : 'ghost'} size="sm" onClick={() => { setNewSpeaker('B'); setNewRole('嘉宾'); }}>B · 嘉宾</Button>
          </div>
        </div>
        <div className="hf-fld">
          <label>性格描述</label>
          <Input placeholder="如：活泼 · 举例型 · 语速 1.1" value={newTraits} onChange={(e) => setNewTraits(e.target.value)} />
        </div>
        {newError && <p className="wu-caption" style={{ color: 'var(--wu-semantic-danger)' }}>{newError}</p>}
        <p className="wu-caption">形象图可在创建后于检查器中上传（PNG / JPEG，≤10MB）。</p>
      </Modal>
    </AppShell>
  );
}
