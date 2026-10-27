import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getJson } from '../lib/api';
import { Button } from './wu';
type Report={available:boolean;blockers?:string[];baselineJobId:string|null;baselineAvailable?:boolean;candidateCount?:number;retiredCount?:number;notice?:string;segments:{cameraId:string;role:string;outputStartFrame:number;outputEndFrame:number;reuseCandidate:boolean;affectedUtteranceIds:string[];resampleWholeTrackDependency:boolean}[]};
export function MotionImpactPlan({projectId,revision}:{projectId:string;revision:number}) {
  const [opened,setOpened]=useState(false);
  const query=useQuery({queryKey:['motion-impact',projectId,revision],queryFn:()=>getJson<Report>(`/projects/${encodeURIComponent(projectId)}/plan/motion`),enabled:opened,retry:false});
  const report=query.data;
  return <details onToggle={e=>setOpened(e.currentTarget.open)}><summary>动作片段重做与复用规划（CPU）</summary>
    <Button size="sm" disabled={query.isFetching} onClick={()=>void query.refetch()}>重新计算规划</Button>
    {query.isFetching&&<p role="status">正在核对真实媒体与输入依赖…</p>}{query.error&&<p role="alert">{query.error.message}</p>}
    {report&&(!report.available?<p>{report.blockers?.join('；')}</p>:<>
      <p>{report.baselineAvailable?`对照任务 ${report.baselineJobId}；候选 ${report.candidateCount} 段，旧计划移除 ${report.retiredCount} 段。`:'尚无可比较的完整真实成片快照，本次作为新计划展示。'}</p>
      <p>{report.notice}</p><div className="motion-impact-table"><table><thead><tr><th>机位 / 角色</th><th>区间（秒）</th><th>结论</th><th>音频依赖单元</th></tr></thead><tbody>{report.segments.map((s,i)=><tr key={i}><td>{s.cameraId} / {s.role}</td><td>{(s.outputStartFrame/25).toFixed(2)}–{(s.outputEndFrame/25).toFixed(2)}</td><td>{s.reuseCandidate?'输入一致，待校验缓存':'新片段或依赖改变，需生成'}{s.resampleWholeTrackDependency?'；依赖整条角色轨':''}</td><td>{s.affectedUtteranceIds.join('、')||'静音区间'}</td></tr>)}</tbody></table></div>
    </>)}
  </details>;
}
