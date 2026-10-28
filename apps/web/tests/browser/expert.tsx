/** Isolated UI harness: intercept every API call, never contact a model or production store. */
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ExpertPreviewPlanner } from '../../src/components/ExpertPreviewPlanner';
import type { Project } from '../../src/lib/types';
import '../../src/styles/tokens.css';
import '../../src/styles/components.css';
import '../../src/styles/duo-shell.css';

const turns = Array.from({length: 6}, (_, i) => ({ id: `T${i}`, speaker: i%2 ? 'B' : 'A',
  lines: [{id: `L${i}`, displayText: ['今天我们怎么提前检查视频效果？','先看两轮对话，确认嘴唇稳定和角色交接。','其中一句不好，可以单独重做吗？','可以，采用前保留原来的版本。','那就检查一下中间的范围。','好的，满意后继续整片。'][i]}] }));
const project = { id: 'isolated-ux', revision:0,currentDraftRevision: 'R1', scriptRevisions: [{id:'R1',turns}],
  motionPack: {id:'fixture',version:'v1'}, audioTimeline:{revisionId:'R1'} } as Project;
type State = {plans: any[]; candidates: any[]; selection: {revision: number; selected: Record<string,string>; versions: any[]}};
let state: State = JSON.parse(localStorage.getItem('duocast-expert-ux-fixture') ?? 'null') ?? {plans: [], candidates: [], selection:{revision:0,selected:{},versions:[]}};
const persist = () => localStorage.setItem('duocast-expert-ux-fixture',JSON.stringify(state));
let assemblies: any[]=[];
window.fetch = async (input, init) => {
  const path = String(input).split('/expert-video')[1];
  const body = JSON.parse(String(init?.body ?? '{}'));
  let response: unknown;
  if (path === '/plans') response = state.plans;
  else if (path === '/candidates') response = {candidates:state.candidates,selection:state.selection};
  else if(path==='/assemblies')response=assemblies;
  else if(path==='/assembly-plan')response={ready:true,reason:'模拟覆盖完整，可合成（不调用模型）',assemblyId:'EA-fixture',missingWindowIds:[]};
  else if(path==='/continuation-plan'){
    const plan={planId:'EP-continuation-fixture',operation:'continue',selectedTurnIds:turns.map(t=>t.id),
      displayFrameRange:[0,300],windows:[{windowId:'W1'},{windowId:'W2'}],turnWindowBindings:{W1:['T0','T1'],W2:['T2','T3','T4','T5']},
      executionAvailable:true,reusedWindowCount:1,generateWindowCount:1};
    state.plans.push(plan);response=plan;
  }
  else if(path==='/assemble'){
    if(!assemblies.length)assemblies.push({jobId:'ASSEMBLY1',status:'succeeded',stage:'模拟合成完成',stale:false,result:{artifactIds:['fixture'],simulated:false}});
    response={jobId:'ASSEMBLY1'};
  }else if(path?.startsWith('/assemblies/')&&path.endsWith('/adopt'))response={version:'OUT-fixture-v1'};
  else if(String(input)==='/api/projects/isolated-ux')response=project;
  else if (path?.endsWith('-plan')) {
    const indices = body.turnIds.map((id:string) => turns.findIndex(t=>t.id===id));
    const plan = { planId: 'EP-'+crypto.randomUUID().replaceAll('-','').slice(0,24), selectedTurnIds:body.turnIds,
      displayFrameRange:[Math.min(...indices)*50,(Math.max(...indices)+1)*50], windows:[{windowId:'W1',reason:'selected_turn'}],
      turnWindowBindings:{W1:body.turnIds}, executionAvailable:true, operation:path==='/preview-plan'?'preview':'regenerate' };
    state.plans.push(plan); response=plan;
  } else if (path === '/generate') {
    let candidate=state.candidates.find(c=>c.plan.planId===body.planId);
    if (!candidate) {
      candidate={jobId:'J'+(state.candidates.length+1),status:'succeeded',stage:'模拟任务完成',stale:false,
        plan:state.plans.find(p=>p.planId===body.planId),result:{artifactIds:['fixture'],simulated:false}};
      state.candidates.unshift(candidate);
    }
    response={jobId:candidate.jobId};
  } else if (path?.endsWith('/adopt') || path === '/restore') {
    if (body.expectedRevision!==state.selection.revision) return new Response(JSON.stringify({detail:'版本冲突，请刷新'}),{status:409});
    const selected = path==='/restore' ? state.selection.versions.find(v=>v.version===body.version).selected
      : {...state.selection.selected,W1:path.split('/')[2]};
    state.selection={revision:state.selection.revision+1,selected,versions:[...state.selection.versions,{version:state.selection.revision+1,selected}]};
    response=state.selection;
  } else throw new Error('隔离验收禁止未声明请求：'+String(input));
  persist(); return new Response(JSON.stringify(response),{headers:{'Content-Type':'application/json'}});
};
ReactDOM.createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient()}>
  <main className="wu" style={{maxWidth:1000,margin:'auto',padding:24}}>
    <h1>专家流程 · 隔离模拟验收</h1><p>模拟任务与版本，不调用真实模型。视频素材是测试色块，不代表生成质量。</p>
    <ExpertPreviewPlanner project={project}/>
  </main>
</QueryClientProvider>);
