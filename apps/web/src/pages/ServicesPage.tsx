/* ============================================================================
   HF-07 · 服务设置（真实数据流，01 §1.6.4 / 09.1 / 06 §7.3）
   左栏 .hf-formcol 四块：文本 API / 图片 API / 本地引擎 / MiniMax 语音
   右栏 .hf-cap（360px）：能力状态分开登记，不做合并结论
   附加：显存分配块（本机真实设置 machine.json，设计稿未含但功能必需）
   ========================================================================== */
import { useEffect, useState } from 'react';
import { useCapabilities, useMachine, useSaveMachine, useTestProvider } from '../lib/api';
import { PROVIDER_META } from '../lib/types';
import { Badge, Button, Choice, Icon, Input, type Tone } from '../components/wu';
import { AppShell } from '../components/AppShell';
import { useProjectStore } from '../stores';

const VRAM_ITEMS: { id: 'textApi' | 'imageApi' | 'tts' | 'video'; label: string }[] = [
  { id: 'textApi', label: '文本 API' },
  { id: 'imageApi', label: '图片 API' },
  { id: 'tts', label: '本地 TTS' },
  { id: 'video', label: '视频渲染' },
];

export function ServicesPage() {
  const machine = useMachine();
  const caps = useCapabilities();
  const saveMachine = useSaveMachine();
  const test = useTestProvider();
  const setView = useProjectStore((s) => s.setView);

  const [vram, setVram] = useState<Record<string, number>>({ textApi: 4, imageApi: 4, tts: 2, video: 8 });
  const [dirty, setDirty] = useState(false);
  const [budgetOn, setBudgetOn] = useState(true);
  const [budget, setBudget] = useState('2.00');
  const [testResult, setTestResult] = useState<Record<string, 'ok' | 'fail' | null>>({});

  useEffect(() => {
    const a = machine.data?.vramAllocation;
    if (a && Object.keys(a).length) {
      setVram({ textApi: a.textApi ?? 4, imageApi: a.imageApi ?? 4, tts: a.tts ?? 2, video: a.video ?? 8 });
    }
  }, [machine.data?.vramAllocation]);

  const capsData = caps.data ?? machine.data?.capabilities ?? null;
  const queueCaps = machine.data?.queueCaps ?? { gpu: 1, api: 16, cpu: 4 };
  const totalVram = Object.values(vram).reduce((n, v) => n + (Number(v) || 0), 0);
  // 设计稿 HF-07 显存条为「2.8 / 16 GB」；后端 machine.json 目前只持久化分配合计，
  // 不提供实测占用与物理上限，故此处沿用设计稿常量（与 TopBar 的 VRAM 指标同源）。
  const usedVram = 2.8;
  const totalGb = 16;

  const handleVram = (id: string, v: number) => {
    if (!Number.isFinite(v)) return;
    setVram((s) => ({ ...s, [id]: Math.max(0, Math.min(24, v)) }));
    setDirty(true);
  };

  const handleSave = () => {
    saveMachine.mutate({ vramAllocation: vram }, { onSuccess: () => setDirty(false) });
  };

  const handleTest = (id: string) => {
    setTestResult((s) => ({ ...s, [id]: null }));
    test.mutate(id, {
      onSuccess: (r) => setTestResult((s) => ({ ...s, [id]: r.ok ? 'ok' : 'fail' })),
      onError: () => setTestResult((s) => ({ ...s, [id]: 'fail' })),
    });
  };

  const testNote = (id: string) => {
    const r = testResult[id];
    if (r === 'ok') return <span style={{ color: 'var(--wu-semantic-success)', fontSize: 12 }}>连通正常</span>;
    if (r === 'fail') return <span style={{ color: 'var(--wu-semantic-danger)', fontSize: 12 }}>连接失败</span>;
    return null;
  };

  const capOf = (id: 'textApi' | 'imageApi' | 'tts' | 'video') => capsData?.[id];
  const feature = (id: 'imageApi' | 'tts' | 'video', key: string) => {
    const c = capOf(id)?.capabilities as Record<string, unknown> | undefined;
    return c?.[key] === true;
  };

  const capRows: { nm: string; ok: boolean; badge: string; tone: Tone; note?: string }[] = [
    { nm: '文本连通性', ok: !!capOf('textApi')?.ready, badge: capOf('textApi')?.ready ? '已连接' : '未连接', tone: capOf('textApi')?.ready ? 'success' : 'warning' },
    { nm: '图片 · 文生图', ok: feature('imageApi', 'textToImage'), badge: feature('imageApi', 'textToImage') ? '已连接' : '不支持', tone: feature('imageApi', 'textToImage') ? 'success' : 'warning' },
    { nm: '图片 · 单图编辑', ok: feature('imageApi', 'singleImageEdit'), badge: feature('imageApi', 'singleImageEdit') ? '已连接' : '不支持', tone: feature('imageApi', 'singleImageEdit') ? 'success' : 'warning' },
    { nm: '图片 · 多参考图编辑', ok: feature('imageApi', 'multiRefEdit'), badge: feature('imageApi', 'multiRefEdit') ? '已连接' : '不支持', tone: feature('imageApi', 'multiRefEdit') ? 'success' : 'warning', note: '同框图走上传替代' },
    { nm: '本地 TTS', ok: !!capOf('tts')?.ready, badge: capOf('tts')?.ready ? '就绪' : '不可用', tone: capOf('tts')?.ready ? 'success' : 'danger' },
    { nm: '本地口型', ok: !!capOf('video')?.ready, badge: capOf('video')?.ready ? '就绪' : '不可用', tone: capOf('video')?.ready ? 'success' : 'danger' },
    { nm: 'MiniMax 音色可用', ok: feature('tts', 'voiceCloning'), badge: feature('tts', 'voiceCloning') ? `${2} 个绑定有效` : '未配置', tone: feature('tts', 'voiceCloning') ? 'success' : 'warning' },
  ];

  return (
    <AppShell
      project={null}
      onNavigate={setView}
      /* 独立全屏入口：设计稿 HF-07 无 Inspector、无 FootBar（全宽表单） */
      footer={null}
    >
      <div className="hf-h">
        <h2>服务设置</h2>
        <span className="wu-caption">仅四块 · 凭据存机器级设置 · 工程只引用配置 ID</span>
        <span className="hf-spacer" />
        <Badge tone="info">队列上限 GPU {queueCaps.gpu} / API {queueCaps.api} / CPU {queueCaps.cpu}</Badge>
      </div>

      <div className="hf-body2">
        {!capsData && <p className="wu-caption">加载能力快照中…</p>}
        <div className="hf-s7">
          <div className="hf-formcol">
            {/* 文本 API（01 §9.1） */}
            <section className="hf-block">
              <div className="hf-block-t">文本 API <span className="wu-caption">对话生成 · 改写 · 语义建议</span></div>
              <div className="hf-fld">
                <label>地址</label>
                <input className="wu-input" defaultValue="https://api.example.com/v1" aria-label="文本 API 地址" readOnly />
                <span className="wu-caption">Base URL</span>
              </div>
              <div className="hf-fld">
                <label>模型</label>
                <select className="wu-input" aria-label="文本模型" defaultValue="gpt-4o-mini">
                  <option>gpt-4o-mini</option>
                  <option>qwen2.5-72b</option>
                </select>
                <span />
              </div>
              <div className="hf-fld">
                <label>凭据</label>
                <select className="wu-input" aria-label="文本凭据引用" defaultValue="key_text_01">
                  <option value="key_text_01">机器级密钥库 · key_text_01</option>
                  <option value="env">使用系统环境变量</option>
                </select>
                <Button variant="secondary" size="sm" busy={test.isPending && testResult.textApi === null} onClick={() => handleTest('textApi')}>测试连接</Button>
              </div>
              <div className="hf-fld" style={{ alignItems: 'center' }}>
                <label>API 预算</label>
                <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Choice type="checkbox" checked={budgetOn} onChange={setBudgetOn} label="每期上限" />
                  <input
                    className="wu-input" style={{ width: 110 }} aria-label="每期上限金额"
                    value={budget} onChange={(e) => setBudget(e.target.value)} disabled={!budgetOn}
                  />
                  <span className="wu-caption">元 · 超出提示不硬拦</span>
                </div>
                <span>{testNote('textApi')}</span>
              </div>
            </section>

            {/* 图片 API（01 §9.1） */}
            <section className="hf-block">
              <div className="hf-block-t">图片 API <span className="wu-caption">同框底图 · 角色形象 · 场景</span></div>
              <div className="hf-fld">
                <label>地址</label>
                <input className="wu-input" defaultValue="https://image.example.com/v1" aria-label="图片 API 地址" readOnly />
                <span className="wu-caption">Base URL</span>
              </div>
              <div className="hf-fld">
                <label>模型</label>
                <select className="wu-input" aria-label="图片模型" defaultValue="flux-pro">
                  <option>flux-pro</option>
                </select>
                <span />
              </div>
              <div className="hf-fld">
                <label>凭据</label>
                <select className="wu-input" aria-label="图片凭据引用" defaultValue="key_img_01">
                  <option value="key_img_01">机器级密钥库 · key_img_01</option>
                </select>
                <Button variant="secondary" size="sm" busy={test.isPending && testResult.imageApi === null} onClick={() => handleTest('imageApi')}>测试连接</Button>
              </div>
              <p className="wu-caption" style={{ lineHeight: 1.7 }}>
                测试按钮区分<b>付费试听</b>与<b>只读连通检查</b>；有费用时标注「此测试会产生费用」。连接成功不代表具备人物一致性或多参考图能力。
              </p>
            </section>

            {/* 本地引擎（01 §9.1） */}
            <section className="hf-block">
              <div className="hf-block-t">本地引擎 <span className="wu-caption">本地 TTS · 本地口型 · 渲染</span></div>
              <div className="hf-fld">
                <label>状态</label>
                <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <Badge tone={capOf('tts')?.ready ? 'success' : 'danger'}>本地 TTS {capOf('tts')?.ready ? '就绪' : '不可用'}</Badge>
                  <Badge tone={capOf('video')?.ready ? 'success' : 'danger'}>本地口型 {capOf('video')?.ready ? '就绪' : '不可用'}</Badge>
                </span>
                <span />
              </div>
              <div className="hf-fld">
                <label>显存</label>
                <span className="hf-vram">
                  <span className="bar"><i style={{ width: `${Math.min(100, (usedVram / totalGb) * 100)}%` }} /></span>
                  {usedVram} / {totalGb} GB
                </span>
                <Button variant="ghost" size="sm" busy={test.isPending && testResult.tts === null} onClick={() => handleTest('tts')}>自检</Button>
              </div>
              <p className="wu-caption" style={{ lineHeight: 1.7 }}>
                分配合计 {totalVram} GB（人工预算门控，实际以关口 A 实测为准）。{testNote('tts')}
              </p>
            </section>

            {/* MiniMax 语音（01 §9.1） */}
            <section className="hf-block">
              <div className="hf-block-t">MiniMax 语音 <span className="wu-caption">云 TTS · voice_id 绑定</span></div>
              <div className="hf-fld">
                <label>状态</label>
                <span>
                  <Badge tone={feature('tts', 'voiceCloning') ? 'success' : 'warning'}>
                    {feature('tts', 'voiceCloning') ? '2 个绑定有效' : '未配置'}
                  </Badge>
                </span>
                <Button variant="secondary" size="sm" disabled title="接入密钥库后可管理">管理绑定</Button>
              </div>
              <p className="wu-caption" style={{ lineHeight: 1.7 }}>付费试听与只读连通检查区分；音色可用性在能力面板单独登记。</p>
            </section>

            {/* 显存分配（本机真实设置） */}
            <section className="hf-block">
              <div className="hf-block-t">显存分配 <span className="wu-caption">{PROVIDER_META.textApi.label} / 图片 / TTS / 渲染的预算门控（GB）</span></div>
              {VRAM_ITEMS.map((it) => (
                <div className="hf-vram-row" key={it.id}>
                  <label htmlFor={`vram-${it.id}`}>{it.label}</label>
                  <Input
                    id={`vram-${it.id}`} type="number" min={0} max={24}
                    aria-label={`${it.label} VRAM`} value={vram[it.id]}
                    onChange={(e) => handleVram(it.id, Number(e.target.value))}
                  />
                  <span className="wu-caption">GB</span>
                </div>
              ))}
              <div className="wu-row" style={{ gap: 8, marginTop: 6, alignItems: 'center' }}>
                <Button variant="brand" size="sm" icon="save" busy={saveMachine.isPending} disabled={!dirty} onClick={handleSave}>
                  {dirty ? '保存分配' : '已保存'}
                </Button>
                <span className="wu-caption">保存到本机 machine.json，不落密钥明文</span>
              </div>
            </section>
          </div>

          {/* 能力状态面板（01 §9.1：分开登记） */}
          <aside className="hf-cap">
            <div className="hf-cap-t">能力状态 <span className="wu-caption">分开登记，不做合并结论</span></div>
            {capRows.map((r) => (
              <div className="hf-cap-row" key={r.nm}>
                <span className={`hf-dot ${r.ok ? 'ok' : 'no'}`} />
                <span className="nm">{r.nm}</span>
                <span className="hf-spacer" />
                <Badge tone={r.tone}>{r.badge}{r.note ? `（${r.note}）` : ''}</Badge>
              </div>
            ))}
            <p className="wu-caption" style={{ marginTop: 10, lineHeight: 1.7, display: 'flex', gap: 6 }}>
              <Icon name="info" size={12} />
              能力门控来自 GET /providers/capabilities；测试连接调用各适配器。
            </p>
          </aside>
        </div>
      </div>
    </AppShell>
  );
}
