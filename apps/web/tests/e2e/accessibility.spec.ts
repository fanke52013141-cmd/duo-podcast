import {test,expect,project,open} from './fixtures';

test('弹窗 Escape 返回触发按钮，选项卡支持方向键和首尾键',async({page,request})=>{
  await page.goto('/');const trigger=page.getByRole('button',{name:'新建本期',exact:true});await trigger.click();
  const dialog=page.getByRole('dialog',{name:'新建本期'});await expect(dialog).toBeVisible();await page.keyboard.press('Escape');await expect(dialog).not.toBeVisible();await expect(trigger).toBeFocused();
  const id=await project(request);await open(page,id,'创建本期');
  const topic=page.getByRole('tab',{name:'从话题开始'});await topic.focus();await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'粘贴文章'})).toBeFocused();await page.keyboard.press('End');await expect(page.getByRole('tab',{name:'已有脚本'})).toBeFocused();
  await page.keyboard.press('Home');await expect(topic).toBeFocused();
});

test('真实视频逐帧定位带动原片同步',async({page})=>{
  await page.goto('/');await page.getByRole('button',{name:'动作资产库与审看',exact:true}).click();
  await page.getByRole('combobox',{name:/^资产包/}).selectOption('CPU-REVIEW');
  await page.getByRole('combobox',{name:/^选择片段/}).selectOption('clip-A');
  const frame=page.getByLabel('审看帧位置');await expect(frame).toBeVisible();await frame.fill('25');
  await expect(frame).toHaveValue('25');const processed=page.getByLabel('处理后视频主时钟');
  await expect.poll(()=>processed.evaluate((el:HTMLVideoElement)=>el.currentTime)).toBe(1);
  await page.getByRole('button',{name:'下一帧',exact:true}).click();await expect(frame).toHaveValue('26');
  const source=page.getByLabel('原始视频同步预览');await expect.poll(()=>source.evaluate((el:HTMLVideoElement)=>el.currentTime)).toBeCloseTo(1.04,2);
});

test('动作包409保留草稿并可另存新包',async({page,request})=>{
  const source=await (await request.get('/api/motion-packs/CPU-REVIEW')).json();
  const draft={name:'浏览器冲突测试包',description:source.description,characters:source.characters,cameras:source.cameras,clips:source.clips};
  const created=await request.post('/api/motion-packs',{data:draft});expect(created.ok()).toBeTruthy();const pack=await created.json();
  await page.goto('/');await page.getByRole('button',{name:'动作资产库与审看',exact:true}).click();await page.getByRole('combobox',{name:/^资产包/}).selectOption(pack.id);
  const name=page.getByLabel('名称',{exact:true}).first();await expect(name).toHaveValue(draft.name);await name.fill('需要保留的本机动作草稿');
  const remote=await request.put(`/api/motion-packs/${pack.id}`,{data:{expectedRevision:pack.revision,draft:{...draft,name:'已更新的远端动作包'}}});expect(remote.ok()).toBeTruthy();
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();await expect(page.getByRole('status')).toContainText('本地草稿已保留');await expect(name).toHaveValue('需要保留的本机动作草稿');
  await page.getByRole('button',{name:'另存为新资产包',exact:true}).click();await expect(page.getByRole('status')).toContainText('已另存新资产包');
  expect(await page.getByRole('combobox',{name:/^资产包/}).inputValue()).not.toBe(pack.id);
  expect((await (await request.get(`/api/motion-packs/${pack.id}`)).json()).name).toBe('已更新的远端动作包');
});
