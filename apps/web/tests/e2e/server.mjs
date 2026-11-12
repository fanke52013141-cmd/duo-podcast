// Dedicated process tree and a fresh storage root for every regression run.
import {spawn, spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, writeFileSync} from 'node:fs';
import path from 'node:path';

const repo=path.resolve(import.meta.dirname,'../../../..');
const workspace=process.env.DUOCAST_E2E_WORKSPACE;
const review=path.join(repo,'.review','browser-regression');
if(!workspace || !path.resolve(workspace).startsWith(review+path.sep)) throw new Error('E2E workspace must stay under .review/browser-regression');
const python=process.env.DUOCAST_TEST_PYTHON ?? path.join(repo,'apps/server/.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
if(!existsSync(python)) throw new Error('Missing server venv; set DUOCAST_TEST_PYTHON or follow scripts/README.md');
if(!existsSync(path.join(repo,'apps/web/dist/index.html'))) throw new Error('Build the frontend before test:e2e');
mkdirSync(workspace,{recursive:true});
const env={...process.env,PYTHONUTF8:'1',DUOCAST_DEMO_ONLY:'1',
  DUOCAST_STORAGE:path.join(workspace,'storage'),DUOCAST_PORT:process.env.DUOCAST_E2E_PORT,
  NO_PROXY:'127.0.0.1,localhost,::1'};
const seed=spawnSync(python,[path.join(repo,'scripts/acceptance/seed_cpu_workspace.py'),'--storage',env.DUOCAST_STORAGE],
  {cwd:repo,env,encoding:'utf8',timeout:45_000,windowsHide:true});
if(seed.status!==0) throw new Error(`CPU fixture setup failed: ${seed.stderr || seed.error || seed.stdout}`);
writeFileSync(path.join(workspace,'fixture-setup.log'),seed.stdout,'utf8');
console.log(`E2E isolated workspace: ${workspace}`);
const server=spawn(python,['-u','-m','uvicorn','duocast.main:app','--host','127.0.0.1','--port',env.DUOCAST_PORT],
  {cwd:path.join(repo,'apps/server'),env,stdio:'inherit',windowsHide:true});
server.on('error',error=>{console.error(error);process.exitCode=1;});
server.on('exit',code=>{process.exitCode=code??1;});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.kill());
process.on('exit',()=>server.kill());
