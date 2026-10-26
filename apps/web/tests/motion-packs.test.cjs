const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const ts=require('typescript');
function load(fetch=async()=>{throw Error('unexpected fetch');}){
  const exports={};
  class ApiError extends Error{constructor(status,message){super(message);this.status=status;}}
  const api={ApiError,humanizeError:(status,text)=>`${status}: ${text}`,getJson:async url=>(await fetch('/api'+url)).json(),sendJson:async(url,method,body)=>{const res=await fetch('/api'+url,{method,body:JSON.stringify(body)});if(!res.ok)throw new ApiError(res.status,await res.text());return res.json();}};
  const source=fs.readFileSync(require('node:path').join(__dirname,'../src/lib/motionPacks.ts'),'utf8');
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,require:()=>api,fetch});
  return {helper:exports,ApiError};
}
const regions={bodyRegions:[{x:.2,y:.3,width:.4,height:.5}],exclusionRegions:[{x:.3,y:.1,width:.2,height:.2}],backgroundRegion:{x:0,y:0,width:.15,height:1}};
test('adoption requires server evidence, clean saved draft and human reason; hard failure cannot be overridden',()=>{
  const {helper:h}=load();
  const good={status:'needs_review',hardFailures:[]};
  assert.ok(h.adoptionBlock(undefined,'viewed',false));
  assert.ok(h.adoptionBlock(good,'viewed',true));
  assert.ok(h.adoptionBlock(good,' ',false));
  assert.ok(h.adoptionBlock({...good,status:'failed'},'override',false));
  assert.ok(h.adoptionBlock({...good,hardFailures:['NO_STRUCTURAL_MOTION']},'override',false));
  assert.equal(h.adoptionBlock(good,'五官清晰，物体结构一致',false),'');
});
test('regions reject paths, invalid bounds, missing annotation and excessive count',()=>{
  const {helper:h}=load();
  assert.equal(h.parseInspectionRegions(JSON.stringify(regions)).bodyRegions.length,1);
  for(const invalid of [{...regions,path:'C:/private'}, {...regions,exclusionRegions:[]},{...regions,bodyRegions:Array(9).fill(regions.bodyRegions[0])},{...regions,backgroundRegion:{x:.9,y:0,width:.2,height:1}},{...regions,bodyRegions:[{...regions.bodyRegions[0],path:'injected'}]}])assert.throws(()=>h.parseInspectionRegions(JSON.stringify(invalid)));
});
test('save whitelists editable metadata and propagates revision conflict instead of retry overwrite',async()=>{
  const calls=[];
  const {helper:h,ApiError}=load(async(url,options)=>{calls.push({url,...options});return {ok:false,status:409,text:async()=>'revision changed'};});
  const pack={id:'pack/one',revision:7,name:'demo',description:'',characters:{},cameras:[],clips:[],checks:{fake:true},reviews:{accepted:true},path:'C:/private'};
  await assert.rejects(h.saveMotionPack(pack,pack),e=>e instanceof ApiError&&e.status===409);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/motion-packs/pack%2Fone');
  const body=JSON.parse(calls[0].body);
  assert.equal(body.expectedRevision,7);
  assert.deepEqual(Object.keys(body.draft).sort(),['cameras','characters','clips','description','name']);
});
test('inspection and review use revision and cannot send detector conclusions',async()=>{
  const calls=[];
  const {helper:h}=load(async(url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>({revision:9})};});
  await h.inspectMotionClip({id:'pack',revision:8},'clip',regions);
  await h.reviewMotionClip({id:'pack',revision:9},'clip',true,'  人工核对完成  ');
  assert.equal(JSON.parse(calls[0].body).expectedRevision,8);
  assert.equal(JSON.parse(calls[1].body).reason,'人工核对完成');
  assert.equal(JSON.parse(calls[1].body).expectedRevision,9);
  assert.equal(JSON.parse(calls[0].body).report,undefined);
});
test('uploads send raw File bytes only and invalid video is rejected before request',async()=>{
  const calls=[];
  const {helper:h}=load(async(url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>({artifactId:'registered',fileHash:'a'.repeat(64)})};});
  await assert.rejects(h.uploadMotionVideo({name:'private.exe',size:10}));
  await assert.rejects(h.uploadMotionVideo({name:'clip.mp4',size:201*1024*1024}));
  assert.equal(calls.length,0);
  const file={name:'clip.mp4',size:10};await h.uploadMotionVideo(file);
  assert.equal(calls[0].url,'/api/motion-media?extension=mp4');assert.equal(calls[0].body,file);
  const image={name:'ref.png',size:10};await h.uploadMotionImage(image);
  assert.equal(calls[1].url,'/api/motion-media/images');assert.equal(calls[1].body,image);
});
test('fixed boxes only cover selected frames and imported boxes require complete bounded integer coverage',()=>{
  const {helper:h}=load();
  const clip={frameCount:4,usableStartFrame:1,usableEndFrame:3,nativeWidth:100,nativeHeight:80};
  const boxes=h.selectedFixedBoxes(clip,[10,10,30,40]);
  assert.equal(boxes[0],null);assert.equal(boxes[3],null);
  assert.deepEqual(Array.from(boxes[1]),[10,10,30,40]);
  assert.equal(h.validateFaceBoxes(clip,boxes).length,4);
  for(const invalid of [[null,null,null,null],[null,[true,0,5,5],[0,0,5,5],null],[null,[0,0,101,5],[0,0,5,5],null],[null,[0,0,5,5],null]])assert.throws(()=>h.validateFaceBoxes(clip,invalid));
  assert.throws(()=>h.selectedFixedBoxes(clip,[0,0,100.1,50]));
});
