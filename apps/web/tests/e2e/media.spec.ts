import {test,expect,open,data} from './fixtures';
import {createHash} from 'node:crypto';

test('浏览器下载备份并导入，媒体内容保持一致',async({page,request},info)=>{
  await page.goto('/');const card=page.locator('.hf-pcard').filter({hasText:'CPU-MEDIA'});
  const download=page.waitForEvent('download');await card.getByRole('link',{name:'备份工程（含必要媒体）'}).click();
  const file=info.outputPath('backup.zip');await (await download).saveAs(file);
  await page.getByLabel('选择工程备份').setInputFiles(file);
  const result=page.getByRole('region',{name:'工程导入结果'});await expect(result).toBeVisible();
  const title=await result.getByRole('heading').innerText();const id=title.replace('工程已另存：','');
  const p=await data(request,id);expect(id).not.toBe('CPU-MEDIA');
  const source=await request.get('/api/artifacts/CPU-WAV/file');const target=await request.get(`/api/artifacts/${p.audioTimeline.masterAudioAssetId}/file`);
  expect(source.ok()&&target.ok()).toBeTruthy();const hash=(b:Buffer)=>createHash('sha256').update(b).digest('hex');expect(hash(await target.body())).toBe(hash(await source.body()));
});

for(const width of [640,900,1440])test(`波形选段与布局 ${width}px`,async({page},info)=>{
  await page.setViewportSize({width,height:1000});await open(page,'CPU-MEDIA','试听配音');
  const wave=page.getByRole('region',{name:'真实音频波形与选段'});await wave.getByRole('button',{name:'加载真实音频波形'}).click();
  await expect(wave.getByRole('img')).toBeVisible();await wave.scrollIntoViewIfNeeded();
  await page.getByLabel('波形定位秒').fill('2.5');
  await page.getByLabel('选段起点秒').fill('0.5');await page.getByLabel('选段终点秒').fill('1.5');await wave.getByRole('button',{name:'试听选段'}).click();
  await expect.poll(async()=>Number(await page.getByLabel('波形定位秒').inputValue())).toBe(1.5);
  await page.waitForTimeout(300);expect(Number(await page.getByLabel('波形定位秒').inputValue())).toBe(1.5);
  await page.getByLabel('选段起点秒').fill('');await expect(wave.getByRole('button',{name:'试听选段'})).toBeDisabled();
  expect(await wave.locator('svg').innerHTML()).not.toContain('NaN');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  await page.screenshot({path:info.outputPath(`waveform-${width}.png`),fullPage:true});
});
