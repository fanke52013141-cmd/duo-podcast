/* ============================================================================
   HF-03 · 阶段③ 试听配音（真实数据流，01 §5 权威）
   主视图：A/B 声音绑定卡 + 合成任务 + 时间轨（unit 层，48kHz 权威样本数）
   检查器：所选单元属性 / 确认配音（approvals 只追加）
   ========================================================================== */
import { useMemo, useRef, useState } from 'react';
import {
  useConfirm, useJobs, useProject, useVoiceSynthesize,
} from '../lib/api';
import type { AudioTimeline, Job, Project, ScriptRevision, SynthesisUnit, VoiceBinding } from '../lib/types';
import { Alert, Badge, Button, Icon, Input, Progress } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const SPEAKERS: { id: 'A' | 'B'; label: string; role: string }[] = [
  { id: 'A', label: 'A · 主持', role: '推进话题 / 提问' },
  { id: 'B', label: 'B · 嘉宾', role: '展开观点 / 举例' },
];

const DEFAULT_MODELS: Record<'A' | 'B', string> = { A: 'CosyVoice2-0.5B', B: 'CosyVoice2-0.5B' };

function defaultBindings(proj: Project): Record<'A' | 'B', VoiceBinding> {
  const fromProj = (spk: 'A' | 'B') => proj.voiceBindings.find((b) => b.characterId === spk);
  return {
    A: fromProj('A') ?? { id: 'VB-A', characterId: 'A', providerProfileId: 'mock-tts', modelId: DEFAULT_MODELS.A, localRefAudio: null, minimaxVoiceId: null, auditionState: 'none' },
    B: fromProj('B') ?? { id: 'VB-B', characterId: 'B', providerProfileId: 'mock-tts', modelId: DEFAULT_MODELS.B, localRefAudio: null, minimaxVoiceId: null, auditionState: 'none' },
  };
}

function fmtDur(samples: number, sampleRate: number): string {
  const secs = sampleRate > 0 ? samples / sampleRate : 0;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function VoicePage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const synth = useVoiceSynthesize();
  const confirm = useConfirm();
  const setView = useProjectStore((s) => s.setView);

  const proj = project.data;
  const [bindings, setBindings] = useState<Record<'A' | 'B', VoiceBinding> | null>(null);
  const [refNames, setRefNames] = useState<Record<'A' | 'B', string>>({ A: '', B: '' });
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<Record<'A' | 'B', HTMLInputElement | null>>({ A: null, B: null });

  const revision: ScriptRevision | undefined =
    proj?.scriptRevisions.find((r) => r.id === proj.currentDraftRevision)
    ?? proj?.scriptRevisions[proj.scriptRevisions.length - 1];
  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;

  // 绑定初始值：工程已有绑定（后端写回）优先，否则默认
  const bs = bindings ?? (proj ? defaultBindings(proj) : null);

  const speakerByTurn = useMemo(() => {
    const m = new Map<string, 'A' | 'B'>();
    revision?.turns.forEach((t) => m.set(t.id, t.speaker));
    return m;
  }, [revision]);

  const turnCount = revision?.turns.length ?? 0;
  const synthJob: Job | undefined = (jobs.data ?? []).find(
    (j) => j.kind === 'tts.synthesize' && ['queued', 'running', 'recovering'].includes(j.status),
  );
  const synthesizing = !!synthJob;
  const voiceApproved = proj?.approvals.some(
    (a) => a.kind === 'voice' && a.decision === 'accepted' && timeline && a.inputRevisionId === timeline.revisionId,
  ) ?? false;

  const bindingsPayload = useMemo(() => {
    if (!bs) return {};
    return Object.fromEntries(
      Object.entries(bs).map(([spk, b]) => [
        spk, { id: b.id, characterId: b.characterId, providerProfileId: b.providerProfileId, modelId: b.modelId },
      ]),
    );
  }, [bs]);

  const gapMs = timeline?.transitionGapMs ?? (turnCount > 1 ? Array(turnCount - 1).fill(320) : []);

  const canSynthesize = !!proj && !!revision && turnCount > 0 && !synthesizing && !voiceApproved;

  const updateBinding = (spk: 'A' | 'B', patch: Partial<VoiceBinding>) => {
    if (!bs) return;
    setBindings({ ...bs, [spk]: { ...bs[spk], ...patch } });
  };

  const handleRefFile = (spk: 'A' | 'B', file: File | undefined) => {
    if (!file) return;
    setRefNames((r) => ({ ...r, [spk]: file.name }));
    updateBinding(spk, { localRefAudio: { artifactId: `ref-${spk.toLowerCase()}-${file.name}`, path: '', fileHash: '' } });
  };

  const handleSynthesize = () => {
    if (!proj || !revision || !bs) return;
    setError(null);
    setSelectedUnitId(null);
    synth.mutate({
      projectId,
      clientToken: crypto.randomUUID(),
      revisionId: revision.id,
      voiceBindings: bindingsPayload,
      transitionGapMs: gapMs,
    }, { onError: (e) => setError((e as Error).message) });
  };

  const handleConfirm = () => {
    if (!proj || !timeline) return;
    confirm.mutate({ projectId, expectedRevision: proj.revision, kind: 'voice', inputRevisionId: timeline.revisionId });
  };

  // ---- 时间轨派生 ----
  const totalSecs = timeline ? fmtDur(timeline.sampleCount, timeline.sampleRate) : '—';
  const units = timeline?.units ?? [];
  const maxSamples = Math.max(1, ...units.map((u) => u.sampleCount));
  const totalSamples = timeline?.sampleCount ?? 0;
  const unitStartSecs = (u: SynthesisUnit) =>
    (timeline?.unitOffsets[u.id] ?? 0) / (timeline?.sampleRate ?? 48000);
  const selectedUnit = units.find((u) => u.id === selectedUnitId) ?? null;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('script')}>上一步</Button>
            <Badge tone={voiceApproved ? 'success' : 'warning'} icon={voiceApproved ? 'check' : 'alert'}>
              确认点 · 配音确认{voiceApproved ? '（已确认）' : ''}
            </Badge>
            <span className="wu-caption">确认后可选自动启动画面生成</span>
            <span className="hf-spacer" />
            <Button size="sm" disabled={!voiceApproved} onClick={() => setView('visual')}>
              下一步<Icon name="arrowRight" size={14} />
            </Button>
          </span>
        }
      />
      }
    >
      <StageHead title="③ 试听配音">
        {revision && <Badge tone="info">脚本 {revision.id} · {turnCount} 条话轮</Badge>}
        {timeline && <Badge tone={voiceApproved ? 'success' : 'warning'}>{timeline.revisionId} · {totalSecs}</Badge>}
        <span className="hf-spacer" />
        <Button
          variant="brand" size="sm" icon="mic" busy={synthesizing} disabled={!canSynthesize}
          onClick={handleSynthesize}
        >
          {voiceApproved ? '已确认 · 重新合成' : synthesizing ? '合成中' : '合成整期'}
        </Button>
      </StageHead>

      <div className="hf-body" style={{ display: 'flex', gap: 20, minHeight: 0 }}>
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
          {error && <Alert tone="danger" >{error}</Alert>}
          {!proj?.scriptRevisions.length && (
            <Alert tone="warning">
              还没有脚本。请先在阶段①创建本期并生成对话。
              <a className="hf-link" onClick={() => setView('script')}>前往阶段② 编辑对话</a>
            </Alert>
          )}
          {voiceApproved && (
            <Alert tone="info">
              配音已确认（{timeline?.revisionId}）。重新合将覆盖当前时间轨并回到待确认；确认后可前往
              <a className="hf-link" onClick={() => setView('visual')}>阶段④ 预览画面</a>。
            </Alert>
          )}

          {/* ---- 声音绑定 ---- */}
          <div className="hf-zone-t">声音绑定<Icon name="mic" size={15} /><span className="wu-caption">每个角色一个绑定；本地参考音频用于克隆音色</span></div>
          {bs && (
            <div className="hf-voice-grid" style={{ marginBottom: 18 }}>
              {SPEAKERS.map((spk) => {
                const b = bs[spk.id];
                return (
                  <div className="hf-voice-card" key={spk.id}>
                    <div className="spk">
                      <Badge tone={spk.id === 'A' ? 'info' : 'speaker-b'}>{spk.label}</Badge>
                      <span className="wu-caption">{spk.role}</span>
                      <span className="hf-spacer" />
                      <Badge tone={b.auditionState === 'passed' ? 'success' : 'muted'}>
                        {b.auditionState === 'passed' ? '试听通过' : '未试听'}
                      </Badge>
                    </div>
                    <div className="hf-fld">
                      <label>提供方</label>
                      <Input aria-label={`${spk.id} 提供方`} value={b.providerProfileId} readOnly />
                    </div>
                    <div className="hf-fld">
                      <label>模型</label>
                      <Input
                        aria-label={`${spk.id} 模型`}
                        value={b.modelId}
                        onChange={(e) => updateBinding(spk.id, { modelId: e.target.value })}
                        placeholder="如 CosyVoice2-0.5B"
                      />
                    </div>
                    <div className="hf-fld">
                      <label>参考音频</label>
                      <div className="wu-row" style={{ gap: 8, minWidth: 0 }}>
                        <Button
                          variant="secondary" size="sm" icon="link"
                          onClick={() => fileInput.current[spk.id]?.click()}
                        >{refNames[spk.id] ? refNames[spk.id] : '选择文件'}</Button>
                        <input
                          ref={(el) => { fileInput.current[spk.id] = el; }}
                          type="file" accept="audio/*" hidden
                          onChange={(e) => handleRefFile(spk.id, e.target.files?.[0])}
                        />
                        <span className="wu-caption" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {b.localRefAudio ? '已绑定（本地引用）' : '未绑定 · 使用提供方默认音色'}
                        </span>
                      </div>
                    </div>
                    <div className="wu-row" style={{ gap: 8 }}>
                      <Button variant="ghost" size="sm" icon="play" disabled title="接入真实 TTS 后可试听">
                        试听{spk.id === 'A' ? '例句' : '例句'}
                      </Button>
                      <span className="wu-caption">示例句朗读 · 试听后绑定状态更新</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* ---- 合成任务 ---- */}
          <div className="hf-zone-t">合成任务<span className="wu-caption">三层对象：话轮 → 合成单元 → 引擎请求；时长权威 = 引擎 sampleCount</span></div>
          {synthJob && (
            <Alert tone="info">
              正在合成 {synthJob.kind} · {synthJob.stage} 阶段
              <div style={{ marginTop: 8 }}>
                <Progress value={synthJob.stage === 'post' ? 90 : synthJob.stage === 'infer' ? 55 : 25} />
              </div>
            </Alert>
          )}
          {!synthJob && !timeline && (
            <Alert tone="muted">尚未合成时间轨。上方点击「合成整期」后，将逐单元请求 TTS 并累积整数样本偏移。</Alert>
          )}

          {/* ---- 时间轨 ---- */}
          {timeline && (
            <>
              <div className="wu-row" style={{ gap: 10, margin: '10px 0 8px' }}>
                <Badge tone="info">权威总长 {totalSamples.toLocaleString()} 样本</Badge>
                <Badge tone="muted">{timeline.sampleRate}Hz · {totalSecs}</Badge>
                <Badge tone="muted">{units.length} 单元</Badge>
                <span className="hf-spacer" />
                <span className="wu-caption">unit 起始 = 整数样本偏移；点击单元查看属性</span>
              </div>
              <div className="hf-timeline" role="list" aria-label="音频时间轨">
                {units.map((u) => {
                  const spk = speakerByTurn.get(u.turnId) ?? 'A';
                  const pct = Math.max(4, Math.round((u.sampleCount / maxSamples) * 100));
                  return (
                    <div
                      key={u.id}
                      role="listitem"
                      className="hf-unit"
                      data-spk={spk}
                      data-selected={u.id === selectedUnitId || undefined}
                      style={u.id === selectedUnitId ? { outline: '1px solid var(--wu-semantic-brand)', outlineOffset: -1 } : undefined}
                      onClick={() => setSelectedUnitId(u.id)}
                    >
                      <span className="hf-mono" style={{ minWidth: 30 }}>{u.id}</span>
                      <Badge tone={spk === 'A' ? 'info' : 'speaker-b'}>{spk}</Badge>
                      <span className="bar"><i style={{ width: `${pct}%` }} /></span>
                      <span className="hf-mono" style={{ minWidth: 56, textAlign: 'right' }}>
                        {unitStartSecs(u).toFixed(1)}s · {fmtDur(u.sampleCount, timeline.sampleRate)}
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="wu-caption" style={{ marginTop: 6 }}>
                转场间隔（transitionGapMs）：{gapMs.length ? gapMs.join(' / ') : '—'} ms；合成后单元候选音频登记为不可变资产。
              </p>
            </>
          )}
        </div>

        {/* ---- 检查器 ---- */}
        <aside className="hf-inspector" aria-label="检查器" style={{ position: 'sticky', top: 0, height: 'fit-content' }}>
          <div className="hf-ins-sec">
            <h4>所选单元 <span className="wu-caption" style={{ fontWeight: 400 }}>{selectedUnit ? selectedUnit.id : '未选'}</span></h4>
            {selectedUnit ? (
              <div className="hf-detail-grid">
                <div className="hf-kv"><b>话轮</b><span className="hf-mono">{selectedUnit.turnId}</span></div>
                <div className="hf-kv"><b>台词行</b><span className="hf-mono">{selectedUnit.lineIds.join(', ')}</span></div>
                <div className="hf-kv"><b>绑定</b><span>{selectedUnit.voiceBindingId}</span></div>
                <div className="hf-kv"><b>情绪</b><span>{String(selectedUnit.emotion?.label ?? '自然')}</span></div>
                <div className="hf-kv"><b>语速</b><span>{selectedUnit.speedRatio.toFixed(2)}</span></div>
                <div className="hf-kv"><b>样本数</b><span className="hf-mono">{selectedUnit.sampleCount.toLocaleString()}</span></div>
                <div className="hf-kv"><b>候选资产</b><span className="hf-mono" style={{ wordBreak: 'break-all' }}>{selectedUnit.candidateAudioAssetIds.length} 份 · {selectedUnit.candidateAudioAssetIds[0] ?? '—'}</span></div>
              </div>
            ) : (
              <p className="wu-caption">点击时间轨中的单元查看引擎请求属性；候选音频在合成时登记。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>确认配音</h4>
            {timeline ? (
              <>
                <p className="wu-caption" style={{ lineHeight: 1.6, marginBottom: 10 }}>
                  确认将锁定 {timeline.revisionId} 的时间轨作为后续画面与渲染输入；确认后重新合成会回到待确认。
                </p>
                <Button
                  variant="brand" size="sm" icon="check" disabled={voiceApproved}
                  onClick={handleConfirm}
                >{voiceApproved ? '配音已确认' : '确认配音'}</Button>
              </>
            ) : (
              <p className="wu-caption">先合成时间轨（至少 1 条话轮）后才能确认。</p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>依赖提示</h4>
            <ul className="hf-ins-list">
              <li>阶段② 脚本确认：{proj?.approvals.some((a) => a.kind === 'script' && a.decision === 'accepted') ? '已通过' : '未确认'}</li>
              <li>时间轨过期：脚本话轮改动后需重新合成</li>
              <li>音频资产不可变：每次合成为新 artifactId</li>
            </ul>
          </div>
        </aside>
      </div>
    </AppShell>
  );
}
