const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const source=fs.readFileSync(path.join(__dirname,'../src/lib/motionComparison.ts'),'utf8');
const exportsObject={};
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:exportsObject});
const {createMotionComparison,sourceReference}=exportsObject;
function media(duration=10) {
  return {duration,currentTime:0,playbackRate:1,paused:true,playCalls:0,pauseCalls:0,
    async play(){this.playCalls++;this.paused=false;},pause(){this.pauseCalls++;this.paused=true;}};
}
test('accepts only registered reference shape and never uses arbitrary path',()=>{
  assert.equal(sourceReference(null),null);
  assert.equal(sourceReference({path:'C:/private.mp4'}),null);
  assert.equal(sourceReference({artifactId:'asset',fileHash:'123'}),null);
  assert.equal(sourceReference({artifactId:'',fileHash:'a'.repeat(64)}),null);
  assert.equal(sourceReference({artifactId:'asset',fileHash:'a'.repeat(64)}).artifactId,'asset');
});
test('processed video drives play, seek, rate, drift correction and pause',async()=>{
  const leader=media(), follower=media(), notes=[];
  const controller=createMotionComparison(leader,follower,n=>notes.push(n));
  leader.currentTime=3;leader.playbackRate=.5;leader.paused=false;
  controller.setOffset(1.25);
  await controller.play();
  assert.equal(follower.currentTime,4.25);
  assert.equal(follower.playbackRate,.5);
  assert.equal(follower.paused,false);
  leader.currentTime=4;controller.sync();
  assert.equal(follower.currentTime,5.25);
  follower.currentTime=5.20;controller.sync();
  assert.equal(follower.currentTime,5.20); // no continuous seek for small drift
  leader.playbackRate=1.5;controller.sync(true);
  assert.equal(follower.playbackRate,1.5);
  controller.pause();assert.equal(follower.paused,true);
  controller.dispose();
});
test('unavailable metadata or out-of-range offset pauses instead of holding an endpoint',()=>{
  const leader=media(),follower=media(NaN),notes=[];
  const controller=createMotionComparison(leader,follower,n=>notes.push(n));
  leader.paused=false;controller.sync();
  assert.equal(leader.paused,true);assert.match(notes.at(-1),/元数据/);
  follower.duration=2;leader.currentTime=1;leader.paused=false;
  controller.setOffset(1);
  assert.equal(leader.paused,true);assert.match(notes.at(-1),/超出/);
  leader.paused=false;controller.setOffset(NaN);
  assert.equal(leader.paused,true);assert.match(notes.at(-1),/有限/);
});
test('late follower play after pause or dispose cannot restart playback',async()=>{
  let resolvePlay;
  const leader=media(), follower=media(), notes=[];
  follower.play=function(){return new Promise(resolve=>{resolvePlay=()=>{this.paused=false;resolve();};});};
  const controller=createMotionComparison(leader,follower,n=>notes.push(n));
  leader.paused=false;
  const pending=controller.play();
  controller.pause();resolvePlay();await pending;
  assert.equal(follower.paused,true);
  leader.paused=false;
  const second=controller.play();
  controller.dispose();resolvePlay();await second;
  assert.equal(follower.paused,true);
});
test('failed follower playback pauses both players and reports it',async()=>{
  const leader=media(), follower=media(), notes=[];
  follower.play=async()=>{throw new Error('decode failed');};
  leader.paused=false;
  await createMotionComparison(leader,follower,n=>notes.push(n)).play();
  assert.equal(leader.paused,true);assert.equal(follower.paused,true);
  assert.match(notes.at(-1),/无法播放/);
});
