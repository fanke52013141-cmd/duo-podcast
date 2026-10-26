const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, extras = {}) {
  const exports = {};
  const source = fs.readFileSync(path.join(__dirname, '../src/lib', file), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  vm.runInNewContext(compiled.outputText, { exports, ...extras });
  return exports;
}

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
