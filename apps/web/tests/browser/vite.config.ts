import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { createReadStream, statSync } from 'node:fs';
import { resolve } from 'node:path';

export default defineConfig({plugins:[react(),{name:'isolated-fixture-media',configureServer(server){
  server.middlewares.use((req,res,next)=>{
    if(req.url==='/api/artifacts/fixture/file'){
      res.setHeader('Content-Type','video/mp4');
      const file=resolve('../../.review/expert-ux-fixture.mp4');
      const size=statSync(file).size;
      const range=/^bytes=(\d+)-(\d*)$/.exec(req.headers.range ?? '');
      const start=range?Number(range[1]):0;
      const end=range&&range[2]?Math.min(Number(range[2]),size-1):size-1;
      res.setHeader('Accept-Ranges','bytes');res.setHeader('Content-Length',end-start+1);
      if(range){res.statusCode=206;res.setHeader('Content-Range',`bytes ${start}-${end}/${size}`);}
      const stream=createReadStream(file,{start,end});
      stream.on('error',()=>{res.statusCode=404;res.end('fixture not prepared');});
      stream.pipe(res);
    }else if(req.url?.startsWith('/api/')){res.statusCode=403;res.end('isolated fixture server: production API disabled');}
    else next();
  });
}}],server:{host:'127.0.0.1',port:5189,strictPort:true}});
