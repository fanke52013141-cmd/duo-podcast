/* ============================================================================
   HF-07 · 服务设置（真实数据流，01 §1.6.4 / 06 §7.3）
   提供方能力卡（能力门控 + 测试连接）+ 机器设置（VRAM 分配，凭据只存引用）
   ========================================================================== */
import { useEffect, useState } from 'react';
import { useCapabilities, useMachine, useSaveMachine, useTestProvider } from '../lib/api';
import { PROVIDER_META, type Capabilities } from '../lib/types';
import { Badge, Button, Icon, Input } from '../components/wu';
import { AppShell, FootBar, StageHead } from '../components/AppShell';
import { useProjectStore } from '../stores';

const PROVIDER_IDS: (keyof Capabilities)[] = ['textApi', 'imageApi', 'tts', 'video'];

const CAP_LABEL: Record<string, string> = {
  streaming: '流式', maxContextChars: '长上下文', structuredOutput: '结构化输出', paid: '付费',
  textToImage: '文生图', singleImageEdit: '单图编辑', multiRefEdit: '多参考编辑',
  aspectRatios: '多画幅', async_: '异步任务', cancel: '可取消', download: '可下载',
  voiceCloning: '音色克隆', emotion: '情绪', speed: '语速', pronunciation: '读音',
  timestamps: '时间戳', dualTrackInput: '双轨输入', resolutions: '多分辨率', fps: '多帧率', progress: '进度',
};

function capChips(caps: Record<string, unknown>): string[] {
  return Object.entries(caps)
    .filter(([, v]) => v === true || typeof v === 'number' || Array.isArray(v))
    .map(([k]) => CAP_LABEL[k] ?? k);
}

export function ServicesPage() {
  const machine = useMachine();
  const caps = useCapabilities();
  const saveMachine = useSaveMachine();
  const test = useTestProvider();
  const setView = useProjectStore((s) => s.setView);

  const [vram, setVram] = useState<Record<string, number>>({ textApi: 4, imageApi: 4, tts: 2, video: 8 });
  const [dirty, setDirty] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, 'ok' | 'fail' | null>>({});

  useEffect(() => {
    const a = machine.data?.vramAllocation;
    if (a && Object.keys(a).length) {
      setVram({ textApi: a.textApi ?? 4, imageApi: a.imageApi ?? 4, tts: a.tts ?? 2, video: a.video ?? 8 });
    }
  }, [machine.data?.vramAllocation]);

  const capsData = caps.data ?? machine.data?.capabilities ?? null;
  const queueCaps = machine.data?.queueCaps ?? { gpu: 1, api: 16, cpu: 4 };

  const handleVram = (id: string, v: number) => {
    if (!Number.isFinite(v)) return;
    setVram((s) => ({ ...s, [id]: Math.max(0, Math.min(24, v)) }));
    setDirty(true);
  };

  const handleSave = () => {
    saveMachine.mutate({ vramAllocation: vram }, {
      onSuccess: () => setDirty(false),
    });
  };

  const handleTest = (id: string) => {
    setTestResult((s) => ({ ...s, [id]: null }));
    test.mutate(id, {
      onSuccess: (r) => setTestResult((s) => ({ ...s, [id]: r.ok ? 'ok' : 'fail' })),
      onError: () => setTestResult((s) => ({ ...s, [id]: 'fail' })),
    });
  };

  return (
    <AppShell
      project={null}
      onNavigate={setView}
      footer={
        <FootBar hint={
          <span className="wu-row" style={{ gap: 12 }}>
            <Badge tone="muted">密钥只存引用</Badge>
            <span className="wu-caption">凭据存储（keyring）在关口 B 接入；当前为 mock 适配器联调</span>
          </span>
        }
      />
      }
    >
      <StageHead title="服务设置">
        <span className="hf-spacer" />
        <Badge tone="info">队列上限 GPU {queueCaps.gpu} / API {queueCaps.api} / CPU {queueCaps.cpu}</Badge>
      </StageHead>

      <div className="hf-body" style={{ display: 'flex', gap: 20, minHeight: 0 }}>
        <div className="hf-panel" style={{ flex: 1, minWidth: 0 }}>
          <div className="hf-zone-t">提供方<span className="wu-caption">能力门控来自 GET /providers/capabilities；测试连接调用各适配器</span></div>
          {!capsData && <p className="wu-caption">加载能力快照中…</p>}
          <div className="hf-voice-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
            {PROVIDER_IDS.map((id) => {
              const meta = PROVIDER_META[id];
              const cap = capsData?.[id];
              const ready = cap?.ready ?? false;
              const chips = cap ? capChips(cap.capabilities as Record<string, unknown>) : [];
              const result = testResult[id];
              return (
                <div className="hf-svc-card" key={id}>
                  <div className="head">
                    <Icon name={meta.icon} size={18} />
                    <h3>{meta.label}</h3>
                    <Badge tone={ready ? 'success' : 'danger'}>{ready ? '可用' : '不可用'}</Badge>
                    <span className="hf-spacer" />
                    <span className="hf-cap"><Icon name="link" size={13} />{cap ? '已连接' : '未连接'}</span>
                  </div>
                  <p className="wu-caption" style={{ margin: 0, lineHeight: 1.5 }}>{meta.desc}</p>
                  <div className="wu-row" style={{ gap: 6, flexWrap: 'wrap' }}>
                    {chips.map((c) => <Badge key={c} tone="muted">{c}</Badge>)}
                  </div>
                  <div className="wu-row" style={{ gap: 8 }}>
                    <Button
                      variant="secondary" size="sm" icon="link" busy={test.isPending && !result}
                      onClick={() => handleTest(id)}
                    >测试连接</Button>
                    {result === 'ok' && <span className="hf-meta-warn" style={{ color: 'var(--wu-semantic-success)' }}>连通正常</span>}
                    {result === 'fail' && <span className="hf-meta-warn">连接失败</span>}
                  </div>
                </div>
              );
            })}
          </div>

          <div className="hf-zone-t" style={{ marginTop: 22 }}>机器设置<span className="wu-caption">VRAM 分配为人工预算门控（GB）；保存到本机 machine.json，不落密钥明文</span></div>
          <div className="hf-svc-card" style={{ maxWidth: 560 }}>
            <div className="hf-fld"><label>文本 API</label><Input type="number" min={0} max={24} aria-label="文本 API VRAM" value={vram.textApi} onChange={(e) => handleVram('textApi', Number(e.target.value))} /></div>
            <div className="hf-fld"><label>图片 API</label><Input type="number" min={0} max={24} aria-label="图片 API VRAM" value={vram.imageApi} onChange={(e) => handleVram('imageApi', Number(e.target.value))} /></div>
            <div className="hf-fld"><label>本地 TTS</label><Input type="number" min={0} max={24} aria-label="本地 TTS VRAM" value={vram.tts} onChange={(e) => handleVram('tts', Number(e.target.value))} /></div>
            <div className="hf-fld"><label>视频渲染</label><Input type="number" min={0} max={24} aria-label="视频渲染 VRAM" value={vram.video} onChange={(e) => handleVram('video', Number(e.target.value))} /></div>
            <div className="wu-row" style={{ gap: 8, marginTop: 6 }}>
              <Button variant="brand" size="sm" icon="save" busy={saveMachine.isPending} disabled={!dirty} onClick={handleSave}>
                {dirty ? '保存分配' : '已保存'}
              </Button>
              <span className="wu-caption">估算合计 {Object.values(vram).reduce((n, v) => n + (Number(v) || 0), 0)} GB（顶部 VRAM 表仅供人工参考，实际以关口 A 实测为准）</span>
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
