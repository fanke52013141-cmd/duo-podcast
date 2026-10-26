import { ApiError, getJson, humanizeError, sendJson } from './api';

export type Region = { x: number; y: number; width: number; height: number };
export type MediaRef = { artifactId: string; fileHash: string };
export type MotionCharacter = { characterId: string; name: string; objectType: 'human'|'rigid_object'|'articulated_object'|'other'; referenceImage: MediaRef|null; motionNotes: string };
export type MotionCamera = { id: string; mode: 'twoShot'|'overShoulder'; masterImage: MediaRef|null; subjectSpeaker: 'A'|'B'|null; foregroundSpeaker: 'A'|'B'|null; subjectRegion: Region };
export type MotionClip = { id: string; role: 'A'|'B'; cameraId: string; state: 'speaking'|'listening'|'responding'; media: MediaRef; nativeWidth: number; nativeHeight: number; fps: number; frameCount: number; usableStartFrame: number; usableEndFrame: number; bodyRegion: Region; provenance?: Record<string, unknown>; faceCoordinates?: MediaRef|null };
export type MotionDraft = { name: string; description: string; characters: Partial<Record<'A'|'B', MotionCharacter>>; cameras: MotionCamera[]; clips: MotionClip[] };
export type MotionReport = { status: 'passed'|'failed'|'needs_review'; hardFailures: string[]; issues: { code: string; message: string; startFrame?: number; endFrame?: number; severity?: string }[]; native: Record<string, unknown>; metrics: Record<string, unknown>; evidence: Record<string, unknown> };
export type MotionPack = MotionDraft & { id: string; revision: number; version: string; checks: Record<string, {contentHash: string; report: MotionReport}>; reviews: Record<string, {accepted: boolean; reason: string}> };
export type MotionReadiness = {ready: boolean; missing: string[]; clipIds: string[]};
export type InspectionRegions = {bodyRegions: Region[]; exclusionRegions: Region[]; backgroundRegion: Region};
export type FaceBox = [number,number,number,number];
export function selectedFixedBoxes(clip:MotionClip,box:FaceBox):(FaceBox|null)[]{
  if(!Number.isInteger(clip.frameCount)||clip.frameCount>36000||clip.frameCount<1||box.some(n=>!Number.isInteger(n))||!(0<=box[0]&&box[0]<box[2]&&box[2]<=clip.nativeWidth&&0<=box[1]&&box[1]<box[3]&&box[3]<=clip.nativeHeight))throw new Error('人脸框须为原生画面内的整数像素坐标，源视频不超过36000帧。');
  return Array.from({length:clip.frameCount},(_,i)=>i>=clip.usableStartFrame&&i<clip.usableEndFrame?[...box] as FaceBox:null);
}
export function validateFaceBoxes(clip:MotionClip,value:unknown):(FaceBox|null)[]{
  if(!Array.isArray(value)||value.length!==clip.frameCount||value.length>36000)throw new Error('JSON必须为与源视频完整帧数相同的boxes数组。');
  for(let i=0;i<value.length;i++){
    const box=value[i];
    if(box===null&&!(i>=clip.usableStartFrame&&i<clip.usableEndFrame))continue;
    if(!Array.isArray(box)||box.length!==4||box.some(n=>typeof n!=='number'||!Number.isInteger(n))||!(0<=box[0]&&box[0]<box[2]&&box[2]<=clip.nativeWidth&&0<=box[1]&&box[1]<box[3]&&box[3]<=clip.nativeHeight))throw new Error(`第${i}帧缺少合法人脸框`);
  }
  return value as (FaceBox|null)[];
}
export const saveMotionCoordinates=(pack:MotionPack,clipId:string,boxes:(FaceBox|null)[])=>sendJson<MotionPack>(`${path(pack.id)}/clips/${encodeURIComponent(clipId)}/coordinates`,'POST',{expectedRevision:pack.revision,boxes});
export const fullRegion = (): Region => ({x:0,y:0,width:1,height:1});
export const emptyMotionDraft = (): MotionDraft => ({name:'新动作资产包',description:'',characters:{},cameras:[],clips:[]});

// Explicit whitelist: a returned pack's trusted checks, reviews, IDs and paths
// never travel back as editable draft metadata.
export function motionDraft(pack: MotionDraft): MotionDraft {
  return {name:pack.name,description:pack.description,characters:pack.characters,cameras:pack.cameras,clips:pack.clips};
}
export function adoptionBlock(report: MotionReport|undefined, reason: string, dirty: boolean): string {
  if(dirty)return '请先保存草稿并重新检查';
  if(!report)return '请先运行服务端检查';
  if(report.status==='failed'||report.hardFailures.length)return '检查失败，修复后重新检查';
  if(!reason.trim())return '请填写人工审看结论';
  return '';
}
export function parseInspectionRegions(text: string): InspectionRegions {
  const value = JSON.parse(text) as InspectionRegions;
  const valid=(r:Region)=>r && Object.keys(r).sort().join(',')==='height,width,x,y' && [r.x,r.y,r.width,r.height].every(n=>typeof n==='number'&&Number.isFinite(n)) && r.x>=0&&r.y>=0&&r.width>0&&r.height>0&&r.x+r.width<=1&&r.y+r.height<=1;
  if(Object.keys(value).sort().join(',')!=='backgroundRegion,bodyRegions,exclusionRegions'||!Array.isArray(value.bodyRegions)||value.bodyRegions.length<1||value.bodyRegions.length>8||!Array.isArray(value.exclusionRegions)||value.exclusionRegions.length<1||value.exclusionRegions.length>16||!value.bodyRegions.every(valid)||!value.exclusionRegions.every(valid)||!valid(value.backgroundRegion))throw new Error('区域需在0到1内：身体1–8个，排除1–16个，独立背景1个；仅填写x/y/width/height。');
  return value;
}
const base='/motion-packs';
const path=(id:string)=>`${base}/${encodeURIComponent(id)}`;
export const listMotionPacks=()=>getJson<MotionPack[]>(base);
export const loadMotionPack=(id:string)=>getJson<MotionPack>(path(id));
export const createMotionPack=(draft:MotionDraft)=>sendJson<MotionPack>(base,'POST',motionDraft(draft));
export async function saveMotionPack(pack:MotionPack,draft:MotionDraft):Promise<MotionPack>{
  const response=await fetch('/api'+path(pack.id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({expectedRevision:pack.revision,draft:motionDraft(draft)})});
  if(!response.ok)throw new ApiError(response.status,humanizeError(response.status,await response.text()));
  return response.json();
}
export const inspectMotionClip=(pack:MotionPack,clipId:string,regions:InspectionRegions)=>sendJson<MotionPack>(`${path(pack.id)}/clips/${encodeURIComponent(clipId)}/inspect`,'POST',{expectedRevision:pack.revision,...regions});
export const reviewMotionClip=(pack:MotionPack,clipId:string,accepted:boolean,reason:string)=>sendJson<MotionPack>(`${path(pack.id)}/clips/${encodeURIComponent(clipId)}/review`,'POST',{expectedRevision:pack.revision,accepted,reason:reason.trim()});
export const motionReadiness=(id:string,style:string)=>getJson<MotionReadiness>(`${path(id)}/readiness?style=${encodeURIComponent(style)}`);
export async function uploadMotionVideo(file:File):Promise<{artifactId:string;fileHash:string;native:{width:number;height:number;fps:number;frames:number}}>{
  if(file.size>200*1024*1024)throw new Error('动作视频不得超过200MB');
  const extension=file.name.split('.').pop()?.toLowerCase();
  if(!extension||!['mp4','mov','mkv','webm','avi'].includes(extension))throw new Error('请选择MP4/MOV/MKV/WebM/AVI视频');
  const response=await fetch(`/api/motion-media?extension=${extension}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
  if(!response.ok)throw new ApiError(response.status,humanizeError(response.status,await response.text()));
  return response.json();
}
export async function uploadMotionImage(file:File):Promise<MediaRef>{
  if(file.size>10*1024*1024)throw new Error('图片不得超过10MB');
  const response=await fetch('/api/motion-media/images',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
  if(!response.ok)throw new ApiError(response.status,humanizeError(response.status,await response.text()));
  return response.json();
}
