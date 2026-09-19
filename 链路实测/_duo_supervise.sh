#!/bin/bash
# 监督循环：swap=8 渲染 4 段裁窗，服务器掉线自动重启续跑，最多 12 轮
cd "C:/Users/Administrator/Desktop/双人播客项目/链路实测"
for round in $(seq 1 12); do
  if [ -f _video/duocast_duo_sameframe.mp4 ]; then echo "SUPERVISE_DONE"; break; fi
  echo "== round $round $(date +%H:%M:%S)"
  if ! curl -s --noproxy "*" -o /dev/null -w "%{http_code}" http://127.0.0.1:8188/system_stats | grep -q 200; then
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -like '*main.py*8188*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" 2>/dev/null
    sleep 3
    cd "D:/PPT_Studio_Assets/InfiniteTalk_TTS/InfiniteTalk_Runtime/ComfyUI"
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 ./../venv/Scripts/python.exe main.py --port 8188 --listen 127.0.0.1 >> "C:/Users/Administrator/Desktop/双人播客项目/链路实测/_video/comfy_server.log" 2>&1 &
    cd "C:/Users/Administrator/Desktop/双人播客项目/链路实测"
    for i in $(seq 1 40); do sleep 5; curl -s --noproxy "*" -o /dev/null -w "%{http_code}" http://127.0.0.1:8188/system_stats 2>/dev/null | grep -q 200 && break; done
  fi
  SWAP_BLOCKS=8 PYTHONIOENCODING=utf-8 python make_video_duo_auto.py >> _video/duo_auto.log 2>&1
done
