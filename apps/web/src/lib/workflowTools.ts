import type { Turn } from './types';
import type { View } from '../stores';

export function errorGuide(error: string, kind: string): {target:View;steps:string[]} {
  const target = kind.startsWith('tts')?'voice':kind.startsWith('script')?'script':kind.startsWith('visual')||kind.startsWith('sample')?'visual':'render';
  if (/空间|no space|disk full|ENOSPC/i.test(error)) return {target:'home' as const, steps:['前往工程恢复与空间管理，查看剩余空间。','先备份，再回收可重建缓存；空间充足后重新提交任务。']};
  if (/401|403|凭据|unauthorized|api.?key/i.test(error)) return {target:'services' as const, steps:['核对凭据环境变量与模型权限。','重启服务并检测当前配置，再回到工程重新提交任务。']};
  if (/显存|out of memory|cuda/i.test(error)) return {target:'services' as const, steps:['停止其他占用显存的推理，检查本地引擎状态。','降低生成规格后先做短样片，再重新提交任务。']};
  if (/缺失|不存在|not found|no such file/i.test(error)) return {target, steps:['核对引用媒体、模型和模板是否仍在原位置。','从备份恢复缺失媒体或重新生成；不要直接覆盖旧模板目录。']};
  if (/connect|timeout|连接|超时/i.test(error)) return {target:'services' as const, steps:['确认引擎已经启动，核对服务地址并运行连接检测。','先核实外部请求是否已完成，再决定重试，避免重复调用。']};
  if (/409|冲突|版本|过期/i.test(error)) return {target, steps:['保留本机草稿，重新读取工程的最新版本。','比较修改后再保存或派生新草稿，不要反复覆盖重试。']};
  return {target, steps:['回到对应制作阶段，核对输入、已采用版本及环境。','已有结果保留；外部状态未知时先核实，再提交新任务。']};
}

export function replaceTurns(turns: Turn[], search: string, replacement: string, speaker: string) {
  if (!search) return {turns, count:0};
  let count = 0;
  const next = turns.map(t=>{
    if (speaker !== 'all' && t.speaker !== speaker) return t;
    const lines = t.lines.map(l=>{
      const matches=l.displayText.split(search).length-1;
      count+=matches;
      if(!matches)return l;
      // Custom pronunciation is independent from visible text and must survive.
      return {...l,displayText:l.displayText.split(search).join(replacement),spokenText:l.spokenText===l.displayText?l.spokenText.split(search).join(replacement):l.spokenText};
    });
    return lines.some((l,i)=>l!==t.lines[i])?{...t,lines}:t;
  });
  return {turns:count?next:turns,count};
}

export function filterProjects<T extends {id:string;title:string;updatedAt?:string}>(projects:T[], search:string, stage:string, sort:string, stageOf:(p:T)=>string) {
  const q=search.trim().toLocaleLowerCase();
  return projects.filter(p=>(!q||`${p.id} ${p.title}`.toLocaleLowerCase().includes(q))&&(stage==='all'||stageOf(p)===stage)).sort((a,b)=>{
    const result=sort==='title'?a.title.localeCompare(b.title):sort==='id'?a.id.localeCompare(b.id):sort==='oldest'?(a.updatedAt??'').localeCompare(b.updatedAt??''):(b.updatedAt??'').localeCompare(a.updatedAt??'');
    return result||a.id.localeCompare(b.id);
  });
}

export function scriptStats(turns: Turn[]) {
  const chars=turns.reduce((n,t)=>n+t.lines.reduce((m,l)=>m+Array.from(l.displayText.replace(/\s/g,'')).length,0),0);
  return {chars, minSeconds:Math.round(chars/5+turns.length*.2),maxSeconds:Math.round(chars/3+turns.length*.6)};
}

export function validScriptRecovery(value: unknown): value is {revisionId:string;turns:Turn[]} {
  if(!value||typeof value!=='object')return false;
  const data=value as {revisionId?:unknown;turns?:unknown};
  return typeof data.revisionId==='string'&&Array.isArray(data.turns)&&data.turns.every(t=>t&&typeof t.id==='string'&&(t.speaker==='A'||t.speaker==='B')&&Array.isArray(t.lines)&&t.lines.every((l: {id?:unknown;displayText?:unknown;spokenText?:unknown})=>l&&typeof l.id==='string'&&typeof l.displayText==='string'&&typeof l.spokenText==='string'));
}
