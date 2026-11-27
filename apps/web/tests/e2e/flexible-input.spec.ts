import {execFileSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {test,expect,project,open,data} from './fixtures';

async function emptyProject(request:any){
  const response=await request.post('/api/projects',{data:{title:`FREE-${randomUUID().slice(0,8)}`}});
  expect(response.ok()).toBeTruthy();return (await response.json()).id as string;
}

function wav(seconds=0.2){
  const samples=Math.round(48000*seconds),buffer=Buffer.alloc(44+samples*2);
  buffer.write('RIFF',0);buffer.writeUInt32LE(36+samples*2,4);buffer.write('WAVEfmt ',8);
  buffer.writeUInt32LE(16,16);buffer.writeUInt16LE(1,20);buffer.writeUInt16LE(1,22);
  buffer.writeUInt32LE(48000,24);buffer.writeUInt32LE(96000,28);buffer.writeUInt16LE(2,32);
  buffer.writeUInt16LE(16,34);buffer.write('data',36);buffer.writeUInt32LE(samples*2,40);
  return buffer;
}

test('文案模板可下载，TXT 直接导入为新草稿',async({page,request})=>{
  const id=await emptyProject(request);await open(page,id,'创建本期');
  await page.getByRole('tab',{name:'已有脚本'}).click();
  const downloading=page.waitForEvent('download');
  await page.getByRole('button',{name:'TXT 模板',exact:true}).click();
  expect((await downloading).suggestedFilename()).toBe('duocast-script-template.txt');
  await page.getByLabel('上传脚本文件').setInputFiles({
    name:'episode.txt',mimeType:'text/plain',buffer:Buffer.from('A: 欢迎收听。\nB: 今天直接导入。\n','utf8'),
  });
  await expect(page.getByText(/已导入 2 条话轮/)).toBeVisible();
  const saved=await data(request,id);
  expect(saved.currentDraftRevision).toBeTruthy();
  const revision=saved.scriptRevisions.find((item:any)=>item.id===saved.currentDraftRevision);
  expect(revision.turns.map((turn:any)=>turn.speaker)).toEqual(['A','B']);
  expect(revision.turns[1].lines[0].displayText).toBe('今天直接导入。');
});

test('无需先运行 TTS，上传单条录音后即可采用并确认',async({page,request})=>{
  const id=await project(request);await open(page,id,'试听配音');
  const upload=page.getByRole('button',{name:'上传音频',exact:true});
  await expect(upload).toBeEnabled();await upload.click();
  await page.getByLabel('上传单元音频').setInputFiles({
    name:'voice.wav',mimeType:'audio/wav',buffer:wav(),
  });
  await expect(page.getByText(/已上传 voice.wav/)).toBeVisible();
  const adopt=page.getByRole('button',{name:'采纳',exact:true});
  await expect(adopt).toBeVisible();await adopt.click();
  await expect.poll(async()=>(await data(request,id)).audioTimeline?.masterAudioAssetId).toBeTruthy();
  const confirm=page.getByRole('button',{name:'确认配音',exact:true});
  await expect(confirm).toBeEnabled();await confirm.click();
  await expect(page.getByRole('button',{name:'已确认',exact:true})).toBeVisible();
});

test('MiniMax 与火山服务配置可切换并做无网络校验',async({page})=>{
  await page.goto('/');await page.getByRole('button',{name:'服务设置',exact:true}).click();
  const provider=page.getByLabel('语音提供方');
  await provider.selectOption('minimax');
  await expect(page.getByLabel('MiniMax 服务根地址')).toHaveValue('https://api.minimaxi.com');
  await page.getByLabel('MiniMax B voice_id').fill('voice-b-fixture');
  await page.getByRole('button',{name:'保存前校验',exact:true}).click();
  await expect(page.getByText(/本机配置格式校验通过/)).toBeVisible();
  await provider.selectOption('volcengine');
  await expect(page.getByLabel('火山语音服务根地址')).toHaveValue('https://openspeech.bytedance.com');
  await page.getByLabel('火山 A speaker').fill('speaker-a-fixture');
  await page.getByLabel('火山 B speaker').fill('speaker-b-fixture');
  await page.getByRole('button',{name:'保存前校验',exact:true}).click();
  await expect(page.getByText(/本机配置格式校验通过/)).toBeVisible();
});

test('直接上传视频不需要脚本或时间轨，可采用并下载',async({page,request},info)=>{
  const id=await emptyProject(request);await open(page,id,'生成导出');
  await page.getByLabel('成片工作模式').selectOption('direct');
  await expect(page.getByText(/不经过本地合成，也不需要脚本或配音时间轨/)).toBeVisible();
  const file=info.outputPath('direct.mp4');
  execFileSync('ffmpeg',['-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x96:r=25:d=1',
    '-f','lavfi','-i','anullsrc=r=48000:cl=mono','-t','1','-c:v','libx264','-pix_fmt','yuv420p',
    '-c:a','aac','-shortest',file],{stdio:'ignore'});
  await page.getByLabel('直接上传视频文件').setInputFiles(file);
  await expect(page.getByText(/已上传 direct.mp4/)).toBeVisible();
  await expect(page.locator('video')).toBeVisible();
  await page.getByRole('button',{name:'采用候选',exact:true}).click();
  await expect.poll(async()=>(await data(request,id)).outputVersion).toBe('OUT-DIRECT-v1');
  await expect(page.getByText(/当前采用：OUT-DIRECT-v1/)).toBeVisible();
  await expect(page.getByRole('link',{name:'下载完整视频'})).toBeVisible();
});
