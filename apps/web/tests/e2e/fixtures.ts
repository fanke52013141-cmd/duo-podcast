import {test as base, expect, type APIRequestContext, type Page} from '@playwright/test';
import {randomUUID} from 'node:crypto';
export const original='第一句保留原文。';
export const turns=(text=original)=>[{id:'T1',speaker:'A',lines:[{id:'L1',displayText:text,spokenText:text}]}];
export async function project(request:APIRequestContext, approved=false, versions=false){
  const response=await request.post('/api/projects',{data:{title:`E2E-${randomUUID().slice(0,8)}`}});expect(response.ok()).toBeTruthy();
  const p=await response.json();
  const patch=await request.patch(`/api/projects/${p.id}`,{data:{expectedRevision:p.revision,scriptRevisions:[{id:'R1',turns:turns()},...(versions?[{id:'R2',turns:turns('另一分支的原文。')}]:[])],currentDraftRevision:versions?'R2':'R1',...(approved?{approvals:[{revisionId:'R1',approvedAt:new Date().toISOString()}]}:{})}});
  expect(patch.ok()).toBeTruthy();return p.id as string;
}
export async function data(request:APIRequestContext,id:string){const r=await request.get(`/api/projects/${id}`);expect(r.ok()).toBeTruthy();return r.json();}
export async function open(page:Page,id:string,stage='编辑对话'){
  await page.goto('/');await page.locator('.hf-pcard').filter({hasText:id}).getByRole('button',{name:'继续制作'}).click();
  await page.getByRole('navigation',{name:'阶段导航'}).getByRole('button',{name:new RegExp(stage)}).click();
}
export async function edit(page:Page,old:string,text:string){await page.getByText(old,{exact:true}).dblclick();await page.locator('.hf-turn textarea').fill(text);}
export const test=base.extend<{pageErrors:void}>({pageErrors:[async({page,baseURL},use)=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.context().route('**/*',route=>new URL(route.request().url()).origin===new URL(baseURL!).origin?route.continue():route.abort());
  await use();expect(errors).toEqual([]);
},{auto:true}]});
export {expect};
