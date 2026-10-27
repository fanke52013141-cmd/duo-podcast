import {spawn,execFileSync} from 'node:child_process';
import {mkdirSync,openSync,closeSync} from 'node:fs';
import path from 'node:path';

export default async function setup(){
  const url=`http://127.0.0.1:${process.env.DUOCAST_E2E_PORT}/api/health`;
  const available=async()=>{try{return (await fetch(url,{signal:AbortSignal.timeout(500)})).ok;}catch{return false;}};
  if(await available())throw new Error(`E2E port is already occupied: ${url}`);
  const workspace=process.env.DUOCAST_E2E_WORKSPACE!;mkdirSync(workspace,{recursive:true});
  const log=openSync(path.join(workspace,'server.log'),'a');
  const server=spawn(process.execPath,[path.join(import.meta.dirname,'server.mjs')],{
    env:{...process.env,PYTHONUTF8:'1'},stdio:['ignore',log,log],windowsHide:true,
    detached:process.platform!=='win32',
  });closeSync(log);
  let failure:Error|undefined;server.on('error',e=>failure=e);
  const cleanup=()=>{
    if(!server.pid)return;
    if(process.platform==='win32'){
      // Terminate only the process tree created above, including venv launchers.
      try{execFileSync(path.join(process.env.SystemRoot??'C:\\Windows','System32','taskkill.exe'),['/PID',String(server.pid),'/T','/F'],{stdio:'ignore',windowsHide:true,timeout:10_000});}catch{if(server.exitCode===null)server.kill();}
    }else{try{process.kill(-server.pid,'SIGKILL');}catch{}}
  };
  try{
    const deadline=Date.now()+45_000;
    while(Date.now()<deadline){
      if(failure)throw failure;if(server.exitCode!==null)throw new Error(`E2E server exited: ${server.exitCode}; see ${workspace}/server.log`);
      if(await available())return async()=>{cleanup();};
      await new Promise(r=>setTimeout(r,200));
    }
    throw new Error(`E2E server startup timed out; see ${workspace}/server.log`);
  }catch(error){cleanup();throw error;}
}
