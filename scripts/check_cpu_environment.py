"""Offline developer preflight. Does not install, call engines or load GPU libraries."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sys


def inspect(root, port=8100, *, check_port=True):
    root = Path(root)
    checks = [{'name':'Python >= 3.10','ok':sys.version_info >= (3,10),'required':True}]
    for module in ('fastapi','uvicorn','pydantic','httpx','sse_starlette','multipart'):
        checks.append({'name':module,'ok':importlib.util.find_spec(module) is not None,'required':True})
    checks.append({'name':'前端生产构建','ok':(root/'apps/web/dist/index.html').is_file(),'required':True})
    for name in ('node','ffmpeg','ffprobe'):
        checks.append({'name':name,'ok':shutil.which(name) is not None,'required':False})
    if check_port:
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',port))
            checks.append({'name':f'本机端口 {port}','ok':True,'required':True})
        except OSError:
            checks.append({'name':f'本机端口 {port}','ok':False,'required':True,
                           'hint':'端口已占用或当前执行环境禁止监听，请更换端口或在允许监听的终端启动。'})
    return {'ready':all(c['ok'] for c in checks if c['required']), 'checks':checks,
            'mode':'CPU/mock','notice':'无需CUDA、显卡、模型和密钥；FFmpeg用于媒体检查，缺失不阻止基础编辑。'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8100)
    parser.add_argument('--skip-port',action='store_true')
    args=parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    report=inspect(Path(__file__).resolve().parents[1],args.port,check_port=not args.skip_port)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['ready'] else 1


if __name__=='__main__':
    raise SystemExit(main())
