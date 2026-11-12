import {test,expect,project,data,open,edit,original} from './fixtures';

test('派生草稿响应延迟时继续输入不被旧快照覆盖',async({page,request})=>{
  const id=await project(request,false,true);await open(page,id);await page.getByLabel('选择版本').selectOption('R1');
  let release!:()=>void,arrived!:()=>void;let first=true;
  const gate=new Promise<void>(r=>release=r),received=new Promise<void>(r=>arrived=r);
  await page.route(`**/api/projects/${id}`,async route=>{
    if(route.request().method()!=='PATCH'||!first)return route.continue();first=false;
    const response=await route.fetch();arrived();await gate;await route.fulfill({response});
  });
  try{
    await edit(page,original,'第一次保存的文字。');await page.locator('.hf-turn textarea').blur();await received;
    await edit(page,'第一次保存的文字。','保存期间继续输入的最新文字。');await page.locator('.hf-turn textarea').blur();release();
    await expect.poll(async()=>{const p=await data(request,id);return p.scriptRevisions.find((r:any)=>r.id===p.currentDraftRevision).turns[0].lines[0].displayText;}).toBe('保存期间继续输入的最新文字。');
    await expect(page.getByText('保存期间继续输入的最新文字。',{exact:true})).toBeVisible();
    expect((await data(request,id)).scriptRevisions.find((r:any)=>r.id==='R1').turns[0].lines[0].displayText).toBe(original);
  }finally{release();}
});

test('未失焦输入在下一步前保存',async({page,request})=>{
  const id=await project(request);await open(page,id);await edit(page,original,'离页前完整保存。');
  await page.getByRole('button',{name:'下一步',exact:true}).click();
  await expect.poll(async()=>JSON.stringify((await data(request,id)).scriptRevisions)).toContain('离页前完整保存。');
  await expect(page.getByRole('navigation',{name:'阶段导航'}).getByRole('button',{name:/配音/})).toHaveAttribute('aria-current','page');
});

test('断连阻止离页，刷新后恢复为新草稿',async({page,request})=>{
  const id=await project(request);await open(page,id);
  await page.route(`**/api/projects/${id}`,route=>route.request().method()==='PATCH'?route.abort():route.continue());
  await edit(page,original,'断连仍保留的本机文字。');await page.getByRole('button',{name:'下一步',exact:true}).click();
  await expect(page.getByRole('navigation',{name:'阶段导航'}).getByRole('button',{name:/编辑对话/})).toHaveAttribute('aria-current','page');
  expect(JSON.stringify((await data(request,id)).scriptRevisions)).not.toContain('断连仍保留');
  await page.unroute(`**/api/projects/${id}`);page.once('dialog',d=>d.accept());await page.reload();
  await page.getByRole('button',{name:'恢复为新草稿（保留服务端版本）'}).click();
  await expect.poll(async()=>JSON.stringify((await data(request,id)).scriptRevisions)).toContain('断连仍保留');
  const p=await data(request,id);expect(p.scriptRevisions.find((r:any)=>r.id==='R1').turns[0].lines[0].displayText).toBe(original);
});

test('历史版本编辑派生草稿，切换前落盘',async({page,request})=>{
  const id=await project(request,false,true);await open(page,id);const select=page.getByLabel('选择版本');
  await select.selectOption('R1');await edit(page,original,'历史分支的新文字。');await select.selectOption('R2');
  await expect(page.getByText('另一分支的原文。',{exact:true})).toBeVisible();
  const p=await data(request,id);expect(p.scriptRevisions.find((r:any)=>r.id==='R1').turns[0].lines[0].displayText).toBe(original);
  expect(JSON.stringify(p.scriptRevisions)).toContain('历史分支的新文字。');
});

test('真实409保留本机稿且不重试覆盖远端',async({page,request})=>{
  const id=await project(request);await open(page,id);let release!:()=>void,arrived!:()=>void,calls=0;
  const gate=new Promise<void>(r=>release=r),received=new Promise<void>(r=>arrived=r);
  await page.route(`**/api/projects/${id}`,async route=>{if(route.request().method()!=='PATCH')return route.continue();calls++;arrived();await gate;await route.continue();});
  try{
    await edit(page,original,'本机冲突稿。');await page.locator('.hf-turn textarea').blur();await received;
    const p=await data(request,id);const r=await request.patch(`/api/projects/${id}`,{data:{expectedRevision:p.revision,title:'远端已更新'}});expect(r.ok()).toBeTruthy();release();
    await expect(page.getByText(/409/).first()).toBeVisible();await expect(page.getByText('本机冲突稿。',{exact:true})).toBeVisible();
    expect(calls).toBe(1);expect((await data(request,id)).title).toBe('远端已更新');
  }finally{release();}
});
