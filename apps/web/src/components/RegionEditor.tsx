import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { sendJson } from '../lib/api';
import type { Project, VisualVariant } from '../lib/types';
import { Button } from './wu';
type Box = {x:number;y:number;width:number;height:number};
export function RegionEditor({project,variant}: {project:Project;variant:VisualVariant}) {
  const qc=useQueryClient();
  const [regions,setRegions]=useState(variant.personRegions as Record<string,Box>);
  const [message,setMessage]=useState('');
  const drag=useRef<{speaker:string;x:number;y:number;box:Box}|null>(null);
  useEffect(()=>setRegions(variant.personRegions as Record<string,Box>),[variant]);
  return <section><p className="wu-caption">拖动人物框定位；下方可精确调整尺寸。保存后需重新检查样片。</p>
    <div className="duo-region-editor" onPointerMove={e=>{const d=drag.current;if(!d)return;const rect=e.currentTarget.getBoundingClientRect();setRegions(s=>({...s,[d.speaker]:{...d.box,x:Math.min(1-d.box.width,Math.max(0,d.box.x+(e.clientX-d.x)/rect.width)),y:Math.min(1-d.box.height,Math.max(0,d.box.y+(e.clientY-d.y)/rect.height))}}));}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
      <img alt="人物区域校准" src={`/api/artifacts/${variant.masterImage.artifactId}/file`}/>
      {Object.entries(regions).map(([s,b])=><button type="button" key={s} aria-label={`拖动 ${s} 人物框`} onPointerDown={e=>{e.currentTarget.parentElement?.setPointerCapture(e.pointerId);drag.current={speaker:s,x:e.clientX,y:e.clientY,box:{...b}};}} style={{position:'absolute',left:b.x*100+'%',top:b.y*100+'%',width:b.width*100+'%',height:b.height*100+'%',background:'transparent',border:'2px solid '+(s==='A'?'#3580bb':'#b66d50'),color:'#fff',touchAction:'none'}}>{s}</button>)}
    </div>
    {Object.entries(regions).map(([s,b])=><div key={s}><b>{s}</b>{(['x','y','width','height'] as const).map(k=><label key={k}>{k}<input aria-label={`${s}区域${k}`} type="number" min="0" max="1" step="0.01" value={b[k]} onChange={e=>setRegions(all=>({...all,[s]:{...all[s],[k]:Number(e.target.value)}}))}/></label>)}</div>)}
    <Button onClick={()=>setRegions({A:regions.B,B:regions.A})}>交换 A/B 区域</Button>
    <Button onClick={async()=>{try{await sendJson(`/projects/${project.id}/visual/regions`,'POST',{variantId:variant.id,regions,expectedRevision:project.revision});await qc.invalidateQueries({queryKey:['project',project.id]});setMessage('已保存，请重新检查样片。');}catch(e){setMessage((e as Error).message);}}}>保存人物区域</Button><p role="status">{message}</p>
  </section>;
}
