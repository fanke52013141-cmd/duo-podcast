import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { getJson, sendJson } from '../lib/api';
import { Button } from './wu';
type Report = {freeBytes:number;cacheBytes:number;mediaBytes:Record<string,number>;projects:{id:string;title:string;referencedBytes:number}[];trash:{token:string;kind:string;title?:string}[];notice:string};
const mb=(n:number)=>(n/1024/1024).toFixed(1)+' MB';
export function MaintenancePanel() {
  const qc=useQueryClient();
  const [open,setOpen]=useState(false);
  const [message,setMessage]=useState('');
  const report=useQuery({queryKey:['maintenance'],queryFn:()=>getJson<Report>('/maintenance'),enabled:open});
  const act=async(path:string)=>{try{await sendJson(path,'POST',{});setMessage('操作完成。');await qc.invalidateQueries({queryKey:['maintenance']});await qc.invalidateQueries({queryKey:['projects']});}catch(e){setMessage((e as Error).message);}};
  return <section><Button variant="secondary" onClick={()=>setOpen(s=>!s)}>工程恢复与空间管理</Button>{open&&<div className="hf-block">
    {report.error&&<p role="alert">{report.error.message}</p>}{message&&<p role="status">{message}</p>}
    {report.data&&<><p>可用空间 {mb(report.data.freeBytes)} · 可重建查找缓存 {mb(report.data.cacheBytes)}</p><p>{report.data.notice}</p>
      <Button onClick={()=>void act('/maintenance/cache-trash')}>将查找缓存移入回收区</Button>
      <h4>工程引用媒体</h4>{report.data.projects.map(p=><p key={p.id}>{p.title} · {mb(p.referencedBytes)} <a href={`/api/projects/${p.id}/backup`} download>下载工程备份</a></p>)}
      <h4>回收区</h4>{report.data.trash.length===0?<p>暂无回收记录</p>:report.data.trash.map(t=><div key={t.token}><span>{t.title??'查找缓存'} · {t.token}</span><Button onClick={()=>void act(`/maintenance/restore/${encodeURIComponent(t.token)}`)}>恢复</Button></div>)}
    </>}
  </div>}</section>;
}
