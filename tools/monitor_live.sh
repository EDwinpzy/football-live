#!/bin/bash
# 足球直播 10 分钟播放监控：采集心跳/错误/卡顿日志，供事后分析
# usage: monitor_live.sh <分钟数>
ADB='/c/应用/MuMuPlayer/nx_main/adb.exe'
SER=127.0.0.1:16416
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/backend-src/monitor_live.log"
MINS=${1:-10}
echo "=== monitor start $(date +%H:%M:%S) ===" > "$OUT"
for i in $(seq 1 $((MINS * 2))); do
  "$ADB" -s $SER logcat -d -s OTV:V 2>/dev/null \
    | grep -aE "hb |playerError|stall|line fail|live renew|自动重试|vlc|VLC|丢帧|watchdog" >> "$OUT"
  echo "--- tick $i @ $(date +%H:%M:%S) ---" >> "$OUT"
  sleep 30
done
echo "=== monitor end $(date +%H:%M:%S) ===" >> "$OUT"
