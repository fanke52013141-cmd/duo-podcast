import { useQuery } from '@tanstack/react-query';
import { getJson } from '../lib/api';
type Plan={reusableTurnIds:string[];generateTurnIds:string[];estimateSeconds:[number,number]|null;measurementCount:number;estimatedTemporaryBytes:number;freeBytes:number;impact:string};
export function ImpactPlan({projectId,revision}:{projectId:string;revision:number}) {
  const query=useQuery({queryKey:['impact',projectId,revision],queryFn:()=>getJson<Plan>(`/projects/${projectId}/plan`)});
  const p=query.data;
  return <details className="hf-block"><summary>修改影响、复用与资源预算</summary>{query.error&&<p>{query.error.message}</p>}{p&&<><p>{p.impact}</p><p>可复用配音 {p.reusableTurnIds.length} 条 · 待生成 {p.generateTurnIds.length} 条</p><p>真实视频耗时参考：{p.estimateSeconds?`${Math.round(p.estimateSeconds[0])}–${Math.round(p.estimateSeconds[1])} 秒（${p.measurementCount} 条相似记录，不含当前排队）`:'尚无相似真实任务数据'}</p><p>临时空间预算约 {(p.estimatedTemporaryBytes/1024**2).toFixed(0)} MB · 可用 {(p.freeBytes/1024**3).toFixed(1)} GB</p></>}</details>;
}
