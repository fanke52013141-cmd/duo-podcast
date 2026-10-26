import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { explicitMotionPin, listMotionPacks, type MotionReadiness } from '../lib/motionPacks';
import { getJson, usePatchProject } from '../lib/api';
import type { Project } from '../lib/types';
import { Button } from './wu';
export type MotionPin={id:string;version:string};
export type ShotStyle='twoShot'|'overShoulder'|'mixed';
const styles:{id:ShotStyle;label:string}[]=[{id:'twoShot',label:'双人同框'},{id:'overShoulder',label:'双向过肩正反打'},{id:'mixed',label:'同框与过肩组合'}];
export function useMotionReadiness(pin:MotionPin|null|undefined,style:ShotStyle='twoShot'){
  return useQuery({queryKey:['motion-ready',pin?.id,pin?.version,style],enabled:!!pin,
    queryFn:()=>getJson<MotionReadiness>(`/motion-packs/${encodeURIComponent(pin!.id)}/readiness?style=${style}&version=${encodeURIComponent(pin!.version)}`)});
}
export function MotionPackSelector({pin,style,onChange,onReady,disabled=false}:{pin:MotionPin|null;style:ShotStyle;onChange:(pin:MotionPin|null,style:ShotStyle)=>void;onReady?:(ready:boolean)=>void;disabled?:boolean}){
  const packs=useQuery({queryKey:['motion-packs'],queryFn:listMotionPacks});
  const two=useMotionReadiness(pin,'twoShot'),ots=useMotionReadiness(pin,'overShoulder'),mixed=useMotionReadiness(pin,'mixed');
  const current=style==='twoShot'?two:style==='overShoulder'?ots:mixed;
  useEffect(()=>onReady?.(!pin||current.data?.ready===true),[pin?.id,pin?.version,current.data?.ready,onReady]);
  return <fieldset disabled={disabled}><legend>动作来源与镜头</legend><label>动作资产包<select value={pin?.id??''} onChange={e=>{const pack=packs.data?.find(p=>p.id===e.target.value);onChange(pack?explicitMotionPin(pack):null,'twoShot');}}><option value="">保留旧快速演播室模板</option>{pin&&!packs.data?.some(p=>p.id===pin.id)&&<option value={pin.id}>{pin.id}（固定历史版本）</option>}{packs.data?.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>{packs.error&&<p role="alert">{packs.error.message}</p>}{pin&&<><p>固定版本：{pin.version}。资产库更新不会自动切换本工程。</p><Button disabled={!packs.data?.some(p=>p.id===pin.id&&p.version!==pin.version)} onClick={()=>{const latest=packs.data?.find(p=>p.id===pin.id);if(latest)onChange(explicitMotionPin(latest),style);}}>明确选择该资产包最新版本</Button><label>镜头组合<select value={style} onChange={e=>onChange(pin,e.target.value as ShotStyle)}>{styles.map(s=><option key={s.id} value={s.id} disabled={(s.id==='twoShot'?two:s.id==='overShoulder'?ots:mixed).data?.ready!==true}>{s.label} · {(s.id==='twoShot'?two:s.id==='overShoulder'?ots:mixed).data?.ready?'已就绪':'未就绪'}</option>)}</select></label><div>{styles.map(s=>{const q=s.id==='twoShot'?two:s.id==='overShoulder'?ots:mixed;return <details key={s.id}><summary>{s.label}：{q.isPending?'验证中':q.error?'验证失败':q.data?.ready?'服务端已确认就绪':'缺少素材或审看'}</summary>{q.error&&<p>{q.error.message}</p>}<ul>{q.data?.missing.map(m=><li key={m}>{m}</li>)}</ul></details>;})}</div></>}</fieldset>;
}
export function ProjectMotionBinding({project}:{project:Project}){
  const [pin,setPin]=useState<MotionPin|null>(project.motionPack??null),[style,setStyle]=useState<ShotStyle>(project.shotStyle??'twoShot'),[ready,setReady]=useState(false);
  const patch=usePatchProject();
  useEffect(()=>{setPin(project.motionPack??null);setStyle(project.shotStyle??'twoShot');},[project.motionPack?.id,project.motionPack?.version,project.shotStyle]);
  if(project.generationMode!=='fast')return <p>动作资产包当前仅支持快速模式。</p>;
  return <section><MotionPackSelector pin={pin} style={style} onChange={(p,s)=>{setPin(p);setStyle(s);setReady(false);}} onReady={setReady} disabled={patch.isPending}/><Button disabled={!ready||patch.isPending||!pin} onClick={()=>patch.mutate({projectId:project.id,expectedRevision:project.revision,patch:{motionPack:pin,shotStyle:style}})}>将所选固定版本绑定到工程</Button>{patch.error&&<p role="alert">{patch.error.message}</p>}</section>;
}
