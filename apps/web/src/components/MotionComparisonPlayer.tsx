import { useEffect, useRef, useState, type RefObject } from 'react';
import type { FaceBox, MotionClip } from '../lib/motionPacks';
import { createMotionComparison, sourceReference } from '../lib/motionComparison';

const fileUrl=(id:string)=>`/api/artifacts/${encodeURIComponent(id)}/file`;

export function MotionComparisonPlayer({clip, video, boxes, currentFrame, setCurrentFrame}:{
  clip:MotionClip; video:RefObject<HTMLVideoElement>; boxes:(FaceBox|null)[];
  currentFrame:number; setCurrentFrame:(frame:number)=>void;
}) {
  const source = sourceReference(clip.provenance?.sourceMedia);
  const original = useRef<HTMLVideoElement>(null);
  const controller = useRef<ReturnType<typeof createMotionComparison>|null>(null);
  const [offset,setOffset] = useState('0');
  const [ready,setReady] = useState(false);
  const [message,setMessage] = useState('');
  useEffect(()=>{
    setOffset('0'); setReady(false); setMessage('');
    if (!source || !video.current || !original.current) return;
    const pair = createMotionComparison(video.current, original.current, setMessage);
    controller.current=pair;
    return ()=>{pair.dispose(); if(controller.current===pair)controller.current=null;};
  },[clip.id,clip.media.artifactId,source?.artifactId,source?.fileHash,video]);
  useEffect(()=>{
    if (ready && controller.current) controller.current.setOffset(offset.trim()===''?NaN:Number(offset));
  },[offset,ready]);

  return <section aria-label="动作素材对比" className="motion-comparison">
    <h3>素材对比</h3>
    <p>处理后视频为播放时钟。原片时间 = 处理后时间 + 偏移；播放、暂停、拖动和速度按该时钟同步。若素材经过变速、倒放或插帧，固定偏移不能完整对齐，请逐段核对。</p>
    {!source && <p role="status">该片段尚未绑定可用的已登记原始视频；仍可单独审看处理后素材。</p>}
    {source && <label>原片对齐偏移（秒）<input type="number" step="0.04" value={offset} onChange={e=>setOffset(e.target.value)} aria-label="原片对齐偏移秒"/></label>}
    {message && <p role="status">{message}</p>}
    <div className="motion-comparison-videos">
      {source && <div><strong>已登记原始视频</strong><video key={source.artifactId} ref={original} muted playsInline preload="metadata"
        aria-label="原始视频同步预览" src={fileUrl(source.artifactId)}
        onLoadedMetadata={()=>setReady(true)} onError={()=>{setReady(false);controller.current?.unavailable();}}
        onEnded={()=>controller.current?.ended()}/></div>}
      <div><strong>处理后视频</strong><div className="motion-comparison-overlay">
        <video key={clip.media.artifactId} ref={video} controls playsInline preload="metadata" aria-label="处理后视频主时钟"
          src={fileUrl(clip.media.artifactId)}
          onPlay={()=>{if(source)void controller.current?.play();}}
          onPause={()=>controller.current?.pause()}
          onRateChange={()=>controller.current?.sync(true)}
          onSeeking={()=>controller.current?.sync(true)}
          onSeeked={e=>{setCurrentFrame(Math.min(clip.frameCount-1,Math.round(e.currentTarget.currentTime*clip.fps)));controller.current?.sync(true);}}
          onTimeUpdate={e=>{setCurrentFrame(Math.min(clip.frameCount-1,Math.floor(e.currentTarget.currentTime*clip.fps)));controller.current?.sync();}}
          onEnded={()=>controller.current?.pause()}/>
        {boxes[currentFrame]&&<svg aria-label={`第${currentFrame}帧人脸框预览`} viewBox={`0 0 ${clip.nativeWidth} ${clip.nativeHeight}`} preserveAspectRatio="xMidYMid meet"><rect x={boxes[currentFrame]![0]} y={boxes[currentFrame]![1]} width={boxes[currentFrame]![2]-boxes[currentFrame]![0]} height={boxes[currentFrame]![3]-boxes[currentFrame]![1]} fill="none" stroke="#f5c450" strokeWidth={Math.max(1,clip.nativeWidth/400)}/></svg>}
      </div></div>
    </div>
    <p>对比只辅助人工观察；同步播放不代表五官、身份或动作质量已通过。</p>
  </section>;
}
