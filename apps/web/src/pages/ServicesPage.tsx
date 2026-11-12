import { useEffect, useState } from 'react';
import { useMachine, useSaveMachine, useTestProvider } from '../lib/api';
import { Alert, Badge, Button, Field, Input } from '../components/wu';
import { AppShell } from '../components/AppShell';
import { useProjectStore } from '../stores';
import { sendJson } from '../lib/api';

const CHANNELS = [{key:'text', label:'文本', real:'toapis'}, {key:'image', label:'图片', real:'toapis'}, {key:'tts', label:'语音', real:'comfyui'}, {key:'video', label:'视频', real:'comfyui'}];
const DEFAULTS: Record<string, string | number> = {text_provider:'mock',image_provider:'mock',tts_provider:'mock',video_provider:'mock',text_model:'deepseek-flash',text_base_url:'https://toapis.com',image_base_url:'https://toapis.com',comfyui_url:'http://127.0.0.1:8188',video_url:'http://127.0.0.1:8191',credential_env:'DUOCAST_TOAPIS_KEY',budget_yuan:2};

export function ServicesPage() {
  const machine = useMachine();
  const save = useSaveMachine();
  const test = useTestProvider();
  const setView = useProjectStore(s=>s.setView);
  const [config,setConfig] = useState(DEFAULTS);
  const [results,setResults] = useState<Record<string,string>>({});
  const [message,setMessage] = useState('');
  const [validating,setValidating] = useState(false);
  const [validation,setValidation] = useState<{valid:boolean;errors:string[];warnings:string[]}|null>(null);
  const [diagnostics,setDiagnostics] = useState<Record<string,unknown>|null>(null);
  useEffect(()=>{
    if(machine.data) setConfig({...DEFAULTS,...Object.fromEntries(Object.entries(machine.data.activeConfig ?? {}).filter(([,v])=>typeof v!=='boolean')) as Record<string,string|number>,...machine.data.runtimeConfig});
  },[machine.data]);
  const change=(key:string,value:string|number)=>{setValidation(null);setConfig(s=>({...s,[key]:value}));};
  const validate=async()=>{setValidating(true);try{const result=await sendJson<{valid:boolean;errors:string[];warnings:string[]}>('/machine/validate-config','POST',{runtimeConfig:config});setValidation(result);return result.valid;}catch(e){setMessage((e as Error).message);return false;}finally{setValidating(false);}};
  return <AppShell project={null} onNavigate={setView} footer={null}>
    <div className="hf-h"><h2>环境检查与服务设置</h2><Button variant="secondary" onClick={()=>setView('home')}>返回工程列表</Button></div>
    <div className="hf-body2">
      <Alert tone="info">配置保存后重启服务生效；检测使用当前已生效配置，不产生付费内容。已有脚本可直接导入，无需文本 API。</Alert>
      {message && <Alert tone={save.isError?'danger':'info'}>{message}</Alert>}
      {machine.error && <Alert tone="danger">{machine.error.message}</Alert>}
      <div className="hf-s7">
        <div className="hf-formcol">
          {CHANNELS.map(c=><section className="hf-block" key={c.key}>
            <h3>{c.label}服务</h3>
            <Field label="提供方"><select className="wu-input" aria-label={c.label+'提供方'} value={config[c.key+'_provider']} onChange={e=>change(c.key+'_provider',e.target.value)}><option value="mock">演示 · 不产真实媒体</option><option value={c.real}>{c.real} · 真实服务</option></select></Field>
            {(c.key==='text'||c.key==='image') && <Field label="服务根地址"><Input aria-label={c.label+'服务根地址'} value={config[c.key+'_base_url']} onChange={e=>change(c.key+'_base_url',e.target.value)}/></Field>}
            {c.key==='text' && <Field label="模型"><Input aria-label="文本模型" value={config.text_model} onChange={e=>change('text_model',e.target.value)}/></Field>}
            {c.key==='image' && <p className="wu-caption">图片适配器使用 gpt-image-2-vip 文生图；人物参考编辑尚未开放，可上传已审看的双人底图。</p>}
            {(c.key==='tts'||c.key==='video') && <Field label="引擎地址"><Input aria-label={c.label+'引擎地址'} value={config[c.key==='tts'?'comfyui_url':'video_url']} onChange={e=>change(c.key==='tts'?'comfyui_url':'video_url',e.target.value)}/></Field>}
            <Badge tone="muted">{machine.data?.capabilities[c.key==='text'?'textApi':c.key==='image'?'imageApi':c.key as 'tts'|'video']?.mode==='mock'?'演示可运行':'已装配 · 待检测及样片验证'}</Badge>
            <Button variant="secondary" busy={test.isPending} onClick={()=>test.mutate(c.key,{onSuccess:r=>setResults(s=>({...s,[c.key]:(r.simulated?'演示自检：':'')+(r.message??r.error??'检测完成')+(r.checkedAt?' · '+new Date(r.checkedAt).toLocaleTimeString(): '')})),onError:e=>setResults(s=>({...s,[c.key]:e.message}))})}>检测当前配置</Button>
            {results[c.key] && <p role="status">{results[c.key]}</p>}
          </section>)}
          <section className="hf-block"><h3>凭据与预算</h3>
            <Field label="凭据环境变量名称"><Input aria-label="凭据环境变量名称" value={config.credential_env} onChange={e=>change('credential_env',e.target.value)}/></Field>
            <p className="wu-caption">仅保存变量名称，密钥在系统环境中配置；当前进程{machine.data?.activeConfig?.credentialPresent?'已读取凭据':'未读取凭据'}。备份不包含密钥。</p>
            <Field label="每期预算提醒（元）"><Input type="number" min="0" aria-label="每期预算提醒" value={config.budget_yuan} onChange={e=>change('budget_yuan',Number(e.target.value))}/></Field>
            <p className="wu-caption">预算会持久化。实际费用尚未统计，当前不提供硬额度保证。</p>
            {validation&&<Alert tone={validation.valid?'info':'danger'}>{validation.valid?'本机配置格式校验通过；未测试网络或推理。':'配置尚不能使用：'}<ul>{[...validation.errors,...validation.warnings].map(t=><li key={t}>{t}</li>)}</ul></Alert>}
            <Button variant="secondary" busy={validating} disabled={save.isPending} onClick={()=>void validate()}>保存前校验</Button>
            <Button busy={save.isPending||validating} onClick={async()=>{if(await validate())save.mutate({runtimeConfig:config},{onSuccess:()=>setMessage('配置已保存。请重启服务后检测新配置；当前任务继续使用冻结输入。'),onError:e=>setMessage(e.message)});}}>保存配置（重启生效）</Button>
          </section>
        </div>
        <aside className="hf-cap"><h3>本机诊断</h3><Button onClick={async()=>{try{const r=await fetch('/api/diagnostics');if(!r.ok)throw new Error('诊断失败');setDiagnostics(await r.json());}catch(e){setMessage((e as Error).message);}}}>检查运行环境</Button>
          <p><a className="hf-link" href="/api/diagnostics/package" download>下载脱敏诊断包</a></p><p className="wu-caption">仅含环境能力和错误分类，不含路径、工程正文、服务地址、密钥及原始日志。</p>
          <p className="wu-caption">检测 Python、FFmpeg、参考音频、快速引擎文件和磁盘。真实模型质量需实际样片验收。</p>
          {diagnostics && <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(diagnostics,null,2)}</pre>}
        </aside>
      </div>
    </div>
  </AppShell>;
}
