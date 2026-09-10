/* ============================================================================
   HF-06 · 资产库（真实数据流，06 §7.2 manifest 展示）
   主视图：不可变资产网格 + 类型筛选；选中资产查看依赖哈希与参数快照
   ========================================================================== */
import { useMemo, useState } from 'react';
import { useArtifacts } from '../lib/api';
import type { ArtifactEntry, Project } from '../lib/types';
import { Badge, Button, Icon, type IconName, type Tone } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const KIND_META: Record<ArtifactEntry['kind'], { label: string; icon: IconName; tone: Tone }> = {
  audio: { label: '音频', icon: 'mic', tone: 'info' },
  image: { label: '图片', icon: 'image', tone: 'speaker-b' },
  video: { label: '视频', icon: 'film', tone: 'brand' },
  subtitle: { label: '字幕', icon: 'edit', tone: 'muted' },
  mixed: { label: '合成', icon: 'sparkle', tone: 'success' },
};

const FILTERS: (ArtifactEntry['kind'] | 'all')[] = ['all', 'audio', 'image', 'video', 'subtitle', 'mixed'];

export function AssetsPage({ project }: { project: Project | null }) {
  const artifacts = useArtifacts();
  const setView = useProjectStore((s) => s.setView);
  const [kind, setKind] = useState<ArtifactEntry['kind'] | 'all'>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list: ArtifactEntry[] = artifacts.data?.artifacts ?? [];
  const filtered = useMemo(
    () => (kind === 'all' ? list : list.filter((a) => a.kind === kind)),
    [list, kind],
  );
  const selected = list.find((a) => a.artifactId === selectedId) ?? null;

  return (
    <AppShell
      project={project}
      onNavigate={setView}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Badge tone="muted">不可变产物</Badge>
            <span className="wu-caption">共 {list.length} 项 · 资产一经登记不覆写，依赖哈希驱动复用</span>
          </span>
        }
      />
      }
    >
      <StageHead title="资产库">
        <span className="hf-seg" role="group" aria-label="资产类型筛选" style={{ height: 30 }}>
          {FILTERS.map((k) => (
            <button
              key={k} type="button" aria-pressed={kind === k}
              style={{ width: 'auto', padding: '0 12px' }}
              onClick={() => { setKind(k); setSelectedId(null); }}
            >{k === 'all' ? '全部' : KIND_META[k].label}</button>
          ))}
        </span>
        <span className="hf-spacer" />
        <span className="wu-caption">缓存命中即复用，不重复推理与计费</span>
      </StageHead>

      <div className="hf-body" style={{ display: 'flex', gap: 20, minHeight: 0 }}>
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
          {filtered.length === 0 ? (
            <div className="wu-empty">
              <div style={{ fontWeight: 700 }}>暂无{kind === 'all' ? '' : KIND_META[kind].label}资产</div>
              <p className="wu-caption" style={{ margin: '6px auto 14px', maxWidth: 420 }}>
                完成阶段任务后，脚本 / 音频 / 主图 / 成片将登记在此处。
              </p>
              <Button variant="secondary" size="sm" onClick={() => setView('create')}>返回创建本期</Button>
            </div>
          ) : (
            <div className="hf-asset-grid">
              {filtered.map((a) => {
                const m = KIND_META[a.kind];
                return (
                  <div
                    key={a.artifactId}
                    className="hf-asset"
                    role="button"
                    tabIndex={0}
                    aria-pressed={selected?.artifactId === a.artifactId}
                    style={selected?.artifactId === a.artifactId ? { outline: '1px solid var(--wu-semantic-brand)', outlineOffset: 1 } : undefined}
                    onClick={() => setSelectedId(a.artifactId)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedId(a.artifactId); } }}
                  >
                    <div className="thumb"><Icon name={m.icon} size={18} />{m.label}</div>
                    <div className="meta">
                      <b className="hf-mono" style={{ wordBreak: 'break-all' }}>{a.artifactId}</b>
                      <span>{a.workflowVersion ?? 'v0.1'}</span>
                      <span className="hf-mono" style={{ wordBreak: 'break-all' }}>{a.fileHash}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <aside className="hf-inspector" aria-label="检查器" style={{ position: 'sticky', top: 0, height: 'fit-content' }}>
          <div className="hf-ins-sec">
            <h4>所选资产 <span className="wu-caption" style={{ fontWeight: 400 }}>{selected ? selected.artifactId : '未选'}</span></h4>
            {selected ? (
              <div className="hf-detail-grid">
                <div className="hf-kv"><b>类型</b><Badge tone={KIND_META[selected.kind].tone}>{KIND_META[selected.kind].label}</Badge></div>
                <div className="hf-kv"><b>版本</b><span>{selected.workflowVersion ?? '—'}</span></div>
                <div className="hf-kv"><b>文件哈希</b><span className="hf-mono" style={{ wordBreak: 'break-all' }}>{selected.fileHash}</span></div>
                <div className="hf-kv"><b>依赖哈希</b><span className="hf-mono" style={{ wordBreak: 'break-all' }}>{selected.dependencyHash}</span></div>
                <div className="hf-kv"><b>路径</b><span className="hf-mono" style={{ wordBreak: 'break-all' }}>{selected.path || '（占位）'}</span></div>
                <div className="hf-kv" style={{ alignItems: 'flex-start' }}><b>参数</b><span style={{ wordBreak: 'break-all' }}>{JSON.stringify(selected.paramsSnapshot)}</span></div>
              </div>
            ) : (
              <p className="wu-caption">点击资产卡片查看不可变登记信息；相同依赖哈希命中缓存时将复用产物。</p>
            )}
          </div>
        </aside>
      </div>
    </AppShell>
  );
}
