import { useRef, useState } from 'react';
import { getJson, sendJson } from '../lib/api';
import type { Job } from '../lib/types';
import { Button } from './wu';

type Report={receipts:{segmentId:string;promptId:string;status:string}[];notice:string;truncated?:boolean};
const states:Record<string,string>={completed:'引擎已完成，结果尚需核验',failed:'引擎失败',running:'引擎执行中',queued:'引擎排队中',unknown:'引擎未找到记录，禁止据此重采样',unavailable:'引擎不可达或响应无效'};
export function ExternalTaskRecovery({job,onRefresh}:{job:Job;onRefresh:()=>void}) {
  const [busy,setBusy]=useState(false), [error,setError]=useState(''), [report,setReport]=useState<Report|null>(null);
  const lock=useRef(false);
  const run=async(resume:boolean)=>{
    if(lock.current)return; lock.current=true;setBusy(true);setError('');
    try {
      if(resume){await sendJson(`/motion-packs/${encodeURIComponent(job.ownerId!)}/prepare/${encodeURIComponent(job.id)}/resume`,'POST');onRefresh();}
      else setReport(await getJson<Report>(`/jobs/${encodeURIComponent(job.id)}/external-status`));
    }catch(e){setError((e as Error).message);}finally{lock.current=false;setBusy(false);}
  };
  return <section aria-label="外部任务恢复" onClick={e=>e.stopPropagation()} onKeyDown={e=>e.stopPropagation()}>
    <p>先核对已知任务，响应丢失或状态未知时保留原任务记录，避免重复调用。</p>
    <Button size="sm" busy={busy} onClick={()=>void run(false)}>核对原外部任务（只读）</Button>
    {job.kind==='motion.prepare'&&job.ownerType==='motionPack'&&job.ownerId&&job.providerTaskId&&['failed','unknown','paused'].includes(job.status)&&<Button size="sm" disabled={busy} onClick={()=>void run(true)}>继续查询原外部任务（不重采样）</Button>}
    {job.kind!=='motion.prepare'&&<p>专家模式当前仅核对已有记录，生成和恢复执行仍受真实引擎验收门槛限制。</p>}
    {error&&<p role="alert">{error}</p>}
    {report&&<div role="status"><p>{report.notice}</p>{report.receipts.map(r=><p key={r.segmentId}>{r.segmentId} · {r.promptId} · {states[r.status]??'未知'}</p>)}{report.truncated&&<p>仅核对前20个片段；其余请查本机任务记录。</p>}</div>}
  </section>;
}
