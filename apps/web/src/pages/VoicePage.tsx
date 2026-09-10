/* ============================================================================
   HF-03 · 阶段③ 试听配音（真实数据流，01 §5 权威）
   主视图：左 语音引擎面板（固定 300px，引擎单选 + A/B 音色绑定）/ 右 配音清单（话轮级）
   底部：时长检查条 + TimelineBar（全应用唯一深色面，05 §1.5）
   检查器：所选发言 / 间隔调整（transitionGapMs）/ 高级折叠
   ========================================================================== */
import { useMemo, useRef, useState } from 'react';
import {
  useConfirm, useJobs, useProject, useVoiceSynthesize,
} from '../lib/api';
import type { AudioTimeline, Job, Project, ScriptRevision, SynthesisUnit, Turn, VoiceBinding } from '../lib/types';
import { Alert, Badge, Button, Choice, Icon } from '../components/wu';
import { AppShell, FootBar, StageHead, TimelineBar, fmtDur as sharedFmtDur } from '../components/AppShell';
import { useProjectStore } from '../stores';

const SPEAKERS: { id: 'A' | 'B'; name: string; role: string }[] = [
  { id: 'A', name: '李雷', role: '主持' },
  { id: 'B', name: '韩梅梅', role: '嘉宾' },
];

const ENGINES: { id: 'local' | 'minimax'; label: string; profileId: string }[] = [
  { id: 'local', label: '本地引擎（默认）', profileId: 'mock-tts' },
  { id: 'minimax', label: 'MiniMax', profileId: 'minimax' },
];

const VOICE_OPTIONS: Record<'A' | 'B', string[]> = {
  A: ['CosyVoice2-0.5B · 参考音', 'CosyVoice2-0.5B · 增强'],
  B: ['CosyVoice2-0.5B · 参考音', 'CosyVoice2-0.5B · 低沉'],
};

const GAP_MAX_MS = 1200;

function turnText(t: Turn): string {
  return t.lines.map((l) => l.displayText).join('');
}

/** 字面读音提示：与阶段② 编辑对话的判定口径保持一致（缩写、数字+量词） */
function pronTerms(t: Turn): string[] {
  return turnText(t).match(/[A-Z]{2,}|[\d]+(?=[%％:]|(?:个|分|秒|亿|万|元|张|倍))/g) ?? [];
}

function defaultBindings(proj: Project): Record<'A' | 'B', VoiceBinding> {
  const fromProj = (spk: 'A' | 'B') => proj.voiceBindings.find((b) => b.characterId === spk);
  return {
    A: fromProj('A') ?? { id: 'VB-A', characterId: 'A', providerProfileId: 'mock-tts', modelId: VOICE_OPTIONS.A[0], localRefAudio: null, minimaxVoiceId: null, auditionState: 'none' },
    B: fromProj('B') ?? { id: 'VB-B', characterId: 'B', providerProfileId: 'mock-tts', modelId: VOICE_OPTIONS.B[0], localRefAudio: null, minimaxVoiceId: null, auditionState: 'none' },
  };
}

function fmtDur(samples: number, sampleRate: number): string {
  return sharedFmtDur(samples, sampleRate);
}

export function VoicePage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  const jobs = useJobs(projectId);
  const synth = useVoiceSynthesize();
  const confirm = useConfirm();
  const setView = useProjectStore((s) => s.setView);

  const proj = project.data;
  const [bindings, setBindings] = useState<Record<'A' | 'B', VoiceBinding> | null>(null);
  const [engine, setEngine] = useState<'local' | 'minimax'>('local');
  const [refNames, setRefNames] = useState<Record<'A' | 'B', string>>({ A: '', B: '' });
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);
  const [gapOverride, setGapOverride] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<Record<'A' | 'B', HTMLInputElement | null>>({ A: null, B: null });

  const revision: ScriptRevision | undefined =
    proj?.scriptRevisions.find((r) => r.id === proj.currentDraftRevision)
    ?? proj?.scriptRevisions[proj.scriptRevisions.length - 1];
  const timeline: AudioTimeline | null = proj?.audioTimeline ?? null;

  // 绑定初始值：工程已有绑定（后端写回）优先，否则默认
  const bs = bindings ?? (proj ? defaultBindings(proj) : null);

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

  const defaultGaps = timeline?.transitionGapMs ?? (turnCount > 1 ? Array(turnCount - 1).fill(320) : []);
  const gapMs = gapOverride !== null
    ? defaultGaps.map((v, i) => (i === 0 ? gapOverride : v))
    : defaultGaps;

  const canSynthesize = !!proj && !!revision && turnCount > 0 && !synthesizing && !voiceApproved;

  const updateBinding = (spk: 'A' | 'B', patch: Partial<VoiceBinding>) => {
    if (!bs) return;
    setBindings({ ...bs, [spk]: { ...bs[spk], ...patch } });
  };

  const pickEngine = (id: 'local' | 'minimax') => {
    setEngine(id);
    const profile = ENGINES.find((e) => e.id === id)?.profileId ?? 'mock-tts';
    if (!bs) return;
    setBindings({
      A: { ...bs.A, providerProfileId: profile },
      B: { ...bs.B, providerProfileId: profile },
    });
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
  const units = timeline?.units ?? [];
  const unitByTurn = useMemo(() => {
    const m = new Map<string, SynthesisUnit>();
    units.forEach((u) => { if (!m.has(u.turnId)) m.set(u.turnId, u); });
    return m;
  }, [units]);
  const totalSecs = timeline ? fmtDur(timeline.sampleCount, timeline.sampleRate) : '待合成';
  const selectedUnit = units.find((u) => u.id === selectedUnitId) ?? null;
  const turns = revision?.turns ?? [];
  const selectedTurnIdx = selectedUnit
    ? turns.findIndex((t) => t.id === selectedUnit.turnId)
    : -1;
  const gapIdx = selectedTurnIdx > 0 ? selectedTurnIdx - 1 : 0;
  const gapValue = gapMs[gapIdx] ?? 320;
  const gapPct = Math.round((gapValue / GAP_MAX_MS) * 100);
  const prevTurnLabel = turns[gapIdx]?.id ?? '—';
  const nextTurnLabel = turns[gapIdx + 1]?.id ?? '—';

  // 播放头：以当前所选合成单元的起始样本位置近似（真播放由 WaveSurfer 接管前的最小实现）
  const playheadSamples = selectedUnit ? (timeline?.unitOffsets[selectedUnit.id] ?? 0) : 0;
  const playPct = timeline && timeline.sampleCount > 0 ? (playheadSamples / timeline.sampleCount) * 100 : 0;

  return (
    <AppShell
      project={proj ?? null}
      onNavigate={setView}
      timeline={
        <TimelineBar
          timeline={timeline}
          curSecs={timeline ? fmtDur(playheadSamples, timeline.sampleRate) : '00:00.0'}
          labels={[`A 轨（${SPEAKERS[0].name}）`, `B 轨（${SPEAKERS[1].name}）`]}
          speakerOf={(turnId) => revision?.turns.find((t) => t.id === turnId)?.speaker ?? 'A'}
          playPct={playPct}
        />
      }
      inspector={
        <aside className="hf-inspector" aria-label="检查器">
          <div className="hf-ins-sec">
            <h4>所选发言 <span className="wu-caption" style={{ fontWeight: 400 }}>{selectedUnit ? `#${selectedUnit.turnId}` : '未选'}</span></h4>
            {selectedUnit ? (
              <>
                <div className="hf-unit">
                  <b>{selectedUnit.id}</b>
                  <span>合成单元 · {selectedUnit.voiceBindingId}</span>
                  <span className="hf-spacer" />
                  <span className="wu-caption">时长 {timeline ? fmtDur(selectedUnit.sampleCount, timeline.sampleRate) : '待定'}</span>
                </div>
                <div className="hf-unit">
                  <b>{selectedUnit.turnId}</b>
                  <span>自然发言 · {revision?.turns.find((t) => t.id === selectedUnit.turnId)?.speaker ?? 'A'} · {String(selectedUnit.emotion?.label ?? '自然')}</span>
                  <span className="hf-spacer" />
                  <span className="wu-caption">{selectedUnit.lineIds.length} 个句子</span>
                </div>
                <div className="hf-playrow" style={{ borderTop: 'none', paddingTop: 4 }}>
                  <Button variant="secondary" size="sm" disabled>重新生成</Button>
                  <Button variant="secondary" size="sm" disabled>拆出一句</Button>
                  <Button variant="secondary" size="sm" disabled>新旧对比</Button>
                </div>
                <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
                  重新生成<b>默认只重生成对应单元</b>；相邻句受影响时直接显示范围。
                  <a className="hf-link" href="#" onClick={(e) => e.preventDefault()}>拆出一句</a>不承诺与原段完全一致的音色表现。
                </p>
              </>
            ) : (
              <p className="wu-caption" style={{ lineHeight: 1.6 }}>
                点击右侧配音清单中的话轮，查看其合成单元与引擎请求属性；候选音频在合成时登记为不可变资产。
              </p>
            )}
          </div>

          <div className="hf-ins-sec">
            <h4>间隔调整</h4>
            <div className="hf-gap">
              <span className="hf-mono">{prevTurnLabel}</span>
              <span className="track">
                <i style={{ width: `${gapPct}%` }} />
                <span className="knob" style={{ left: `${gapPct}%` }} />
                <input
                  type="range" min={0} max={GAP_MAX_MS} step={20}
                  value={gapValue} aria-label="转场间隔 transitionGapMs"
                  onChange={(e) => setGapOverride(Number(e.target.value))}
                  style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', opacity: 0, margin: 0, cursor: 'pointer' }}
                />
              </span>
              <span className="hf-mono">{nextTurnLabel}</span>
              <span className="hf-mono" style={{ color: 'var(--wu-semantic-text)' }}>{gapValue}ms</span>
            </div>
            <p className="wu-caption" style={{ lineHeight: 1.6 }}>
              拖动改变 <code>transitionGapMs</code> → 重排时间轴，<b>通常不重新合成声音</b>；显式间隔是额外静音，与模型自然停顿的关系见说明。
            </p>
            <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <Button variant="secondary" size="sm" disabled>插入句内停顿</Button>
              <span className="wu-caption">引擎支持时用已验证语法；否则拆分单元后本地插入静音</span>
            </div>
          </div>

          <div className="hf-ins-sec">
            <details className="hf-adv">
              <summary>
                <Icon name="chevronRight" size={16} className="chev" />高级（折叠）
              </summary>
              <div className="hf-adv-body">
                <Choice type="checkbox" checked={false} onChange={() => {}} label="引擎参数（本地采样率 / 增强）" />
                <Choice type="checkbox" checked={false} onChange={() => {}} label="对齐设置（按句子边界切分）" />
                <a className="hf-link" href="#" style={{ fontSize: 12.5 }} onClick={(e) => e.preventDefault()}>混用语音引擎入口 →</a>
              </div>
            </details>
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
      }
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Button variant="ghost" size="sm" icon="arrowLeft" onClick={() => setView('script')}>上一步</Button>
            <Badge tone={voiceApproved ? 'success' : 'warning'} icon={voiceApproved ? 'check' : 'alert'}>
              确认点 · 配音确认{voiceApproved ? '（已确认）' : ''}
            </Badge>
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
        <span className="wu-caption">已保存</span>
        {/* 确认点按钮在阶段标题栏右侧（设计稿 HF-03 位置），不在检查器内 */}
        <Button
          variant="brand" size="sm" icon="check"
          disabled={voiceApproved || !timeline} onClick={handleConfirm}
        >{voiceApproved ? '已确认' : '确认配音'}</Button>
      </StageHead>

      <div className="hf-body">
        {error && <Alert tone="danger">{error}</Alert>}
        {!proj?.scriptRevisions.length && (
          <Alert tone="warning">
            还没有脚本。请先在阶段①创建本期并生成对话。
            <a className="hf-link" onClick={() => setView('script')}>前往阶段② 编辑对话</a>
          </Alert>
        )}
        {voiceApproved && (
          <Alert tone="info">
            配音已确认（{timeline?.revisionId}）。重新合成将覆盖当前时间轨并回到待确认；确认后可前往
            <a className="hf-link" onClick={() => setView('visual')}>阶段④ 预览画面</a>。
          </Alert>
        )}

        <div className="hf-s3">
          {/* ---- 左：语音引擎面板 ---- */}
          <section className="wu-card hf-eng">
            <h3>语音引擎</h3>
            <div className="hf-engine">
              {ENGINES.map((e) => (
                <Choice
                  key={e.id} type="radio" name="engine" value={e.id}
                  checked={engine === e.id} onChange={() => pickEngine(e.id)}
                  label={e.label}
                />
              ))}
            </div>

            {bs && SPEAKERS.map((spk) => {
              const b = bs[spk.id];
              return (
                <div className="hf-voice" key={spk.id}>
                  <Badge tone={spk.id === 'A' ? 'info' : 'speaker-b'}>{spk.id}</Badge>
                  <div>
                    <div className="nm">{spk.name} · {spk.role}</div>
                    <select
                      className="wu-input" aria-label={`${spk.id} 音色`} style={{ marginTop: 4 }}
                      value={b.modelId}
                      onChange={(e) => updateBinding(spk.id, { modelId: e.target.value })}
                    >
                      {VOICE_OPTIONS[spk.id].map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                    <div className="wu-row" style={{ gap: 8, marginTop: 4, minWidth: 0, alignItems: 'center' }}>
                      <button
                        type="button" className="hf-link"
                        style={{ background: 'none', border: 'none', padding: 0, fontSize: 11.5 }}
                        onClick={() => fileInput.current[spk.id]?.click()}
                      >{refNames[spk.id] || '选择参考音频'}</button>
                      <input
                        ref={(el) => { fileInput.current[spk.id] = el; }}
                        type="file" accept="audio/*" hidden
                        onChange={(e) => handleRefFile(spk.id, e.target.files?.[0])}
                      />
                      <span className="wu-caption" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {b.localRefAudio ? '已绑定（本地引用）' : '未绑定 · 用提供方默认音色'}
                      </span>
                    </div>
                  </div>
                  <Button variant="secondary" size="sm" icon="play" disabled title="接入真实 TTS 后可试听">试听</Button>
                </div>
              );
            })}

            <div className="hf-playrow">
              <Button
                size="sm" icon="play" busy={synthesizing} disabled={!canSynthesize}
                onClick={handleSynthesize}
              >
                {voiceApproved ? '重新生成整期配音' : '生成整期配音'}
              </Button>
            </div>
            <p className="wu-caption" style={{ marginTop: 8, lineHeight: 1.6 }}>
              生成整期配音需先为双方绑定音色并做短试听；本地引擎默认，可切 MiniMax，切换后要求重新试听。
            </p>
          </section>

          {/* ---- 右：配音清单（话轮级） ---- */}
          <section className="hf-list2">
            {turns.map((t) => {
              const u = unitByTurn.get(t.id);
              const sel = !!u && u.id === selectedUnitId;
              const dur = u && timeline ? fmtDur(u.sampleCount, timeline.sampleRate) : null;
              const turnIdx = turns.findIndex((x) => x.id === t.id);
              const generating = synthesizing && !u && !!synthJob;
              const genDone = units.filter((x) => turns.findIndex((y) => y.id === x.turnId) <= turnIdx).length;
              return (
                <div
                  key={t.id}
                  className={`hf-turn sm ${sel ? 'selected' : ''}`}
                  role={u ? 'button' : undefined}
                  tabIndex={u ? 0 : undefined}
                  onClick={() => u && setSelectedUnitId(u.id)}
                  onKeyDown={(e) => { if (u && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setSelectedUnitId(u.id); } }}
                >
                  <div className="hf-turn-head">
                    <span className="hf-turn-id">#{t.id}</span>
                    <Badge tone={t.speaker === 'A' ? 'info' : 'speaker-b'}>{t.speaker}</Badge>
                    <span className="hf-turn-text">{turnText(t)}</span>
                    {dur && <Badge tone="success">✓ {dur}</Badge>}
                    {generating && <Badge tone="info">生成中 {genDone}/{turnCount}</Badge>}
                  </div>
                  {generating && <div className="hf-prog" style={{ marginTop: 6 }}><i style={{ width: `${Math.round((genDone / Math.max(1, turnCount)) * 100)}%` }} /></div>}
                  {pronTerms(t).length > 0 && (
                    <div className="hf-turn-meta">
                      <span className="hf-meta-warn">
                        <Icon name="alert" size={12} />读音：{pronTerms(t).length} 处（{[...new Set(pronTerms(t))].slice(0, 4).join('、')}）
                      </span>
                    </div>
                  )}
                </div>
              );
            })}

            {!turns.length && (
              <p className="wu-caption">尚无话轮。请先在阶段② 编辑对话中生成脚本。</p>
            )}

            {turns.length > 0 && (
              <div className="hf-playrow">
                <Button variant="secondary" size="sm" icon="play" disabled={!timeline}>连续试听</Button>
                <Button variant="secondary" size="sm" disabled={!timeline}>独听 A</Button>
                <Button variant="secondary" size="sm" disabled={!timeline}>独听 B</Button>
                <span className="wu-caption">点击台词跳到对应位置</span>
                <span className="hf-spacer" />
                <span className="wu-caption">{timeline ? `${units.length} 个合成单元` : '尚未合成'}</span>
              </div>
            )}
          </section>
        </div>
      </div>

      {/* ---- 时长检查条 ---- */}
      <div className="hf-dur">
        <span>时长检查：<b className="hf-mono">{timeline ? totalSecs : '—'}</b></span>
        <Badge tone={voiceApproved ? 'success' : synthesizing ? 'info' : 'muted'}>
          {voiceApproved ? '已确认' : synthesizing ? '生成中' : '待确认'}
        </Badge>
        <span className="hf-spacer" />
        <span className="wu-caption">配音时长含片头片尾发言及显式首尾停顿；后续新增片尾需重新检查总时长</span>
      </div>
    </AppShell>
  );
}
