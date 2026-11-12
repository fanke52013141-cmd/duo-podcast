const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function deferredPlayer() {
  const pending = [];
  class Audio {
    constructor() { this.paused = true; this.currentTime = 0; this.duration = 10; this.events = {}; }
    addEventListener(name, callback) { this.events[name] = callback; }
    pause() { this.paused = true; this.events.pause?.(); }
    play() {
      this.paused = false; this.events.play?.();
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    }
  }
  const player = load('player.ts', { Audio, require: () => ({ useSyncExternalStore: (_subscribe, snapshot) => snapshot() }) });
  return { player, pending };
}

test('superseded play rejection cannot mark the current candidate as failed', async () => {
  const { player, pending } = deferredPlayer();
  const first = player.playAudio('old');
  const second = player.playAudio('new');
  pending[1].resolve(); await second;
  pending[0].reject(new Error('interrupted by source switch')); await first;
  assert.equal(player.usePlayer().assetId, 'new');
  assert.equal(player.usePlayer().playing, true);
  assert.equal(player.usePlayer().error, '');
});

test('intentional pause and stop do not surface interrupted play as an error', async () => {
  for (const action of ['pauseAudio', 'stopAudio']) {
    const { player, pending } = deferredPlayer();
    const playing = player.playAudio('candidate');
    player[action]();
    pending[0].reject(new Error('interrupted')); await playing;
    assert.equal(player.usePlayer().playing, false);
    assert.equal(player.usePlayer().error, '');
  }
});

test('retrying the same candidate clears its previous play failure', async () => {
  const { player, pending } = deferredPlayer();
  const first = player.playAudio('candidate');
  pending[0].reject(new Error('blocked')); await first;
  assert.match(player.usePlayer().error, /播放未能开始/);
  assert.equal(player.usePlayer().playing, false);
  const retry = player.playAudio('candidate');
  assert.equal(player.usePlayer().error, '');
  pending[1].resolve(); await retry;
  assert.equal(player.usePlayer().playing, true);
});

function load(file, extras = {}) {
  const exports = {};
  const source = fs.readFileSync(path.join(__dirname, '../src/lib', file), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  vm.runInNewContext(compiled.outputText, { exports, ...extras });
  return exports;
}

test('waveform covers every sample and both channels without cancelling opposite phases',()=>{
  const {audioPeaks}=load('waveform.ts');
  const peaks=audioPeaks([new Float32Array([0,.2,0,-.8,1]),new Float32Array([0,-.2,0,.8,-1])],2);
  assert.equal(peaks.length,2);assert.ok(Math.abs(peaks[0]-.2)<1e-6);assert.equal(peaks[1],1);
  assert.equal(audioPeaks([new Float32Array([0,0])],240).length,2);
  assert.throws(()=>audioPeaks([],10));assert.throws(()=>audioPeaks([new Float32Array(2),new Float32Array(1)]));
});

test('selection rejects empty reversed outside and nonfinite ranges',()=>{
  const {boundedSelection}=load('waveform.ts');
  for(const args of [[0,0,2],[2,1,2],[-1,1,2],[0,3,2],[NaN,1,2],[0,Infinity,2]])assert.equal(boundedSelection(...args),null);
  assert.deepEqual(Array.from(boundedSelection(.5,2,2)),[.5,2]);
});

test('waveform download enforces streaming size bound even without a length header',async()=>{
  const {readBoundedAudio}=load('waveform.ts',{DOMException,Uint8Array});
  let cancelled=false,released=false;
  const response={ok:true,headers:{get:()=>null},body:{getReader:()=>({read:async()=>({done:false,value:new Uint8Array(5)}),cancel:async()=>{cancelled=true;},releaseLock:()=>{released=true;}})}};
  await assert.rejects(readBoundedAudio(response,new AbortController().signal,4),/64MB/);
  assert.equal(cancelled,true);assert.equal(released,true);
});

test('waveform read combines actual bytes and abort releases stream',async()=>{
  const {readBoundedAudio}=load('waveform.ts',{DOMException,Uint8Array});
  let i=0,cancelled=0;
  const response={ok:true,headers:{get:()=>null},body:{getReader:()=>({read:async()=>i++?{done:true}:{done:false,value:new Uint8Array([1,2,3])},cancel:async()=>{cancelled++;},releaseLock:()=>{}})}};
  assert.deepEqual(Array.from(new Uint8Array(await readBoundedAudio(response,new AbortController().signal))),[1,2,3]);
  const aborted=new AbortController();aborted.abort();
  await assert.rejects(readBoundedAudio(response,aborted.signal),{name:'AbortError'});assert.equal(cancelled,2);
});

test('tabs wrap across enabled items and support Home and End',()=>{
  const {tabDestination}=load('keyboard.ts');
  assert.equal(tabDestination(['A','C'],'A','ArrowRight'),'C');
  assert.equal(tabDestination(['A','C'],'A','ArrowLeft'),'C');
  assert.equal(tabDestination(['A','C'],'C','Home'),'A');
  assert.equal(tabDestination(['A','C'],'A','End'),'C');
  assert.equal(tabDestination([],'A','ArrowRight'),null);assert.equal(tabDestination(['A'],'A','Enter'),null);
});

test('selected audio stops at endpoint and seeking clears the old selection',async()=>{
  let element;
  class Audio {
    constructor(){element=this;this.currentTime=0;this.duration=5;this.paused=true;this.events={};}
    addEventListener(name,fn){this.events[name]=fn;}
    pause(){this.paused=true;this.events.pause?.();}
    async play(){this.paused=false;this.events.play?.();}
  }
  const player=load('player.ts',{Audio,require:()=>({useSyncExternalStore:(_s,get)=>get()})});
  await player.playAudio('a',1,2);element.currentTime=2.1;element.events.timeupdate();
  assert.equal(element.paused,true);assert.equal(player.usePlayer().current,2);
  await player.playAudio('a',1,2);player.seekAudio(3);element.events.timeupdate();assert.equal(element.paused,false);
  await player.playAudio('a',NaN);assert.equal(element.currentTime,3);
});

test('navigation waits for persistence and prevents leaving after a failed save', async () => {
  const nav = load('navigation.ts');
  let calls = 0;
  let finish;
  nav.registerNavigationGuard(() => new Promise(resolve => { finish = resolve; }));
  const pending = nav.navigate(() => calls++);
  assert.equal(calls, 0);
  await nav.navigate(() => { throw new Error('duplicate navigation should be suppressed'); });
  finish(false); await pending;
  assert.equal(calls, 0);
  nav.registerNavigationGuard(async () => true);
  await nav.navigate(() => calls++);
  assert.equal(calls, 1);
});

test('unmount cleanup cannot remove a newer page guard', async () => {
  const nav = load('navigation.ts');
  const removeOld = nav.registerNavigationGuard(async () => true);
  nav.registerNavigationGuard(async () => false);
  removeOld();
  let navigated = false;
  await nav.navigate(() => { navigated = true; });
  assert.equal(navigated, false);
});

test('recovery storage tolerates invalid JSON and quota failures', () => {
  const nav = load('navigation.ts', { localStorage: { getItem: () => 'not json', setItem: () => { throw new Error('quota'); } } });
  assert.equal(nav.recoveryRead('draft'), null);
  assert.doesNotThrow(() => nav.recoveryWrite('draft', { text: 'kept by server' }));
});

test('a storage cleanup failure cannot break a successful server save', () => {
  const nav=load('navigation.ts',{localStorage:{removeItem(){throw new Error('storage disabled');}}});
  assert.doesNotThrow(()=>nav.recoveryRemove('saved-draft'));
});

test('version switching and recovery suppress duplicates but unlock after failure',async()=>{
  const nav=load('navigation.ts');const run=nav.createSingleFlight();let finish,calls=0;
  const first=run(async()=>{calls++;await new Promise(resolve=>finish=resolve);});
  await run(async()=>calls++);assert.equal(calls,1);finish();await first;
  await assert.rejects(run(async()=>{throw new Error('save failed');}));
  await run(async()=>calls++);assert.equal(calls,2);
});

test('script replacement is literal, role-scoped and preserves custom pronunciation',()=>{
  const tools=load('workflowTools.ts');
  const turns=[{id:'T1',speaker:'A',lines:[{id:'L1',displayText:'价格 $5 $5',spokenText:'价格 五元 五元'}]},
    {id:'T2',speaker:'B',lines:[{id:'L2',displayText:'$5',spokenText:'$5'}]}];
  const result=tools.replaceTurns(turns,'$5','$10','A');
  assert.equal(result.count,2);assert.equal(result.turns[0].lines[0].displayText,'价格 $10 $10');
  assert.equal(result.turns[0].lines[0].spokenText,'价格 五元 五元');
  assert.equal(result.turns[1],turns[1]);assert.equal(turns[0].lines[0].displayText,'价格 $5 $5');
  assert.equal(tools.replaceTurns(turns,'','bad','all').turns,turns);
  assert.equal(tools.replaceTurns(turns,'$5','','B').turns[1].lines[0].spokenText,'');
});

test('corrupt local recovery cannot be treated as editable script content',()=>{
  const tools=load('workflowTools.ts');
  for(const value of [null,[],{revisionId:'R1',turns:'wrong'},{revisionId:'R1',turns:[{id:'T1',speaker:'A',lines:[null]}]}])assert.equal(tools.validScriptRecovery(value),false);
  assert.equal(tools.validScriptRecovery({revisionId:'R1',turns:[{id:'T1',speaker:'A',lines:[{id:'L1',displayText:'你好',spokenText:'你好'}]}]}),true);
});

test('project filters and sorting are stable and do not mutate source data',()=>{
  const tools=load('workflowTools.ts');
  const projects=[{id:'B',title:'早报',updatedAt:'2026-10-01',stage:'voice'},{id:'A',title:'早报',updatedAt:'2026-10-02',stage:'script'}];
  assert.equal(tools.filterProjects(projects,' 早报 ','voice','recent',p=>p.stage)[0].id,'B');
  assert.equal(tools.filterProjects(projects,'','all','recent',p=>p.stage)[0].id,'A');
  assert.equal(tools.filterProjects(projects,'','all','oldest',p=>p.stage)[0].id,'B');
  assert.equal(tools.filterProjects(projects,'','all','title',p=>p.stage)[0].id,'A');
  assert.equal(projects[0].id,'B');assert.equal(tools.filterProjects(projects,'absent','all','id',p=>p.stage).length,0);
});

test('error guides route to actionable settings or maintenance without echoing secrets',()=>{
  const tools=load('workflowTools.ts');
  assert.equal(tools.errorGuide('ENOSPC disk full','renders').target,'home');
  const credentials=tools.errorGuide('401 secret-value','script.generate');
  assert.equal(credentials.target,'services');assert.ok(!JSON.stringify(credentials).includes('secret-value'));
  assert.equal(tools.errorGuide('文件缺失','tts.synthesize').target,'voice');
  assert.equal(tools.errorGuide('connection timeout','renders').target,'services');
  assert.equal(tools.errorGuide('版本冲突','script.generate').target,'script');
});

test('script save conflict is surfaced after one PATCH rather than replaying over a newer draft',async()=>{
  let mutation,calls=0;
  const api=load('api.ts',{require:()=>({useQueryClient:()=>({}),useMutation:options=>{mutation=options;return options;}}),
    fetch:async()=>{calls++;return {ok:false,status:409,text:async()=>'conflict'};}});
  api.useSaveDraft();
  await assert.rejects(mutation.mutationFn({projectId:'P1',patch:{scriptRevisions:[]},expectedRevision:1}),/409/);
  assert.equal(calls,1);
});

test('shared audio uses one element and switches candidates without overlapping playback', async () => {
  const elements = [];
  class Audio {
    constructor() { this.paused = true; this.currentTime = 0; this.duration = 10; this.events = {}; elements.push(this); }
    addEventListener(name, callback) { this.events[name] = callback; }
    pause() { this.paused = true; this.events.pause?.(); }
    async play() { this.paused = false; this.events.play?.(); }
  }
  const player = load('player.ts', { Audio, require: () => ({ useSyncExternalStore: (_subscribe, snapshot) => snapshot() }) });
  await player.playAudio('candidate-one');
  await player.playAudio('candidate-two', 2);
  assert.equal(elements.length, 1);
  assert.equal(elements[0].src, '/api/artifacts/candidate-two/file');
  assert.equal(player.usePlayer().assetId, 'candidate-two');
  assert.equal(player.usePlayer().playing, true);
  player.pauseAudio();
  assert.equal(player.usePlayer().playing, false);
  player.seekAudio(4); elements[0].events.timeupdate();
  assert.equal(player.usePlayer().current, 4);
  elements[0].events.error();
  assert.match(player.usePlayer().error, /音频无法读取/);
});

test('an earlier selection keeps its endpoint after pausing later playback',async()=>{
  let element;
  class Audio{
    constructor(){element=this;this.paused=true;this.currentTime=0;this.duration=3;this.events={};}
    addEventListener(name,callback){this.events[name]=callback;}
    pause(){this.paused=true;this.events.pause?.();}
    async play(){this.paused=false;this.events.play?.();}
  }
  const player=load('player.ts',{Audio,require:()=>({useSyncExternalStore:(_subscribe,snapshot)=>snapshot()})});
  await player.playAudio('tone',2.5);await player.playAudio('tone',0.5,1.5);
  element.currentTime=1.7;element.events.timeupdate();
  assert.equal(player.usePlayer().playing,false);assert.equal(player.usePlayer().current,1.5);
  player.stopAudio();assert.equal(player.usePlayer().current,0);
});
