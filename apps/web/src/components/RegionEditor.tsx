import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { sendJson } from '../lib/api';
import type { Project, VisualVariant } from '../lib/types';
import { Button } from './wu';
type Box = {x:number;y:number;width:number;height:number};
/** 自动近景推导：人物框上部 55%（半身），与后端 crop_region 同口径。 */
const derive = (r: Box): Box => ({ x: r.x, y: r.y, width: r.width, height: r.height * 0.55 });
const derivedAll = (regions: Record<string, Box>) => Object.fromEntries(Object.entries(regions).map(([s, b]) => [s, derive(b)]));
export function RegionEditor({project,variant}: {project:Project;variant:VisualVariant}) {
  const qc=useQueryClient();
  const [regions,setRegions]=useState(variant.personRegions as Record<string,Box>);
  const saved = variant.closeupRegions && Object.keys(variant.closeupRegions).length ? variant.closeupRegions : null;
  const [custom,setCustom]=useState(!!saved);
  const [closeups,setCloseups]=useState<Record<string,Box>>(saved ?? derivedAll(variant.personRegions as Record<string,Box>));
  const [message,setMessage]=useState('');
  const drag=useRef<{speaker:string;kind:'person'|'closeup';x:number;y:number;box:Box}|null>(null);
  useEffect(()=>{
    setRegions(variant.personRegions as Record<string,Box>);
    const sc = variant.closeupRegions && Object.keys(variant.closeupRegions).length ? variant.closeupRegions : null;
    setCustom(!!sc);
    setCloseups(sc ?? derivedAll(variant.personRegions as Record<string,Box>));
  },[variant]);
  // 未自定义时虚线框实时跟随人物框（自动半身推导）；一旦拖动/精调即成为显式近景范围。
  const shown = custom ? closeups : derivedAll(regions);
  const onMove=(e:React.PointerEvent<HTMLDivElement>)=>{
    const d=drag.current;if(!d)return;
    const rect=e.currentTarget.getBoundingClientRect();
    const nx=Math.min(1-d.box.width,Math.max(0,d.box.x+(e.clientX-d.x)/rect.width));
    const ny=Math.min(1-d.box.height,Math.max(0,d.box.y+(e.clientY-d.y)/rect.height));
    if(d.kind==='person') setRegions(s=>({...s,[d.speaker]:{...d.box,x:nx,y:ny}}));
    else { setCustom(true); setCloseups(s=>({...s,[d.speaker]:{...d.box,x:nx,y:ny}})); }
  };
  const startDrag=(e:React.PointerEvent<HTMLButtonElement>,speaker:string,kind:'person'|'closeup',box:Box)=>{
    e.currentTarget.parentElement?.setPointerCapture(e.pointerId);
    drag.current={speaker,kind,x:e.clientX,y:e.clientY,box:{...box}};
  };
  const editCloseup=(s:string,k:keyof Box,v:number)=>{ setCustom(true); setCloseups(all=>({...all,[s]:{...all[s],[k]:v}})); };
  return <section><p className="wu-caption">实线=人物区域（拖动定位）；虚线=近景裁切框。默认虚线框按人物框上部半身自动推导；拖动或精调虚线框后，切近景将完全按照虚线框范围裁切。保存后需重新检查样片。</p>
    <div className="duo-region-editor" onPointerMove={onMove} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
      <img alt="人物区域校准" src={`/api/artifacts/${variant.masterImage.artifactId}/file`}/>
      {Object.entries(regions).map(([s,b])=><button type="button" key={s} aria-label={`拖动 ${s} 人物框`} onPointerDown={e=>startDrag(e,s,'person',b)} style={{position:'absolute',left:b.x*100+'%',top:b.y*100+'%',width:b.width*100+'%',height:b.height*100+'%',background:'transparent',border:'2px solid '+(s==='A'?'#3580bb':'#b66d50'),color:'#fff',touchAction:'none'}}>{s}</button>)}
      {Object.entries(shown).map(([s,b])=><button type="button" key={'cu-'+s} aria-label={`拖动 ${s} 近景虚线框`} onPointerDown={e=>startDrag(e,s,'closeup',b)} style={{position:'absolute',left:b.x*100+'%',top:b.y*100+'%',width:b.width*100+'%',height:b.height*100+'%',background:'transparent',border:'2px dashed '+(s==='A'?'#3580bb':'#b66d50'),color:'#fff',touchAction:'none',fontSize:11}}>{s} 近景{custom?'':'·自动'}</button>)}
    </div>
    {Object.entries(regions).map(([s,b])=><div key={s}><b>{s} 人物</b>{(['x','y','width','height'] as const).map(k=><label key={k}>{k}<input aria-label={`${s}区域${k}`} type="number" min="0" max="1" step="0.01" value={b[k]} onChange={e=>setRegions(all=>({...all,[s]:{...all[s],[k]:Number(e.target.value)}}))}/></label>)}</div>)}
    {Object.entries(shown).map(([s,b])=><div key={'cu-'+s}><b>{s} 近景</b>{(['x','y','width','height'] as const).map(k=><label key={k}>{k}<input aria-label={`${s}近景${k}`} type="number" min="0" max="1" step="0.01" value={Number(b[k].toFixed(4))} onChange={e=>editCloseup(s,k,Number(e.target.value))}/></label>)}</div>)}
    <Button onClick={()=>{setRegions({A:regions.B,B:regions.A});if(custom)setCloseups({A:closeups.B,B:closeups.A});}}>交换 A/B 区域</Button>
    <Button onClick={()=>{setCustom(false);setCloseups(derivedAll(regions));}}>近景恢复自动（半身）</Button>
    <Button onClick={async()=>{try{await sendJson(`/projects/${project.id}/visual/regions`,'POST',{variantId:variant.id,regions,closeupRegions:custom?closeups:{},expectedRevision:project.revision});await qc.invalidateQueries({queryKey:['project',project.id]});setMessage(custom?'已保存：切近景将完全按虚线框裁切，请重新检查样片。':'已保存：近景按自动半身推导，请重新检查样片。');}catch(e){setMessage((e as Error).message);}}}>保存人物区域</Button><p role="status">{message}</p>
  </section>;
}