import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '../lib/api';
import { Alert, Button } from './wu';

interface Plan { ready:boolean; reason:string; assemblyId?:string; missingWindowIds:string[]; windows?:unknown[] }
interface Result { jobId:string; status:string; stage:string; error?:string; stale:boolean; result?:{artifactIds:string[];simulated?:boolean} }
interface Continuation { planId:string;executionAvailable:boolean;reusedWindowCount:number;generateWindowCount:number }

export function ExpertAssembly({projectId,projectRevision}:{projectId:string;projectRevision:number}) {
  const base=`/projects/${encodeURIComponent(projectId)}/expert-video`;
  const queryClient=useQueryClient();
  const [plan,setPlan]=useState<Plan|null>(null);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [busy,setBusy]=useState(false);
  const [continuation,setContinuation]=useState<Continuation|null>(null);
  const [reviewed,setReviewed]=useState<Record<string,boolean>>({});
  const [loaded,setLoaded]=useState<Record<string,boolean>>({});
  const results=useQuery({queryKey:['expert-assemblies',projectId],queryFn:()=>getJson<Result[]>(base+'/assemblies'),refetchInterval:2000});
  async function check() {
    setBusy(true);setError('');setNotice('');
    try { setPlan(await getJson<Plan>(base+'/assembly-plan')); }
    catch(e){setPlan(null);setError(e instanceof Error?e.message:'合成计划检查失败');}
    finally{setBusy(false);}
  }
  async function submit(path:string,body:unknown,message:string) {
    if(busy)return;
    setBusy(true);setError('');setNotice('');
    try{await sendJson(base+path,'POST',body);await results.refetch();await queryClient.invalidateQueries({queryKey:['project',projectId]});setNotice(message);}
    catch(e){setError(e instanceof Error?e.message:'操作失败，可刷新状态后重试');}
    finally{setBusy(false);}
  }
  async function checkContinuation(){
    setBusy(true);setError('');setNotice('');setContinuation(null);
    try{setContinuation(await sendJson<Continuation>(base+'/continuation-plan','POST',{}));}
    catch(e){setError(e instanceof Error?e.message:'续算范围检查失败');}
    finally{setBusy(false);}
  }
  return <section aria-label="已采用窗口整期合成">
    <h3>合成整期成片</h3>
    <p>复用已采用的原生窗口与当前配音，不重新生成口型。缺少的窗口需先生成并采用。</p>
    <Button variant="secondary" disabled={busy} onClick={()=>void checkContinuation()}>检查剩余内容（不生成）</Button>
    {continuation&&<div role="status"><p>保留已采用片段 {continuation.reusedWindowCount} 个，只生成剩余 {continuation.generateWindowCount} 个。完成后在候选区审看整期，再采用。</p>
      {!continuation.generateWindowCount?<p>内容已完整，直接检查下方整期覆盖情况即可。</p>:<Button disabled={busy||!continuation.executionAvailable} onClick={()=>void submit('/generate',{planId:continuation.planId,clientToken:continuation.planId},'续算任务已提交，已有片段会复用；完成后请在候选区审看。')}>一键生成剩余内容</Button>}
      {!continuation.executionAvailable&&<p>专家执行器未就绪，当前只能检查范围。</p>}
    </div>}
    <Button variant="secondary" disabled={busy} onClick={()=>void check()}>检查整期覆盖情况</Button>
    {plan&&<div role="status"><p>{plan.reason}</p>
      {!!plan.missingWindowIds.length&&<p>还需补齐 {plan.missingWindowIds.length} 个片段。请先生成并采用剩余对话，再检查整期覆盖情况。</p>}
      <Button disabled={busy||!plan.ready} onClick={()=>void submit('/assemble',{assemblyId:plan.assemblyId,clientToken:plan.assemblyId},'整期合成任务已提交，不调用模型。')}>合成已采用版本</Button>
    </div>}
    {error&&<div role="alert"><Alert tone="warning">{error}</Alert></div>}
    {notice&&<p role="status">{notice}</p>}
    {results.isError&&<Alert tone="warning">整期任务状态加载失败，请刷新后重试。</Alert>}
    {results.data?.map(result=><article key={result.jobId}>
      <h4>整期候选 {result.jobId}</h4>
      {result.stale&&<Alert tone="warning">采用窗口或工程已改变，需重新合成。</Alert>}
      {result.error&&<Alert tone="warning">{result.error}</Alert>}
      {result.status==='succeeded'&&result.result?.artifactIds.length===1&&!result.result.simulated?<>
        <video controls preload="metadata" style={{width:'100%',maxWidth:720}} aria-label={`${result.jobId}整期成片`}
          src={`/api/artifacts/${encodeURIComponent(result.result.artifactIds[0])}/file`}
          onLoadedMetadata={()=>setLoaded(previous=>({...previous,[result.jobId]:true}))}
          onError={()=>{setLoaded(previous=>({...previous,[result.jobId]:false}));setError('整期视频加载失败，不能采用。');}}/>
        <label style={{display:'block'}}><input type="checkbox" checked={reviewed[result.jobId]??false} onChange={e=>setReviewed(previous=>({...previous,[result.jobId]:e.target.checked}))}/> 已完整观看，确认整期声画及交接通过</label>
        <Button disabled={busy||result.stale||!loaded[result.jobId]||!reviewed[result.jobId]} onClick={()=>void submit(`/assemblies/${result.jobId}/adopt`,{expectedProjectRevision:projectRevision,reviewed:true},'整期已采用，可前往导出页播放和下载；旧成片已保留。')}>采用整期并更新导出</Button>
      </>:<p>{result.stage}（{result.status}）</p>}
    </article>)}
  </section>;
}
