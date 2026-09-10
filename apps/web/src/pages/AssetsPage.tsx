/* ============================================================================
   HF-06 · 资产库（01 §7 / 06 §7.2）
   设计稿语义：素材库 = 角色（形象/性格/多提供方语音绑定）+ 场景 / 同框底图 / 上传素材。
   角色语音绑定取自工程真实 voiceBindings（本地参考音 / MiniMax 可并存）；
   场景与同框底图首版为几何占位，同框底图版本号在有真实变体时跟随后端数据。
   检查器：所选资产 / 语音绑定 / 产物登记（真实 manifest）。
   ========================================================================== */
import { useMemo, useState } from 'react';
import { useArtifacts } from '../lib/api';
import type { ArtifactEntry, Project, VoiceBinding } from '../lib/types';
import { Badge, Button, Icon, type IconName, type Tone } from '../components/wu';
import { AppShell } from '../components/AppShell';
import { useProjectStore } from '../stores';

const KIND_META: Record<ArtifactEntry['kind'], { label: string; icon: IconName; tone: Tone }> = {
  audio: { label: '音频', icon: 'mic', tone: 'info' },
  image: { label: '图片', icon: 'image', tone: 'speaker-b' },
  video: { label: '视频', icon: 'film', tone: 'brand' },
  subtitle: { label: '字幕', icon: 'edit', tone: 'muted' },
  mixed: { label: '合成', icon: 'sparkle', tone: 'success' },
};

interface RoleDef { id: 'A' | 'B'; name: string; desc: string; initial: string; tone: 'a' | 'b'; traits: string; avatar: string }

const ROLES: RoleDef[] = [
  { id: 'A', name: '李雷', desc: '主持 · 男 · 沉稳清晰', initial: '李', tone: 'a', traits: '沉稳 · 主持型 · 语速 1.0', avatar: 'avatar_li_v2.png' },
  { id: 'B', name: '韩梅梅', desc: '嘉宾 · 女 · 温和有说服力', initial: '韩', tone: 'b', traits: '温和 · 嘉宾型 · 语速 1.0', avatar: 'avatar_han_v2.png' },
];

const SCENES = [
  { key: 'studio', name: '录音棚', desc: '16:9 · 主用场景', label: '录音棚', tone: 'studio', fs: 15 },
  { key: 'cafe', name: '咖啡馆', desc: '16:9 · 备用场景', label: '咖啡馆', tone: 'cafe', fs: 15 },
];

type Selection = { kind: 'role'; id: 'A' | 'B' } | { kind: 'scene'; key: string } | { kind: 'artifact'; id: string } | null;

export function AssetsPage({ project }: { project: Project | null }) {
  const artifacts = useArtifacts();
  const setView = useProjectStore((s) => s.setView);
  const [sel, setSel] = useState<Selection>({ kind: 'role', id: 'A' });

  const list: ArtifactEntry[] = artifacts.data?.artifacts ?? [];
  const bindings: VoiceBinding[] = project?.voiceBindings ?? [];
  const latestVariant = project?.visualVariants?.[project.visualVariants.length - 1] ?? null;

  const bindingOf = (id: 'A' | 'B') => bindings.find((b) => b.characterId === id) ?? null;

  const selectedRole = sel?.kind === 'role' ? ROLES.find((r) => r.id === sel.id) ?? null : null;
  const selectedArtifact = sel?.kind === 'artifact' ? list.find((a) => a.artifactId === sel.id) ?? null : null;
  const selectedBindings = useMemo(() => (selectedRole ? bindings.filter((b) => b.characterId === selectedRole.id) : []), [selectedRole, bindings]);

  return (
    <AppShell
      project={project}
      onNavigate={setView}
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>所选资产 <span className="wu-caption" style={{ fontWeight: 400 }}>
              {selectedRole ? selectedRole.name : selectedArtifact ? selectedArtifact.artifactId : sel?.kind === 'scene' ? '场景' : '未选'}
            </span></h4>
            {selectedRole ? (
              <>
                <div className="hf-bigthumb" data-tone={selectedRole.tone}>{selectedRole.initial}</div>
                <div className="hf-fld">
                  <label>性格描述</label>
                  <input className="wu-input" value={selectedRole.traits} readOnly />
                </div>
                <div className="hf-fld">
                  <label>形象</label>
                  <input className="wu-input" value={selectedRole.avatar} readOnly />
                </div>
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

          {selectedRole && (
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
              <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
                本地参考音绑定与 MiniMax voice_id 绑定可并存；切换引擎时保留脚本、形象、场景，只重新生成受影响角色音频。
              </p>
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
              工程复制角色的<b>已选版本</b>；资产库更新<b>不追溯改变历史工程</b>。双人素材优先清晰可见的脸部、少遮挡嘴部的麦克风与可分离的人物区域，背景适度简化。
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
        <Button variant="brand" size="sm" icon="plus" disabled title="接入角色库后端后可新建">新建角色</Button>
        <Button variant="secondary" size="sm" disabled title="接入场景库后端后可新建">新建场景</Button>
        <Button variant="secondary" size="sm" icon="upload" disabled title="接入素材导入后可上传">导入素材</Button>
      </div>

      <div className="hf-body2">
        <div className="hf-sec-t">角色 <span className="wu-caption">保存形象、性格描述、多个语音提供方的独立绑定</span></div>
        <div className="hf-grid">
          {ROLES.map((r) => {
            const b = bindingOf(r.id);
            const active = sel?.kind === 'role' && sel.id === r.id;
            return (
              <div
                key={r.id}
                className={`hf-asset ${active ? 'selected' : ''}`}
                role="button" tabIndex={0} aria-pressed={active}
                onClick={() => setSel({ kind: 'role', id: r.id })}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ kind: 'role', id: r.id }); } }}
              >
                <div className="hf-thumb" data-tone={r.tone}>{r.initial}</div>
                <div className="name">{r.name}</div>
                <div className="desc">{r.desc}</div>
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
          <button type="button" className="hf-plus" disabled title="接入角色库后端后可新建">
            <Icon name="plus" size={20} />
            <span>新建角色</span>
          </button>
        </div>

        <div className="hf-sec-t" style={{ marginTop: 22 }}>场景 / 同框底图 / 上传素材 <span className="wu-caption">可上传或经图片 API 生成</span></div>
        <div className="hf-grid">
          {SCENES.map((s) => (
            <div
              key={s.key}
              className={`hf-asset ${sel?.kind === 'scene' && sel.key === s.key ? 'selected' : ''}`}
              role="button" tabIndex={0}
              aria-pressed={sel?.kind === 'scene' && sel.key === s.key}
              onClick={() => setSel({ kind: 'scene', key: s.key })}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSel({ kind: 'scene', key: s.key }); } }}
            >
              <div className="hf-thumb" data-tone={s.tone} style={{ fontSize: s.fs }}>{s.label}</div>
              <div className="name">{s.name}</div>
              <div className="desc">{s.desc}</div>
            </div>
          ))}
          <div className="hf-asset" role="button" tabIndex={0} onClick={() => latestVariant && setSel({ kind: 'artifact', id: latestVariant.masterImage.artifactId })}>
            <div className="hf-thumb" data-tone="bg" style={{ fontSize: 13, letterSpacing: 1 }}>同框底图</div>
            <div className="name">同框底图 · {latestVariant?.id ?? 'V3'}</div>
            <div className="desc">{latestVariant
              ? `${latestVariant.aspect === 'portrait' ? '9:16' : '16:9'} · ${latestVariant.imageApiConfigRef ?? '图片 API'}`
              : '双人同框 · 16:9 · 图片 API'}</div>
          </div>
          <button type="button" className="hf-plus" disabled title="接入素材导入后可上传">
            <Icon name="upload" size={20} />
            <span>导入素材</span>
          </button>
        </div>

        {list.length === 0 && (
          <div className="hf-sec-t" style={{ marginTop: 22 }}>产物登记 <span className="wu-caption">完成阶段任务后自动登记</span></div>
        )}
      </div>
    </AppShell>
  );
}
