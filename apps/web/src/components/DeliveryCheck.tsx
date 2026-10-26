import { useQuery } from '@tanstack/react-query';
import { getJson } from '../lib/api';
import { useProjectStore } from '../stores';
import { Alert, Button } from './wu';

export function DeliveryCheck({projectId, revision}: {projectId: string; revision: number}) {
  const setView = useProjectStore(s=>s.setView);
  const check = useQuery({queryKey:['delivery-check',projectId,revision],queryFn:()=>getJson<{ready:boolean;issues:{code:string;message:string;target:'script'|'voice'|'visual'|'render';level:string}[];notice:string}>(`/projects/${projectId}/plan/delivery`)});
  return <section aria-label="交付前检查"><h3>交付前检查</h3>
    {check.isPending&&<p role="status">正在检查文件与版本…</p>}
    {check.error&&<Alert tone="danger">{check.error.message}<Button onClick={()=>void check.refetch()}>重新检查</Button></Alert>}
    {check.data&&<><Alert tone={check.data.ready?'success':'warning'}>{check.data.ready?'当前文件与版本检查通过':'请处理以下交付问题'}。{check.data.notice}</Alert>
      <ul>{check.data.issues.map(i=><li key={i.code}>{i.level==='warning'?'提示：':''}{i.message} <Button size="sm" variant="ghost" onClick={()=>setView(i.target)}>前往处理</Button></li>)}</ul>
      <Button variant="secondary" size="sm" onClick={()=>void check.refetch()}>重新检查</Button></>}
  </section>;
}
