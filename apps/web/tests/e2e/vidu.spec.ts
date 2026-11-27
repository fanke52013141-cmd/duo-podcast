import {test,expect,project,open} from './fixtures';

function capabilities(videoMode:'mock'|'live'='mock'){
  const channel=(mode:'mock'|'live')=>({ready:mode==='live',mode,capabilities:{}});
  return {textApi:channel('mock'),imageApi:channel('mock'),tts:channel('mock'),video:channel(videoMode)};
}

function machine(videoProvider='vidu',videoMode:'mock'|'live'='live',credentialPresent=true){
  return {
    runtimeConfig:{},
    activeConfig:{
      video_provider:videoProvider,
      vidu_base_url:'https://service.vidu.cn',
      vidu_model:'3.2',
      viduCredentialPresent:credentialPresent,
    },
    providerProfiles:{},vramAllocation:{},queueCaps:{},capabilities:capabilities(videoMode),
  };
}

async function mockMachine(page:any,settings:Record<string,unknown>){
  await page.route('**/api/machine',async(route:any)=>{
    if(route.request().method()!=='GET')return route.continue();
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(settings)});
  });
}

test('Vidu 服务设置只保存公开配置，不接收密钥',async({page})=>{
  const settings=machine('mock','mock',false);
  settings.activeConfig.vidu_model='3.4';
  await mockMachine(page,settings);
  let validationBody:any=null;
  await page.route('**/api/machine/validate-config',async(route:any)=>{
    validationBody=route.request().postDataJSON();
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({valid:true,errors:[],warnings:[]})});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'服务设置',exact:true}).click();
  const provider=page.getByLabel('视频提供方');
  await expect(provider).toHaveValue('mock');
  await provider.selectOption('vidu');
  await expect(page.getByLabel('Vidu BaseURL')).toHaveValue('https://service.vidu.cn');
  await expect(page.getByLabel('Vidu 模型')).toHaveValue('3.4');
  await expect(page.getByText(/DUOCAST_VIDU_TOKEN/)).toBeVisible();
  await expect(page.getByText(/此页面不接收或保存密钥/)).toBeVisible();
  await page.getByLabel('Vidu BaseURL').selectOption('https://service.vidu.com');
  await page.getByLabel('Vidu 模型').selectOption('3.2');
  await page.getByRole('button',{name:'保存前校验',exact:true}).click();
  await expect(page.getByText(/本机配置格式校验通过/)).toBeVisible();
  expect(validationBody.runtimeConfig).toMatchObject({
    video_provider:'vidu',vidu_base_url:'https://service.vidu.com',vidu_model:'3.2',
  });
  expect(JSON.stringify(validationBody)).not.toContain('DUOCAST_VIDU_TOKEN":');
  await provider.selectOption('comfyui');
  await expect(page.getByLabel('视频引擎地址')).toBeVisible();
  await expect(page.getByLabel('Vidu BaseURL')).toHaveCount(0);
});

test('Vidu API 表单校验参数并只提交到 route mock',async({page,request})=>{
  const id=await project(request);
  const source=await (await request.get(`/api/projects/${id}`)).json();
  const visualVariants=['V1','V2','V3'].map(variantId=>({
    id:variantId,
    masterImage:{artifactId:`IMG-${variantId}`,path:`${variantId}.png`,fileHash:`hash-${variantId}`},
    aspect:'landscape',modelInputTransform:{scale:1,offsetX:0,offsetY:0,pad:'none'},
    personRegions:{},imageApiConfigRef:null,mode:'twoShot',
  }));
  const fixture={
    ...source,currentDraftRevision:'R1',selectedVisualVariantId:'V2',visualVariants,
    audioTimeline:{
      revisionId:'R1',sampleRate:48000,sampleCount:48000*8,units:[],unitOffsets:{},
      transitionGapMs:[],leadInMs:0,tailOutMs:0,annotationVersion:'A1',masterAudioAssetId:'MASTER-AUDIO',
    },
  };
  await page.route(`**/api/projects/${id}`,async(route:any)=>{
    if(route.request().method()!=='GET')return route.continue();
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fixture)});
  });
  await page.route('**/api/jobs?*',route=>route.fulfill({status:200,contentType:'application/json',body:'[]'}));
  await mockMachine(page,machine('vidu','live',true));
  await page.route('**/api/providers/capabilities',route=>route.fulfill({
    status:200,contentType:'application/json',body:JSON.stringify(capabilities('live')),
  }));
  let submitted:any=null;
  await page.route(`**/api/projects/${id}/render/generate`,async(route:any)=>{
    submitted=route.request().postDataJSON();
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({jobId:'JOB-VIDU-E2E',clientToken:submitted.clientToken})});
  });
  await open(page,id,'生成导出');
  await page.getByLabel('成片工作模式').selectOption('api');
  const submit=page.getByRole('button',{name:'Vidu API 生成',exact:true});
  await expect(submit).toBeDisabled();
  await expect(page.getByText(/请填写 Vidu 提示词/)).toBeVisible();
  await expect(page.getByLabel('Vidu 模型')).toHaveValue('3.2');
  await expect(page.getByLabel('Vidu 分辨率')).toHaveValue('1080p');
  await expect(page.getByLabel('Vidu 生成模式').locator('option')).toHaveCount(4);
  await page.getByLabel('Vidu 模型').selectOption('3.4');
  await expect(page.getByLabel('Vidu 生成模式')).toHaveValue('reference');
  await expect(page.getByLabel('Vidu 生成模式').locator('option')).toHaveCount(1);
  await expect(page.getByLabel('Vidu 分辨率').locator('option')).toHaveCount(5);
  await page.getByLabel('Vidu 模型').selectOption('3.2');
  await page.getByLabel('Vidu 生成模式').selectOption('first-last-frame');
  await expect(page.getByLabel('Vidu 图像 V2')).toBeChecked();
  await expect(page.getByLabel('Vidu 图像 V3')).toBeChecked();
  await expect(page.getByLabel('Vidu 图像 V1')).not.toBeChecked();
  await page.getByLabel('Vidu 提示词').fill('双人播客，镜头缓慢推进，动作自然。');
  await page.getByLabel('Vidu 时长（秒）').fill('17');
  await expect(submit).toBeDisabled();
  await expect(page.getByText(/时长须为 1–16 秒整数/)).toBeVisible();
  await page.getByLabel('Vidu 时长（秒）').fill('8');
  await page.getByLabel('Vidu 画幅').selectOption('9:16');
  await page.getByLabel('叠加当前母轨').check();
  await page.getByLabel('母轨起点秒（rangeStartSec）').fill('1.5');
  await expect(submit).toBeDisabled();
  await expect(page.getByText(/配音区间超出母轨总长/)).toBeVisible();
  await page.getByLabel('母轨起点秒（rangeStartSec）').fill('0');
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText(/Vidu API 任务已提交/)).toBeVisible();
  expect(submitted).toMatchObject({
    provider:'vidu',prompt:'双人播客，镜头缓慢推进，动作自然。',viduModel:'3.2',
    viduMode:'first-last-frame',viduDuration:8,viduResolution:'1080p',viduRatio:'9:16',
    imageArtifactIds:['IMG-V2','IMG-V3'],withMasterAudio:true,rangeStartSec:0,
    candidate:true,revisionId:'R1',fps:25,
  });
  expect(submitted.clientToken).toBeTruthy();
  expect(submitted.resolution).toBeTruthy();
});

test('Vidu 生效时停用本地生成，缺 Token 时禁止 API 提交',async({page,request})=>{
  const id=await project(request);
  await mockMachine(page,machine('vidu','live',false));
  await page.route('**/api/providers/capabilities',route=>route.fulfill({
    status:200,contentType:'application/json',body:JSON.stringify(capabilities('live')),
  }));
  await open(page,id,'生成导出');
  await expect(page.getByText(/当前生效的视频提供方为 Vidu，本地生成已停用/)).toBeVisible();
  await expect(page.getByRole('button',{name:'生成候选成片',exact:true})).toBeDisabled();
  await page.getByLabel('成片工作模式').selectOption('api');
  await expect(page.getByRole('button',{name:'Vidu API 生成',exact:true})).toBeDisabled();
  await expect(page.getByText(/未读取 DUOCAST_VIDU_TOKEN/)).toBeVisible();
});
