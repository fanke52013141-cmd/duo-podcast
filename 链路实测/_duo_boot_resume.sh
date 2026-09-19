#!/bin/bash
# 重启后自动续跑：swap=32（昨日验证稳定且 8~10min/段）渲染 4 个裁窗并合成双人同框成片。
# 由计划任务 DuoCastBootResume（SYSTEM，开机触发）调用；跑完或放弃时自行注销任务。
LOG_DIR="C:/Users/Administrator/Desktop/双人播客项目/链路实测/_video"
PY_CLIENT="C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe"
PY_SERVER="D:/PPT_Studio_Assets/InfiniteTalk_TTS/InfiniteTalk_Runtime/venv/Scripts/python.exe"
COMFY="D:/PPT_Studio_Assets/InfiniteTalk_TTS/InfiniteTalk_Runtime/ComfyUI"
cd "C:/Users/Administrator/Desktop/双人播客项目/链路实测" || exit 1
echo "== boot resume $(date +%F_%H:%M:%S)" >> "$LOG_DIR/duo_boot.log"
if [ -f _video/duocast_duo_sameframe.mp4 ]; then
  echo "final already exists; unregister" >> "$LOG_DIR/duo_boot.log"
else
  sleep 25
  for round in $(seq 1 8); do
    [ -f _video/duocast_duo_sameframe.mp4 ] && break
    echo "== round $round $(date +%H:%M:%S)" >> "$LOG_DIR/duo_boot.log"
    if ! curl -s --noproxy "*" -o /dev/null -w "%{http_code}" http://127.0.0.1:8188/system_stats | grep -q 200; then
      cd "$COMFY"
      PYTHONIOENCODING=utf-8 PYTHONUTF8=1 "$PY_SERVER" main.py --port 8188 --listen 127.0.0.1 >> "$LOG_DIR/comfy_boot.log" 2>&1 &
      cd "C:/Users/Administrator/Desktop/双人播客项目/链路实测"
      for i in $(seq 1 40); do
        sleep 5
        curl -s --noproxy "*" -o /dev/null -w "%{http_code}" http://127.0.0.1:8188/system_stats | grep -q 200 && break
      done
    fi
    SWAP_BLOCKS=32 PYTHONIOENCODING=utf-8 "$PY_CLIENT" make_video_duo_auto.py >> "$LOG_DIR/duo_boot.log" 2>&1
  done
fi
schtasks //delete //tn DuoCastBootResume //f >> "$LOG_DIR/duo_boot.log" 2>&1
